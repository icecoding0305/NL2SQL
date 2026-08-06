"""LangGraph 生产用 SQLite checkpoint。

查询历史库只保存展示字段，不能恢复 LangGraph 的中断现场；本模块持久化完整图状态，
使服务重启后仍可继续审批或澄清。测试仍可显式注入 InMemorySaver。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("nl2sql_agent.state", name)
            for name in (
                "NL2SQLState",
                "SchemaHit",
                "IntentSlot",
                "QueryIntent",
                "QueryAssumption",
                "SemanticSubject",
                "SemanticOutput",
                "SemanticPredicate",
                "SemanticGraph",
                "ResolvedQuery",
                "BusinessClarificationOption",
                "BusinessClarification",
                "DecisionSource",
                "DecisionSummary",
                "FieldCandidate",
                "PlannedTable",
                "SchemaPlan",
                "QueryPlan",
                "JoinSpec",
                "FilterSpec",
                "MetricSpec",
                "OutputFieldSpec",
                "OutputGrain",
                "QuerySchemaColumn",
                "QuerySchemaTable",
                "QuerySchemaRelation",
                "QueryMSchema",
                "LogicalOperation",
                "LogicalPlan",
            )
        ]
    )


def create_sqlite_checkpointer(path: str | Path) -> SqliteSaver:
    """创建线程安全、跨进程可共享文件的 SQLite saver。"""
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return SqliteSaver(conn, serde=checkpoint_serializer())
