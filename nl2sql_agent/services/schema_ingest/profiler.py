"""受预算约束的字段画像与规则式语义分类。

每张表最多执行一次 LIMIT 查询；统计基于样本而非全表扫描。敏感字段不计算值域，
样例只保留掩码，避免把原始数据写入 M-Schema 或发送给 LLM。
"""

from __future__ import annotations

from numbers import Number

from nl2sql_agent.services.executor import SQLExecutor
from nl2sql_agent.services.schema_ingest.mysql_fetcher import ColumnMeta, TableMeta


DATE_TYPES = {"date", "datetime", "timestamp", "time", "year"}
NUMBER_TYPES = {
    "tinyint", "smallint", "mediumint", "int", "integer", "bigint",
    "decimal", "numeric", "float", "double", "real",
}
TEXT_TYPES = {"char", "varchar", "text", "tinytext", "mediumtext", "longtext", "json"}


def mask_value(value: object, keep_head: int = 3, keep_tail: int = 4) -> str:
    s = str(value)
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return s[:keep_head] + "*" * (len(s) - keep_head - keep_tail) + s[-keep_tail:]


def _safe_identifier(name: str, dialect: str) -> str:
    if dialect.lower() in {"postgres", "postgresql"}:
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def profile_table(
    executor: SQLExecutor,
    table: TableMeta,
    *,
    sample_size: int = 100,
    example_limit: int = 3,
    dialect: str = "mysql",
) -> None:
    """原地填充 column.profile；失败时保留空画像，不影响结构提取。"""
    if not table.columns or sample_size <= 0:
        return
    columns = ", ".join(_safe_identifier(c.name, dialect) for c in table.columns)
    table_ref = _safe_identifier(table.table_name, dialect)
    if table.schema_name and dialect.lower() in {"postgres", "postgresql"}:
        table_ref = f"{_safe_identifier(table.schema_name, dialect)}.{table_ref}"
    sql = f"SELECT {columns} FROM {table_ref} LIMIT {int(sample_size)}"
    rows = executor.execute(sql, timeout_seconds=15)
    for column in table.columns:
        raw_values = [row.get(column.name) for row in rows]
        non_null = [value for value in raw_values if value is not None]
        profile: dict = {
            "source": "sample",
            "sample_size": len(rows),
            "non_null_count": len(non_null),
            "null_ratio": round((len(rows) - len(non_null)) / len(rows), 4) if rows else None,
            "approx_distinct": len({str(value) for value in non_null}),
        }
        if column.sensitive:
            profile["examples"] = [mask_value(value) for value in non_null[:example_limit]]
            profile["value_stats_suppressed"] = True
        else:
            profile["examples"] = [str(value)[:50] for value in non_null[:example_limit]]
            if non_null and all(isinstance(value, Number) for value in non_null):
                profile["min"] = min(non_null)
                profile["max"] = max(non_null)
                profile["avg"] = round(sum(non_null) / len(non_null), 6)
            elif non_null:
                lengths = [len(str(value)) for value in non_null]
                profile["min_length"] = min(lengths)
                profile["max_length"] = max(lengths)
        column.profile = profile


def classify_column(column: ColumnMeta, table: TableMeta, enum_max_distinct: int = 20) -> None:
    """规则优先分类；不确定项留给后续 LLM 描述阶段，不逐列调用模型。"""
    name = column.name.lower()
    col_type = column.type.lower()
    distinct = column.profile.get("approx_distinct")
    sample_size = column.profile.get("non_null_count", 0)

    if col_type in DATE_TYPES or any(token in name for token in ("date", "time", "year", "month", "日期", "时间")):
        column.category = "datetime"
        column.semantic_role = "dimension"
        if "year" in name:
            column.time_granularity = "year"
        elif "month" in name:
            column.time_granularity = "month"
        elif "date" in name or col_type == "date":
            column.time_granularity = "day"
        else:
            column.time_granularity = "second"
    elif column.primary_key or column.unique or any(
        name.endswith(suffix) for suffix in ("_id", "_no", "_code", "id", "编号", "编码")
    ):
        column.category = "code"
        column.semantic_role = "dimension"
    elif distinct is not None and sample_size and distinct <= enum_max_distinct:
        column.category = "enum"
        column.semantic_role = "dimension"
    elif col_type in NUMBER_TYPES:
        column.category = "numeric"
        column.semantic_role = "measure"
    elif col_type in TEXT_TYPES:
        column.category = "text"
        column.semantic_role = "dimension"
    else:
        column.category = "unknown"
        column.semantic_role = "dimension"


def enrich_table(
    executor: SQLExecutor,
    table: TableMeta,
    config: dict,
    dialect: str = "mysql",
) -> None:
    profiling = config.get("profiling", {})
    if profiling.get("enabled", True):
        try:
            profile_table(
                executor,
                table,
                sample_size=int(profiling.get("sample_size", 100)),
                example_limit=int(profiling.get("example_limit", 3)),
                dialect=dialect,
            )
        except Exception:  # noqa: BLE001 - 画像失败不阻断结构同步
            pass
    enum_max = int(config.get("classification", {}).get("enum_max_distinct", 20))
    for column in table.columns:
        classify_column(column, table, enum_max_distinct=enum_max)
