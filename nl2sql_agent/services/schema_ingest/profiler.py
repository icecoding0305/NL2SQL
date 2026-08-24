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
DEFAULT_EXCLUDED_TYPES = {
    "blob", "tinyblob", "mediumblob", "longblob", "binary", "varbinary",
    "longtext", "mediumtext", "json", "geometry",
}
DEFAULT_EXCLUDED_NAME_PATTERNS = {
    "password", "passwd", "pwd", "secret", "token", "private_key", "access_key",
}


def mask_value(value: object, keep_head: int = 3, keep_tail: int = 4) -> str:
    s = str(value)
    if len(s) <= keep_head + keep_tail:
        return "*" * len(s)
    return s[:keep_head] + "*" * (len(s) - keep_head - keep_tail) + s[-keep_tail:]


def _safe_identifier(name: str, dialect: str) -> str:
    if dialect.lower() in {"postgres", "postgresql"}:
        return '"' + name.replace('"', '""') + '"'
    return "`" + name.replace("`", "``") + "`"


def select_profile_columns(
    table: TableMeta,
    *,
    max_columns: int = 20,
    sensitive_mode: str = "skip",
    excluded_types: set[str] | None = None,
    excluded_name_patterns: set[str] | None = None,
) -> list[ColumnMeta]:
    """Select a bounded, safe set of columns for one table-level sample query."""
    excluded_types = {item.lower() for item in (excluded_types or DEFAULT_EXCLUDED_TYPES)}
    excluded_names = {
        item.lower() for item in (excluded_name_patterns or DEFAULT_EXCLUDED_NAME_PATTERNS)
    }
    eligible: list[ColumnMeta] = []
    for column in table.columns:
        normalized_type = (column.type or column.raw_type).lower().split("(", 1)[0]
        normalized_name = column.name.lower()
        if normalized_type in excluded_types:
            continue
        if any(pattern in normalized_name for pattern in excluded_names):
            continue
        if column.sensitive and sensitive_mode == "skip":
            continue
        eligible.append(column)

    def priority(column: ColumnMeta) -> tuple[int, int]:
        name = column.name.lower()
        normalized_type = (column.type or column.raw_type).lower().split("(", 1)[0]
        score = 0
        score += 80 if any(token in name for token in (
            "status", "state", "type", "category", "level", "flag", "code",
        )) else 0
        score += 60 if normalized_type in NUMBER_TYPES else 0
        score += 55 if normalized_type in DATE_TYPES else 0
        score += 45 if normalized_type in {"char", "varchar"} else 0
        score += 30 if column.indexed else 0
        score += 20 if column.primary_key or column.unique else 0
        return score, -column.ordinal_position

    return sorted(eligible, key=priority, reverse=True)[:max(0, int(max_columns))]


def profile_table(
    executor: SQLExecutor,
    table: TableMeta,
    *,
    sample_size: int = 100,
    example_limit: int = 3,
    dialect: str = "mysql",
    max_columns: int = 20,
    sensitive_mode: str = "skip",
    excluded_types: set[str] | None = None,
    excluded_name_patterns: set[str] | None = None,
) -> dict:
    """原地填充 column.profile；失败时保留空画像，不影响结构提取。"""
    if not table.columns or sample_size <= 0:
        return {
            "status": "skipped", "sampled_columns": 0, "sampled_rows": 0,
            "skipped_columns": len(table.columns),
        }
    selected_columns = select_profile_columns(
        table,
        max_columns=max_columns,
        sensitive_mode=sensitive_mode,
        excluded_types=excluded_types,
        excluded_name_patterns=excluded_name_patterns,
    )
    if not selected_columns:
        return {
            "status": "skipped", "sampled_columns": 0, "sampled_rows": 0,
            "skipped_columns": len(table.columns),
        }
    columns = ", ".join(_safe_identifier(c.name, dialect) for c in selected_columns)
    table_ref = _safe_identifier(table.table_name, dialect)
    if table.schema_name and dialect.lower() in {"postgres", "postgresql"}:
        table_ref = f"{_safe_identifier(table.schema_name, dialect)}.{table_ref}"
    sql = f"SELECT {columns} FROM {table_ref} LIMIT {int(sample_size)}"
    rows = executor.execute(sql, timeout_seconds=15)
    for column in selected_columns:
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
    return {
        "status": "sampled",
        "sampled_columns": len(selected_columns),
        "sampled_rows": len(rows),
        "skipped_columns": len(table.columns) - len(selected_columns),
    }


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
) -> dict:
    profiling = config.get("profiling", {})
    summary = {
        "status": "disabled", "sampled_columns": 0, "sampled_rows": 0,
        "skipped_columns": len(table.columns),
    }
    if profiling.get("enabled", True):
        try:
            summary = profile_table(
                executor,
                table,
                sample_size=int(profiling.get("sample_size", 100)),
                example_limit=int(profiling.get("example_limit", 3)),
                dialect=dialect,
                max_columns=int(profiling.get("max_columns_per_query", 20)),
                sensitive_mode=str(profiling.get("sensitive_mode", "skip")).lower(),
                excluded_types=set(profiling.get("exclude_types") or DEFAULT_EXCLUDED_TYPES),
                excluded_name_patterns=set(
                    profiling.get("exclude_name_patterns") or DEFAULT_EXCLUDED_NAME_PATTERNS
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 画像失败不阻断结构同步
            summary = {
                "status": "failed", "sampled_columns": 0, "sampled_rows": 0,
                "skipped_columns": len(table.columns), "error": str(exc),
            }
    enum_max = int(config.get("classification", {}).get("enum_max_distinct", 20))
    for column in table.columns:
        classify_column(column, table, enum_max_distinct=enum_max)
    return summary
