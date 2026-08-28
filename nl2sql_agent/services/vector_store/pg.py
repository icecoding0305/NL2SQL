"""pgvector 实现:真 embedding 写入,余弦距离检索。

实现 VectorStoreAdapter;schema 层 search_scored 基于 catalog 表列表 + SQL 检索。
"""

from __future__ import annotations

import json

from nl2sql_agent.services.embedding.router import EmbedFn, get_embedding_function
from nl2sql_agent.services.vector_store.base import VectorStoreAdapter
from nl2sql_agent.state import SchemaHit


class PgVectorStore(VectorStoreAdapter):
    COLLECTION_TABLE = "schema_table"    # 表级
    COLLECTION_COLUMN = "schema_column"  # 字段级
    COLLECTION_RELATION = "schema_relation"  # 关系级

    def __init__(self, url: str, catalog, embed: EmbedFn | None = None):
        self.url = url
        self.catalog = catalog
        self._embed = embed or get_embedding_function()

    def _connect(self):
        import psycopg  # 延迟导入

        return psycopg.connect(self.url)

    # ---------------- VectorStoreAdapter ----------------

    def upsert(self, collection: str, id: str, text: str, metadata: dict) -> None:
        vec = self._embed([text])[0]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO schema_embeddings (collection, id, text, metadata, embedding)
                VALUES (%s, %s, %s, %s, %s::vector)
                ON CONFLICT (collection, id) DO UPDATE
                  SET text = EXCLUDED.text, metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding
                """,
                (collection, id, text, json.dumps(metadata, ensure_ascii=False), vec),
            )
            conn.commit()

    def search(self, collection: str, query: str, top_k: int, filters: dict) -> list[dict]:
        vec = self._embed([query])[0]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, text, metadata,
                       1 - (embedding <=> %s::vector) / 2 AS score
                FROM schema_embeddings
                WHERE collection = %s
                  AND metadata::jsonb->>'business_line' = %s
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vec, collection, filters.get("business_line", ""), vec, top_k),
            )
            rows = cur.fetchall()
        return [
            {"id": r[0], "text": r[1], "metadata": json.loads(r[2]) if isinstance(r[2], str) else r[2], "score": max(0.0, float(r[3]))}
            for r in rows
        ]

    # ---------------- 全量入库 ----------------

    def ensure_table(self) -> None:
        """建表(维度来自模型配置)。"""
        from nl2sql_agent.services.embedding.router import load_model_config

        dim = load_model_config().get("embedding", {}).get("dimension", 384)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"CREATE TABLE IF NOT EXISTS schema_embeddings ("
                        f" collection TEXT, id TEXT, text TEXT, metadata TEXT,"
                        f" embedding vector({int(dim)}),"
                        f" PRIMARY KEY (collection, id))")
            conn.commit()

    def rebuild_index(self) -> None:
        """全量重建；优先从 catalog 指向的 effective M-Schema 恢复。"""
        self.ensure_table()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM schema_embeddings WHERE collection = ANY(%s)",
                ([self.COLLECTION_TABLE, self.COLLECTION_COLUMN, self.COLLECTION_RELATION],),
            )
            conn.commit()
        from nl2sql_agent.services.schema_ingest.text_builder import (
            load_mschema_vector_source,
            write_mschema_table_embeddings,
        )

        source = load_mschema_vector_source(getattr(self.catalog, "metadata", {}))
        if source is not None:
            mschema, manifest = source
            for table_name, table in mschema.get("tables", {}).items():
                if table.get("retrieval_eligible", True):
                    write_mschema_table_embeddings(self, mschema, table_name, manifest)
            return
        for tbl in self._all_tables():
            doc = " ".join(
                [tbl.name, tbl.comment]
                + [c["name"] + " " + c.get("comment", "") for c in tbl.columns]
            )
            self.upsert(
                self.COLLECTION_TABLE,
                tbl.name,
                doc,
                {"table_name": tbl.name, "business_line": tbl.business_line},
            )

    def _all_tables(self):
        out = list(getattr(self.catalog, "_shared_tables", []))
        for tables in self.catalog._tables_by_line.values():  # noqa: SLF001
            out.extend(tables)
        return out

    def remove_table(self, table_name: str, columns_per_chunk: int = 1) -> None:
        """删除一张表的向量条目(表级 + 字段级 chunk)。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM schema_embeddings WHERE collection = %s AND id = %s",
                (self.COLLECTION_TABLE, table_name),
            )
            cur.execute(
                "DELETE FROM schema_embeddings WHERE collection = %s AND id LIKE %s",
                (self.COLLECTION_COLUMN, f"{table_name}#col#%"),
            )
            cur.execute(
                "DELETE FROM schema_embeddings WHERE collection = %s AND id LIKE %s",
                (self.COLLECTION_RELATION, f"{table_name}#rel#%"),
            )
            conn.commit()

    # ---------------- schema 层(模块 3 用) ----------------

    def search_scored(
        self, query: str, top_k: int, data_scope: list[str]
    ) -> list[tuple[SchemaHit, float]]:
        tables = self.catalog.tables_for_scope(data_scope)
        names = [t.name for t in tables]
        if not names:
            return []
        vec = self._embed([query])[0]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, 1 - (embedding <=> %s::vector) / 2 AS score
                FROM schema_embeddings
                WHERE collection = %s AND id = ANY(%s)
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vec, self.COLLECTION_TABLE, names, vec, top_k),
            )
            rows = {r[0]: max(0.0, float(r[1])) for r in cur.fetchall()}
        scored = []
        for tbl in tables:
            if tbl.name in rows:
                scored.append(
                    (
                        SchemaHit(
                            table_name=tbl.name,
                            columns=[dict(c) for c in tbl.columns],
                            business_terms=[],
                        ),
                        rows[tbl.name],
                    )
                )
        scored.sort(key=lambda x: -x[1])
        return scored
