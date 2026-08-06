"""依赖装配:AppConfig 与 Deps,以及默认 build_deps。

规则/参数全部从 config/ 读取;LLM 走 Anthropic API(model 名从环境变量读取)。
支持项目根目录的 .env 文件(ANTHROPIC_API_KEY / ANTHROPIC_MODEL 等)。
测试环境通过注入 FakeLLM / InMemoryExecutor 替换。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
import json
from pathlib import Path

from dotenv import load_dotenv

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.executor import SQLExecutor
from nl2sql_agent.services.few_shot_store import FewShotStore
from nl2sql_agent.services.llm import BaseLLMClient, build_llm, build_sql_llm
from nl2sql_agent.services.prompt_loader import PromptLoader
from nl2sql_agent.services.schema_catalog import SchemaCatalog
from nl2sql_agent.services.sql_dialect import SqlDialect
from nl2sql_agent.services.term_mapping import TermMappingService
from nl2sql_agent.services.vector_store import InMemoryVectorStore, PgVectorStore, VectorStoreAdapter

# deps.py -> services -> nl2sql_agent -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# config 位于包内部(nl2sql_agent/config),与项目根目录分开
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_env() -> None:
    """加载项目根目录 .env(不存在则忽略;不覆盖已设置的系统环境变量)。"""
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)
    os.environ.setdefault("PROJECT_ROOT", str(PROJECT_ROOT))


@dataclass
class AppConfig:
    dialect: str = "postgres"
    schema_search_top_k: int = 5
    execution_limit: int = 1000
    execution_timeout_seconds: int = 30
    explain_row_threshold: int = 1_000_000
    row_level_filter: dict = field(default_factory=lambda: {"enabled": True, "column": "business_line"})
    clarification_rules: dict = field(default_factory=dict)
    complexity_rules: dict = field(default_factory=dict)
    sensitive_rules: dict = field(default_factory=dict)
    performance: dict = field(default_factory=dict)


@dataclass
class Deps:
    config: AppConfig
    loader: ConfigLoader
    llm: BaseLLMClient
    term_mapping: TermMappingService
    catalog: SchemaCatalog
    vector_store: VectorStore
    executor: SQLExecutor
    few_shot: FewShotStore
    sql: SqlDialect
    prompts: PromptLoader

    # SQL 专用模型(可选,未配置则 SQL 生成回退 llm)
    sql_llm: BaseLLMClient | None = None

    # 预留扩展点:反馈闭环、语义缓存(暂不实现,先留接口)
    feedback_sink: object = None
    semantic_cache: object = None


def build_executor_from_url(url: str) -> SQLExecutor:
    """按 URL scheme 选择执行器:mysql:// → MySQL,postgres(ql):// → Postgres。"""
    scheme = (url.split(":", 1)[0] or "").lower()
    if scheme.startswith("mysql"):
        from nl2sql_agent.services.executor import MySQLExecutor

        return MySQLExecutor.from_url(url)
    if scheme.startswith("postgres"):
        from nl2sql_agent.services.executor import PostgresExecutor

        return PostgresExecutor(url)
    raise RuntimeError(f"不支持的数据库类型: {scheme!r}(支持 mysql / postgres)")


def build_vector_store(
    loader: ConfigLoader,
    catalog: SchemaCatalog,
    embed=None,
    cache_signature_override: str | None = None,
) -> VectorStoreAdapter:
    """显式读 config/vector_store.yaml 选择后端(不再是环境变量隐式二选一)。

    embed 可注入(测试用 fake);生产默认走配置的 embedding(真语义模型)。
    """
    cfg = loader.load("vector_store.yaml") or {}
    backend = cfg.get("backend", "memory")
    if backend == "pgvector":
        url = os.path.expandvars(cfg.get("pgvector", {}).get("url", ""))
        if not url:
            raise RuntimeError(
                "vector_store.yaml 配置 backend=pgvector 但未提供 pgvector.url(或环境变量 PGVECTOR_URL 未设置)"
            )
        return PgVectorStore(url=url, catalog=catalog, embed=embed)
    embedding_cfg = loader.load("model_config.yaml").get("embedding", {})
    semantic_embedding_cfg = {
        key: embedding_cfg.get(key)
        for key in ("provider", "model", "model_path", "dimension")
        if key in embedding_cfg
    }
    cache_signature = cache_signature_override or json.dumps(
        semantic_embedding_cfg, ensure_ascii=False, sort_keys=True
    )
    return InMemoryVectorStore(catalog, embed=embed, cache_signature=cache_signature)


