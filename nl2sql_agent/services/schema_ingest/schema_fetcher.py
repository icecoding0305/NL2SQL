"""按数据库方言路由 Schema 提取器。"""

from __future__ import annotations

from nl2sql_agent.services.executor import SQLExecutor
from nl2sql_agent.services.schema_ingest.mysql_fetcher import TableMeta, fetch_information_schema
from nl2sql_agent.services.schema_ingest.postgres_fetcher import fetch_postgres_schema


def fetch_schema(
    executor: SQLExecutor,
    schema_name: str,
    dialect: str,
    sensitive_patterns: tuple[str, ...] | None = None,
) -> list[TableMeta]:
    normalized = dialect.lower()
    if normalized in {"postgres", "postgresql"}:
        return fetch_postgres_schema(executor, schema_name, sensitive_patterns)
    if normalized == "mysql":
        return fetch_information_schema(executor, schema_name, sensitive_patterns)
    raise ValueError(f"Schema 提取暂不支持方言:{dialect}")
