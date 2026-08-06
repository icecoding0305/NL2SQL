"""PostgreSQL Schema 元数据提取器，输出与 MySQL 提取器相同的内部模型。"""

from __future__ import annotations

import re

from nl2sql_agent.services.executor import SQLExecutor
from nl2sql_agent.services.schema_ingest.mysql_fetcher import (
    ColumnMeta,
    IndexMeta,
    RelationMeta,
    TableMeta,
    _is_sensitive,
    _norm_type,
)


def fetch_postgres_schema(
    executor: SQLExecutor,
    schema_name: str,
    sensitive_patterns: tuple[str, ...] | None = None,
) -> list[TableMeta]:
    patterns = sensitive_patterns or (
        "身份证", "证件号", "手机", "电话", "mobile", "idnum", "idcard",
    )
    table_rows = executor.execute(
        "SELECT t.table_name AS \"TABLE_NAME\", "
        "COALESCE(obj_description(c.oid), '') AS \"TABLE_COMMENT\", "
        "COALESCE(c.reltuples, 0)::bigint AS \"TABLE_ROWS\" "
        "FROM information_schema.tables t "
        "JOIN pg_namespace n ON n.nspname=t.table_schema "
        "JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=t.table_name "
        "WHERE t.table_schema=%s AND t.table_type='BASE TABLE' ORDER BY t.table_name",
        timeout_seconds=15,
        params=(schema_name,),
    )
    column_rows = executor.execute(
        "SELECT cols.table_name AS \"TABLE_NAME\", cols.column_name AS \"COLUMN_NAME\", "
        "cols.data_type AS \"DATA_TYPE\", cols.udt_name AS \"COLUMN_TYPE\", "
        "COALESCE(col_description(pc.oid, cols.ordinal_position), '') AS \"COLUMN_COMMENT\", "
        "cols.is_nullable AS \"IS_NULLABLE\", cols.column_default AS \"COLUMN_DEFAULT\", "
        "cols.ordinal_position AS \"ORDINAL_POSITION\" "
        "FROM information_schema.columns cols "
        "JOIN pg_namespace pn ON pn.nspname=cols.table_schema "
        "JOIN pg_class pc ON pc.relnamespace=pn.oid AND pc.relname=cols.table_name "
        "WHERE cols.table_schema=%s ORDER BY cols.table_name,cols.ordinal_position",
        timeout_seconds=15,
        params=(schema_name,),
    )
    constraint_rows = executor.execute(
        "SELECT tc.table_name AS \"TABLE_NAME\", tc.constraint_name AS \"CONSTRAINT_NAME\", "
        "tc.constraint_type AS \"CONSTRAINT_TYPE\", kcu.column_name AS \"COLUMN_NAME\", "
        "kcu.ordinal_position AS \"ORDINAL_POSITION\", "
        "kcu2.table_schema AS \"REFERENCED_TABLE_SCHEMA\", "
        "kcu2.table_name AS \"REFERENCED_TABLE_NAME\", "
        "kcu2.column_name AS \"REFERENCED_COLUMN_NAME\" "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "ON kcu.constraint_schema=tc.constraint_schema AND kcu.constraint_name=tc.constraint_name "
        "LEFT JOIN information_schema.referential_constraints rc "
        "ON rc.constraint_schema=tc.constraint_schema AND rc.constraint_name=tc.constraint_name "
        "LEFT JOIN information_schema.key_column_usage kcu2 "
        "ON kcu2.constraint_schema=rc.unique_constraint_schema "
        "AND kcu2.constraint_name=rc.unique_constraint_name "
        "AND kcu2.ordinal_position=kcu.position_in_unique_constraint "
        "WHERE tc.table_schema=%s ORDER BY tc.table_name,tc.constraint_name,kcu.ordinal_position",
        timeout_seconds=15,
        params=(schema_name,),
    )
    index_rows = executor.execute(
        "SELECT tablename AS \"TABLE_NAME\", indexname AS \"INDEX_NAME\", "
        "indexdef AS \"INDEX_DEF\" FROM pg_indexes WHERE schemaname=%s",
        timeout_seconds=15,
        params=(schema_name,),
    )

    constraints: dict[tuple[str, str], dict] = {}
    for row in constraint_rows:
        key = (row.get("TABLE_NAME", ""), row.get("CONSTRAINT_NAME", ""))
        constraints.setdefault(key, {"type": row.get("CONSTRAINT_TYPE") or "", "rows": []})["rows"].append(row)
    indexes: dict[str, list[IndexMeta]] = {}
    for row in index_rows:
        definition = row.get("INDEX_DEF") or ""
        match = re.search(r"\((.*?)\)", definition)
        columns = [part.strip().strip('"') for part in match.group(1).split(",")] if match else []
        indexes.setdefault(row.get("TABLE_NAME", ""), []).append(
            IndexMeta(
                name=row.get("INDEX_NAME", ""),
                columns=columns,
                unique="CREATE UNIQUE INDEX" in definition.upper(),
            )
        )
    columns_by_table: dict[str, list[ColumnMeta]] = {}
    for row in column_rows:
        name = row.get("COLUMN_NAME", "")
        comment = row.get("COLUMN_COMMENT") or ""
        columns_by_table.setdefault(row.get("TABLE_NAME", ""), []).append(
            ColumnMeta(
                name=name,
                type=_norm_type(row.get("DATA_TYPE") or ""),
                raw_type=row.get("COLUMN_TYPE") or row.get("DATA_TYPE") or "",
                comment=comment,
                sensitive=_is_sensitive(name, comment, patterns),
                nullable=str(row.get("IS_NULLABLE", "YES")).upper() == "YES",
                default=row.get("COLUMN_DEFAULT"),
                ordinal_position=int(row.get("ORDINAL_POSITION") or 0),
            )
        )

    result: list[TableMeta] = []
    for row in table_rows:
        table_name = row.get("TABLE_NAME", "")
        primary_keys: list[str] = []
        unique_keys: list[list[str]] = []
        relations: list[RelationMeta] = []
        for (owner, constraint_name), item in constraints.items():
            if owner != table_name:
                continue
            rows = item["rows"]
            source_columns = [r.get("COLUMN_NAME") for r in rows if r.get("COLUMN_NAME")]
            kind = str(item["type"]).upper()
            if kind == "PRIMARY KEY":
                primary_keys = source_columns
            elif kind == "UNIQUE":
                unique_keys.append(source_columns)
            elif kind == "FOREIGN KEY" and rows[0].get("REFERENCED_TABLE_NAME"):
                relations.append(
                    RelationMeta(
                        source_table=table_name,
                        source_columns=source_columns,
                        target_schema=rows[0].get("REFERENCED_TABLE_SCHEMA"),
                        target_table=rows[0].get("REFERENCED_TABLE_NAME") or "",
                        target_columns=[
                            r.get("REFERENCED_COLUMN_NAME")
                            for r in rows if r.get("REFERENCED_COLUMN_NAME")
                        ],
                        constraint_name=constraint_name,
                    )
                )
        columns = columns_by_table.get(table_name, [])
        for column in columns:
            column.primary_key = column.name in primary_keys
            column.unique = column.primary_key or any([column.name] == key for key in unique_keys)
            column.indexed = any(column.name in index.columns for index in indexes.get(table_name, []))
        result.append(
            TableMeta(
                table_name=table_name,
                table_comment=row.get("TABLE_COMMENT") or "",
                columns=columns,
                row_count_estimate=int(row.get("TABLE_ROWS") or 0),
                schema_name=schema_name,
                primary_keys=primary_keys,
                unique_keys=unique_keys,
                indexes=indexes.get(table_name, []),
                relations=relations,
            )
        )
    return result
