"""向量存储适配层:统一接口 + 内存/Postgres 实现。

通过 config/vector_store.yaml 显式选择后端(backend: memory / pgvector)。
"""

from nl2sql_agent.services.vector_store.base import VectorStoreAdapter
from nl2sql_agent.services.vector_store.memory import InMemoryVectorStore
from nl2sql_agent.services.vector_store.pg import PgVectorStore

__all__ = ["VectorStoreAdapter", "InMemoryVectorStore", "PgVectorStore"]
