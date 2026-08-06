"""旧版数据库结构导入器。

新代码应使用 ``schema_ingest.diff_sync.sync``，由 effective M-Schema 投影 catalog。
本模块暂时保留给旧调用方兼容，不再作为推荐入口。
"""

from __future__ import annotations

from pathlib import Path

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.executor import SQLExecutor

# 按列名/注释自动标注敏感字段
_SENSITIVE_PATTERNS = (
    "身份证", "证件号", "证件号码", "证件", "手机", "电话", "mobile", "idnum", "idcard",
)

# 常见类型精简(保留原始类型字符串)
_TYPE_MAP = {
    "int": "int",
    "bigint": "int",
    "smallint": "int",
    "tinyint": "int",
}


def _norm_type(col_type: str) -> str:
    t = col_type.lower().split("(")[0]
    return _TYPE_MAP.get(t, t)


def _is_sensitive(name: str, comment: str) -> bool:
    low = f"{name} {comment}".lower()
    return any(p.lower() in low for p in _SENSITIVE_PATTERNS)


def fetch_tables(executor: SQLExecutor, database: str, schema: str = "dwd") -> list[dict]:
    """查询 INFORMATION_SCHEMA,返回按表组织的结构。"""
    rows = executor.execute(
        "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
        timeout_seconds=15,
        params=(database,),
    )
    col_rows = executor.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
        timeout_seconds=15,
        params=(database,),
    )
    cols_by_table: dict[str, list[dict]] = {}
    for c in col_rows:
        name = c["COLUMN_NAME"]
        comment = c.get("COLUMN_COMMENT") or ""
        col = {
            "name": name,
            "type": _norm_type(c["COLUMN_TYPE"]),
            "comment": comment,
        }
        if _is_sensitive(name, comment):
            col["sensitive"] = True
        cols_by_table.setdefault(c["TABLE_NAME"], []).append(col)

    tables = []
    for t in rows:
        tname = t["TABLE_NAME"]
        tables.append(
            {
                "name": tname,
                "comment": t.get("TABLE_COMMENT") or "",
                "business_line": schema,
                "shared": True,  # 共享事实表,行级按 PLATFORM_CODE 过滤
                "columns": cols_by_table.get(tname, []),
            }
        )
    return tables


def write_schema_catalog(tables: list[dict], base_dir: str | Path) -> None:
    """写入 schema_catalog.yaml(按命名空间分组)。"""
    import yaml

    grouped: dict[str, dict] = {}
    for t in tables:
        line = t["business_line"]
        grouped.setdefault(line, {"tables": []})["tables"].append(t)

    path = Path(base_dir) / "schema_catalog.yaml"
    path.write_text(
        yaml.safe_dump(grouped, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def refresh_catalog_from_db(executor: SQLExecutor, database: str, base_dir: str | Path, schema: str = "dwd") -> int:
    """旧版直写入口；仅为兼容保留，可能绕过 M-Schema 事实源。"""
    import warnings

    warnings.warn(
        "refresh_catalog_from_db 已弃用；请使用 schema_ingest.diff_sync.sync",
        DeprecationWarning,
        stacklevel=2,
    )
    tables = fetch_tables(executor, database, schema=schema)
    write_schema_catalog(tables, base_dir)
    return len(tables)


def reload_deps():
    """配置写入后重建 deps(让新的 schema_catalog 生效)。"""
    from nl2sql_agent.services.deps import build_deps

    return build_deps()
