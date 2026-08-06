"""少样本示例库(模块 7 简单路径)。

预留了 add_example —— 反馈闭环(人工确认通过的案例回流)接入点,暂未接入图内。
"""

from __future__ import annotations

from pathlib import Path

from nl2sql_agent.services.config_loader import ConfigLoader


class FewShotStore:
    def __init__(self, loader: ConfigLoader):
        path: Path = loader.base_dir / "few_shot.yaml"
        data = loader.load("few_shot.yaml") if path.exists() else {}
        self.examples: list[dict] = list(data.get("examples", []))

    def retrieve(self, query: str, top_k: int = 2) -> list[dict]:
        scored = [
            (self._score(query, ex.get("user_query", "")), ex) for ex in self.examples
        ]
        scored.sort(key=lambda x: -x[0])
        return [ex for s, ex in scored[:top_k] if s > 0]

    def _score(self, a: str, b: str) -> int:
        return len(set(a) & set(b))

    def add_example(self, user_query: str, sql: str, used_tables: list[str], tags: list[str]) -> None:
        """反馈闭环写回接口(预留)。"""
        self.examples.append(
            {"user_query": user_query, "sql": sql, "used_tables": used_tables, "tags": tags}
        )
