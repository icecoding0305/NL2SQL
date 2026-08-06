"""Embedding 适配层:对外暴露统一的 embed(texts) -> list[list[float]]。

- provider: local → 本地 sentence-transformers(可换模型/维度)
- provider: fake  → 测试用确定性词袋向量(不依赖模型)
- 以后接入 API 形式(如某厂商 embedding 接口),只改这里的 provider 分支,调用方不变。

配置:config/model_config.yaml 的 embedding 段。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Callable

from nl2sql_agent.services.config_loader import ConfigLoader

EmbedFn = Callable[[list[str]], list[list[float]]]

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


def load_model_config() -> dict:
    return ConfigLoader(_CONFIG_DIR).load("model_config.yaml")


@lru_cache(maxsize=1)
def _load_local_model(model_name: str):
    from sentence_transformers import SentenceTransformer  # 延迟导入

    return SentenceTransformer(model_name)


def get_embedding_function() -> EmbedFn:
    """按配置返回统一的 embedding 函数 embed(texts) -> list[list[float]]。"""
    cfg = load_model_config().get("embedding", {})
    provider = cfg.get("provider", "local")
    if provider == "local":
        # model_path 优先(本地已下载的模型目录,如从 ModelScope 下载;相对路径按项目根解析);
        # 否则按 model 名从 huggingface 下载
        model_ref = cfg.get("model_path") or cfg.get("model", "paraphrase-multilingual-MiniLM-L12-v2")
        if model_ref and not Path(model_ref).is_absolute() and cfg.get("model_path"):
            # 相对路径按项目根(nl2sql_agent/config 向上两级)解析
            model_ref = str(_CONFIG_DIR.parent.parent / model_ref)
        try:
            model = _load_local_model(model_ref)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"加载本地 embedding 模型失败: {e}\n"
                "模型需可下载或离线缓存。可设置 HF_ENDPOINT 指向 huggingface 镜像 "
                "(如 https://hf-mirror.com),或从 ModelScope 下载后配置 model_path,"
                "或用 provider: fake 仅测试。"
            ) from e

        # A retrieval request searches table/column/relation collections with the
        # same query text. Cache singleton query embeddings so one request does not
        # run the local transformer three or more times.
        @lru_cache(maxsize=int(cfg.get("query_cache_size", 256)))
        def embed_one(text: str) -> tuple[float, ...]:
            vector = model.encode([text], normalize_embeddings=True)[0]
            return tuple(float(value) for value in vector.tolist())

        def embed(texts: list[str]) -> list[list[float]]:
            values = list(texts)
            if len(values) == 1:
                return [list(embed_one(values[0]))]
            return model.encode(values, normalize_embeddings=True).tolist()

        return embed
    if provider == "fake":
        return fake_embedding
    raise ValueError(f"不支持的 embedding provider: {provider!r}")


# ---------------- 测试用确定性词袋向量 ----------------

_BUCKETS = 256


def fake_embedding(texts: list[str]) -> list[list[float]]:
    """基于中文二元组 hash 到固定桶的词袋向量(确定性,仅测试,无语义泛化)。"""
    vecs = []
    for text in texts:
        vec = [0.0] * _BUCKETS
        norm = text.replace(" ", "").lower()
        for i in range(max(0, len(norm) - 1)):
            h = hash(norm[i : i + 2]) % _BUCKETS
            vec[h] += 1.0
        norm_len = sum(v * v for v in vec) ** 0.5
        vecs.append([v / norm_len for v in vec] if norm_len else vec)
    return vecs