def build_deps(
    base_dir: str | Path | None = None,
    *,
    llm: BaseLLMClient | None = None,
    sql_llm: BaseLLMClient | None = None,
    executor: SQLExecutor | None = None,
    vector_store: VectorStoreAdapter | None = None,
) -> Deps:
    load_env()  # 先加载 .env,再读取环境变量
    base_dir = base_dir or CONFIG_DIR
    loader = ConfigLoader(base_dir)
    settings = loader.load("settings.yaml")
    db_url = (
        settings.get("database_url")
        or os.getenv("DATABASE_URL")
        or os.getenv("PG_DATABASE_URL")
        or settings.get("pg_database_url")
    )
    configured_dialect = settings.get("dialect", "postgres")
    if db_url:
        scheme = (db_url.split(":", 1)[0] or "").lower()
        if scheme.startswith("mysql"):
            configured_dialect = "mysql"
        elif scheme.startswith("postgres"):
            configured_dialect = "postgres"

    config = AppConfig(
        dialect=configured_dialect,
        schema_search_top_k=int(settings.get("schema_search_top_k", 5)),
        execution_limit=int(settings.get("execution", {}).get("limit", 1000)),
        execution_timeout_seconds=int(settings.get("execution", {}).get("timeout_seconds", 30)),
        explain_row_threshold=int(settings.get("execution", {}).get("explain_row_threshold", 1_000_000)),
        row_level_filter=settings.get("row_level_filter", {"enabled": True, "column": "business_line"}),
        clarification_rules=loader.load("clarification_rules.yaml").get("clarification_rules", {}),
        complexity_rules=loader.load("complexity_rules.yaml").get("complexity_rules", {}),
        sensitive_rules=loader.load("sensitive_rules.yaml").get("sensitive_rules", {}),
        performance=settings.get("performance", {}),
    )

    term_mapping = TermMappingService(loader)
    catalog = SchemaCatalog(loader)
    sql = SqlDialect(config.dialect)

    model_runtime = loader.load("model_config.yaml").get("runtime", {})
    sql_llm = sql_llm or build_sql_llm()  # SQL 专用模型(可选,未配置回退主模型)
    if llm is None:
        if model_runtime.get("main_model_source", "default") == "sql" and sql_llm is not None:
            # 计划、复杂问题理解、结果解释与 SQL 生成复用同一客户端和模型。
            llm = sql_llm
        else:
            llm = build_llm()

    # 向量存储:显式读 config/vector_store.yaml 选择后端
    if vector_store is None:
        vector_store = build_vector_store(loader, catalog)

    if executor is None:
        # 数据库连接:settings.yaml 的 database_url 优先,其次 .env 的 DATABASE_URL,
        # 再兼容旧字段 PG_DATABASE_URL / pg_database_url
        if not db_url:
            raise RuntimeError(
                "未配置数据库连接(.env 的 DATABASE_URL / settings.yaml 的 database_url "
                "或注入 executor),无法执行模块 10"
            )
        executor = build_executor_from_url(db_url)

    return Deps(
        config=config,
        loader=loader,
        llm=llm,
        sql_llm=sql_llm,
        term_mapping=term_mapping,
        catalog=catalog,
        vector_store=vector_store,
        executor=executor,
        few_shot=FewShotStore(loader),
        sql=sql,
        prompts=PromptLoader(CONFIG_DIR / "prompts"),
    )
