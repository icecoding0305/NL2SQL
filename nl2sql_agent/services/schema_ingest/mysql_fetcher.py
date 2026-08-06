"""拉取 MySQL information_schema，构建扩展版 M-Schema 原始元数据。

除表/列外同时提取主键、外键、唯一键、索引、默认值与可空性；列级自动标注
敏感字段，供后续画像脱敏。所有新增字段均有默认值，兼容旧调用方式。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nl2sql_agent.services.executor import SQLExecutor


@dataclass
class ColumnMeta:
    name: str
    type: str
    comment: str
    sensitive: bool = False
    raw_type: str = ""
    nullable: bool = True
    default: Any = None
    primary_key: bool = False
    unique: bool = False
    indexed: bool = False
    ordinal_position: int = 0
    category: str = ""
    semantic_role: str = ""
    time_granularity: str | None = None
    profile: dict = field(default_factory=dict)


@dataclass
class IndexMeta:
    name: str
    columns: list[str] = field(default_factory=list)
    unique: bool = False


@dataclass
class RelationMeta:
    source_table: str
    source_columns: list[str]
    target_table: str
    target_columns: list[str]
    target_schema: str | None = None
    constraint_name: str = ""
    relation_type: str = "foreign_key"


@dataclass
class TableMeta:
    table_name: str
    table_comment: str
    columns: list[ColumnMeta] = field(default_factory=list)
    row_count_estimate: int = 0
    schema_name: str = ""
    primary_keys: list[str] = field(default_factory=list)
    unique_keys: list[list[str]] = field(default_factory=list)
    indexes: list[IndexMeta] = field(default_factory=list)
    relations: list[RelationMeta] = field(default_factory=list)
    preliminary_description: str = ""
    description_confidence: float = 0.0

    @property
    def comment_coverage(self) -> float:
        if not self.columns:
            return 0.0
        covered = sum(1 for c in self.columns if c.comment and len(c.comment) > 0)
        return covered / len(self.columns)


def _norm_type(col_type: str) -> str:
    return col_type.lower().split("(")[0]


def _is_sensitive(name: str, comment: str, patterns: tuple[str, ...]) -> bool:
    low = f"{name} {comment}".lower()
    return any(p.lower() in low for p in patterns)


def fetch_information_schema(
    executor: SQLExecutor, schema_name: str, sensitive_patterns: tuple[str, ...] | None = None
) -> list[TableMeta]:
    """查询 information_schema.TABLES / COLUMNS,返回所有表结构。"""
    patterns = sensitive_patterns or (
        "身份证", "证件号", "证件号码", "证件", "手机", "电话", "mobile", "idnum", "idcard",
    )
    tables_rows = executor.execute(
        "SELECT TABLE_NAME, TABLE_COMMENT, TABLE_ROWS FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME",
        timeout_seconds=15,
        params=(schema_name,),
    )
    col_rows = executor.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, COLUMN_COMMENT, "
        "IS_NULLABLE, COLUMN_DEFAULT, COLUMN_KEY, EXTRA, ORDINAL_POSITION "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION",
        timeout_seconds=15,
        params=(schema_name,),
    )
    # 约束和索引查询失败时降级到 COLUMNS.COLUMN_KEY，不阻断基础结构提取。
    try:
        constraint_rows = executor.execute(
            "SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE, "
            "k.COLUMN_NAME, k.ORDINAL_POSITION, k.REFERENCED_TABLE_SCHEMA, "
            "k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE k "
            "LEFT JOIN information_schema.TABLE_CONSTRAINTS tc "
            "ON tc.CONSTRAINT_SCHEMA=k.CONSTRAINT_SCHEMA "
            "AND tc.TABLE_NAME=k.TABLE_NAME AND tc.CONSTRAINT_NAME=k.CONSTRAINT_NAME "
            "WHERE k.CONSTRAINT_SCHEMA=%s ORDER BY k.TABLE_NAME,k.CONSTRAINT_NAME,k.ORDINAL_POSITION",
            timeout_seconds=15,
            params=(schema_name,),
        )
    except Exception:  # noqa: BLE001 - 某些只读账号不可见约束元数据
        constraint_rows = []
    try:
        index_rows = executor.execute(
            "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, COLUMN_NAME, SEQ_IN_INDEX "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s "
            "ORDER BY TABLE_NAME,INDEX_NAME,SEQ_IN_INDEX",
            timeout_seconds=15,
            params=(schema_name,),
        )
    except Exception:  # noqa: BLE001
        index_rows = []

    constraints: dict[tuple[str, str], dict] = {}
    for row in constraint_rows:
        key = (row.get("TABLE_NAME", ""), row.get("CONSTRAINT_NAME", ""))
        item = constraints.setdefault(key, {"type": row.get("CONSTRAINT_TYPE") or "", "rows": []})
        item["rows"].append(row)
    indexes_by_table: dict[str, dict[str, IndexMeta]] = {}
    for row in index_rows:
        table_name = row.get("TABLE_NAME", "")
        index_name = row.get("INDEX_NAME", "")
        idx = indexes_by_table.setdefault(table_name, {}).setdefault(
            index_name,
            IndexMeta(name=index_name, unique=not bool(row.get("NON_UNIQUE", 1))),
        )
        if row.get("COLUMN_NAME"):
            idx.columns.append(row["COLUMN_NAME"])

    cols_by_table: dict[str, list[ColumnMeta]] = {}
    for c in col_rows:
        name = c["COLUMN_NAME"]
        comment = c.get("COLUMN_COMMENT") or ""
        cols_by_table.setdefault(c["TABLE_NAME"], []).append(
            ColumnMeta(
                name=name,
                type=_norm_type(c.get("DATA_TYPE") or ""),
                comment=comment,
                sensitive=_is_sensitive(name, comment, patterns),
                raw_type=c.get("COLUMN_TYPE") or c.get("DATA_TYPE") or "",
                nullable=str(c.get("IS_NULLABLE", "YES")).upper() == "YES",
                default=c.get("COLUMN_DEFAULT"),
                primary_key=str(c.get("COLUMN_KEY", "")).upper() == "PRI",
                unique=str(c.get("COLUMN_KEY", "")).upper() in {"PRI", "UNI"},
                indexed=bool(c.get("COLUMN_KEY")),
                ordinal_position=int(c.get("ORDINAL_POSITION") or 0),
            )
        )

    tables = []
    for t in tables_rows:
        tname = t["TABLE_NAME"]
        primary_keys: list[str] = []
        unique_keys: list[list[str]] = []
        relations: list[RelationMeta] = []
        for (table_name, constraint_name), item in constraints.items():
            if table_name != tname:
                continue
            rows = item["rows"]
            columns = [r.get("COLUMN_NAME") for r in rows if r.get("COLUMN_NAME")]
            ctype = str(item["type"]).upper()
            if ctype == "PRIMARY KEY":
                primary_keys = columns
            elif ctype == "UNIQUE":
                unique_keys.append(columns)
            if any(r.get("REFERENCED_TABLE_NAME") for r in rows):
                relations.append(
                    RelationMeta(
                        source_table=tname,
                        source_columns=columns,
                        target_schema=rows[0].get("REFERENCED_TABLE_SCHEMA"),
                        target_table=rows[0].get("REFERENCED_TABLE_NAME") or "",
                        target_columns=[
                            r.get("REFERENCED_COLUMN_NAME")
                            for r in rows if r.get("REFERENCED_COLUMN_NAME")
                        ],
                        constraint_name=constraint_name,
                    )
                )
        columns = cols_by_table.get(tname, [])
        if not primary_keys:
            primary_keys = [c.name for c in columns if c.primary_key]
        for c in columns:
            c.primary_key = c.name in primary_keys or c.primary_key
            c.unique = c.unique or any([c.name] == key for key in unique_keys)
            c.indexed = c.indexed or any(c.name in idx.columns for idx in indexes_by_table.get(tname, {}).values())
        tables.append(
            TableMeta(
                table_name=tname,
                table_comment=t.get("TABLE_COMMENT") or "",
                columns=columns,
                row_count_estimate=int(t.get("TABLE_ROWS") or 0),
                schema_name=schema_name,
                primary_keys=primary_keys,
                unique_keys=unique_keys,
                indexes=list(indexes_by_table.get(tname, {}).values()),
                relations=relations,
            )
        )
    return tables
