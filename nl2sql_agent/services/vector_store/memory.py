"""内存向量存储:真 embedding + 余弦相似度。

实现 VectorStoreAdapter;同时提供模块 3 使用的 schema 层方法 search_scored。
embed 由统一 embedding 函数提供(默认 get_embedding_function(),可注入 fake 用于测试)。
"""

from __future__ import annotations

import json
from pathlib import Path

from nl2sql_agent.services.embedding.router import EmbedFn, get_embedding_function
from nl2sql_agent.services.vector_store.base import VectorStoreAdapter
from nl2sql_agent.state import SchemaHit


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class InMemoryVectorStore(VectorStoreAdapter):
    COLLECTION_TABLE = "schema_table"    # 表级
    COLLECTION_COLUMN = "schema_column"  # 字段级
    COLLECTION_RELATION = "schema_relation"  # 关系级

    def __init__(self, catalog, embed: EmbedFn | None = None, cache_signature: str = ""):
        self.catalog = catalog
        self._embed = embed
        self._cache_signature = cache_signature or "default"
        self._store: dict[str, dict[str, dict]] = {}  # collection -> {id: {embedding, text, metadata}}
        self._indexed = False

    # ---------------- VectorStoreAdapter ----------------

    def upsert(self, collection: str, id: str, text: str, metadata: dict) -> None:
        vec = self._embed_texts([text])[0]
        self._store.setdefault(collection, {})[id] = {
            "embedding": vec,
            "text": text,
            "metadata": metadata,
        }

    def search(self, collection: str, query: str, top_k: int, filters: dict) -> list[dict]:
        query_vec = self._embed_texts([query])[0]
        candidates = self._store.get(collection, {})
        results = []
        for id, item in candidates.items():
            if not self._match_filters(item["metadata"], filters):
                continue
            score = max(0.0, _cosine(query_vec, item["embedding"]))
            results.append({"id": id, "text": item["text"], "metadata": item["metadata"], "score": score})
        results.sort(key=lambda r: -r["score"])
        return results[:top_k]

    @staticmethod
    def _match_filters(metadata: dict, filters: dict) -> bool:
        for key, value in filters.items():
            if metadata.get(key) != value:
                return False
        return True

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        if self._embed is None:
            self._embed = get_embedding_function()
        return self._embed(texts)

    # ---------------- schema 层(模块 3 用) ----------------

    def _ensure_indexed(self) -> None:
        if self._indexed:
            return
        from nl2sql_agent.services.schema_ingest.text_builder import (
            load_mschema_vector_source,
            write_mschema_table_embeddings,
        )

        source = load_mschema_vector_source(getattr(self.catalog, "metadata", {}))
        if source is not None:
            mschema, manifest = source
            if self._load_cache(manifest):
                self._indexed = True
                return
            self._indexed = True
            for table_name, table in mschema.get("tables", {}).items():
                if table.get("retrieval_eligible", True):
                    write_mschema_table_embeddings(self, mschema, table_name, manifest)
            self.persist_cache(
                getattr(self.catalog, "metadata", {}).get("m_schema_path", ""), manifest
            )
            return
        for tbl in self._all_tables():
            doc = self._table_doc(tbl)
            self.upsert(
                self.COLLECTION_TABLE,
                tbl.name,
                doc,
                {"table_name": tbl.name, "business_line": tbl.business_line},
            )
        self._indexed = True

    def _cache_path(self, m_schema_path: str | Path | None = None) -> Path | None:
        source = m_schema_path or getattr(self.catalog, "metadata", {}).get("m_schema_path")
        return Path(source).with_name("vector-cache.json") if source else None

    def _load_cache(self, manifest: dict) -> bool:
        path = self._cache_path()
        if path is None or not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if (
            payload.get("semantic_hash") != manifest.get("semantic_hash")
            or payload.get("embedding_signature") != self._cache_signature
        ):
            return False
        self._store = payload.get("collections", {})
        return True

    def persist_cache(self, m_schema_path: str | Path, manifest: dict) -> None:
        """原子保存派生向量；仅语义哈希和模型签名一致时才会在重启后复用。"""
        path = self._cache_path(m_schema_path)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "semantic_hash": manifest.get("semantic_hash", ""),
            "snapshot_id": manifest.get("snapshot_id", ""),
            "embedding_signature": self._cache_signature,
            "collections": self._store,
        }
        temporary_path = path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        temporary_path.replace(path)

    def prepare_incremental(self) -> None:
        """增量同步前恢复上一快照，随后只重算发生变化的表。"""
        self._ensure_indexed()

    def remove_table(self, table_name: str, columns_per_chunk: int = 1) -> None:
        """删除一张表的表级 + 字段级向量条目(增量同步的删表清理)。"""
        self._store.get(self.COLLECTION_TABLE, {}).pop(table_name, None)
        col_coll = self._store.get(self.COLLECTION_COLUMN, {})
        for key in [k for k in col_coll if k.startswith(f"{table_name}#col#")]:
            col_coll.pop(key, None)
        rel_coll = self._store.get(self.COLLECTION_RELATION, {})
        for key in [k for k in rel_coll if k.startswith(f"{table_name}#rel#")]:
            rel_coll.pop(key, None)

    def _all_tables(self):
        out = list(getattr(self.catalog, "_shared_tables", []))
        for tables in self.catalog._tables_by_line.values():  # noqa: SLF001
            out.extend(tables)
        return out

    @staticmethod
    def _table_doc(tbl) -> str:
        return " ".join(
            [tbl.name, tbl.comment]
            + [c["name"] + " " + c.get("comment", "") for c in tbl.columns]
        )

    def rebuild_index(self) -> None:
        """全量重建(embedding 模型切换后必须调用)。"""
        self._store.clear()
        self._indexed = False
        self._ensure_indexed()

    def search_scored(
        self, query: str, top_k: int, data_scope: list[str]
    ) -> list[tuple[SchemaHit, float]]:
        """按 data_scope 过滤,返回 [(SchemaHit, 置信度)],供模块 3/3.5。"""
        self._ensure_indexed()
        query_vec = self._embed_texts([query])[0]
        scored = []
        for tbl in self.catalog.tables_for_scope(data_scope):
            item = self._store.get(self.COLLECTION_TABLE, {}).get(tbl.name)
            if not item:
                continue
            score = max(0.0, _cosine(query_vec, item["embedding"]))
            if score > 0:
                scored.append(
                    (
                        SchemaHit(
                            table_name=tbl.name,
                            columns=[dict(c) for c in tbl.columns],
                            business_terms=[],
                        ),
                        score,
                    )
                )
        scored.sort(key=lambda x: -x[1])
        return scored[:top_k]
