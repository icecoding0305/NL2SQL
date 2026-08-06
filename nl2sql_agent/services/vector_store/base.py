"""向量存储统一接口(VectorStoreAdapter)。

所有实现(InMemory / PgVector)都实现此接口,通过配置显式选择后端。
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class VectorStoreAdapter(ABC):
    @abstractmethod
    def upsert(self, collection: str, id: str, text: str, metadata: dict) -> None:
        """写入一条向量(embed 由实现内部调用统一 embedding 函数)。"""

    @abstractmethod
    def search(
        self, collection: str, query: str, top_k: int, filters: dict
    ) -> list[dict]:
        """语义相似度检索,返回 [{"id","text","metadata","score"}, ...],score ∈ [0,1]。"""
