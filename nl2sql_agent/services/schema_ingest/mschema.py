"""扩展版 Enterprise M-Schema 构建与版本化落盘。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from nl2sql_agent.services.schema_ingest.mysql_fetcher import TableMeta

FORMAT_VERSION = "1.0"
CATALOG_PROJECTION_VERSION = "1.0"


def hydrate_enrichment(tables: list[TableMeta], mschema_path: str | Path) -> None:
    """增量同步时从上一版本恢复未变化表的画像和分类。"""
    path = Path(mschema_path)
    if not path.exists():
        return
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    old_tables = previous.get("tables", {})
    for table in tables:
        old_table = old_tables.get(table.table_name, {})
        table.preliminary_description = old_table.get("preliminary_description", "")
        table.description_confidence = float(old_table.get("description_confidence") or 0.0)
        old_fields = old_table.get("fields", {})
        for column in table.columns:
            old = old_fields.get(column.name, {})
            column.profile = dict(old.get("profile") or {})
            column.category = old.get("category") or ""
            column.semantic_role = old.get("dim_or_meas") or ""
            column.time_granularity = old.get("time_granularity")


def _json_default(value: Any):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _comment_source(raw_comment: str, effective_comment: str) -> str:
    if raw_comment:
        return "origin"
    if effective_comment:
        return "approved_override"
    return "missing"


def build_mschema(
    tables: list[TableMeta],
    *,
    datasource: str,
    schema_name: str,
    namespace: str,
    raw_tables: list[TableMeta] | None = None,
    database_context: str = "",
) -> dict:
    raw_by_name = {table.table_name: table for table in (raw_tables or tables)}
    table_map: dict[str, dict] = {}
    foreign_keys: list[list[str | None]] = []
    relations: list[dict] = []
    for table in tables:
        raw_table = raw_by_name.get(table.table_name, table)
        raw_columns = {column.name: column for column in raw_table.columns}
        fields: dict[str, dict] = {}
        for column in table.columns:
            raw_column = raw_columns.get(column.name, column)
            profile = dict(column.profile or {})
            source = _comment_source(raw_column.comment, column.comment)
            fields[column.name] = {
                "type": column.type,
                "raw_type": column.raw_type or column.type,
                "primary_key": column.primary_key,
                "nullable": column.nullable,
                "default": column.default,
                "unique": column.unique,
                "indexed": column.indexed,
                "comment": column.comment,
                "description_source": source,
                "description_confidence": 1.0 if source in {"origin", "approved_override"} else 0.0,
                "sensitive": column.sensitive,
                "category": column.category,
                "dim_or_meas": column.semantic_role,
                "time_granularity": column.time_granularity,
                "examples": profile.get("examples", []),
                "profile": profile,
            }
        table_source = _comment_source(raw_table.table_comment, table.table_comment)
        table_map[table.table_name] = {
            "comment": table.table_comment,
            "description_source": table_source,
            "row_count_estimate": table.row_count_estimate,
            "primary_keys": table.primary_keys,
            "unique_keys": table.unique_keys,
            "indexes": [
                {"name": index.name, "columns": index.columns, "unique": index.unique}
                for index in table.indexes
            ],
            "preliminary_description": table.preliminary_description,
            "description_confidence": (
                1.0 if table_source in {"origin", "approved_override"}
                else table.description_confidence
            ),
            "fields": fields,
        }
        for relation in table.relations:
            relation_dict = {
                "source_table": relation.source_table,
                "source_columns": relation.source_columns,
                "target_schema": relation.target_schema,
                "target_table": relation.target_table,
                "target_columns": relation.target_columns,
                "constraint_name": relation.constraint_name,
                "relation_type": relation.relation_type,
            }
            relations.append(relation_dict)
            for source, target in zip(relation.source_columns, relation.target_columns):
                foreign_keys.append([
                    relation.source_table, source, relation.target_schema,
                    relation.target_table, target,
                ])
    return {
        "format_version": FORMAT_VERSION,
        "db_id": datasource,
        "schema": schema_name,
        "namespace": namespace,
        "db_info": database_context,
        "tables": table_map,
        "foreign_keys": foreign_keys,
        "relations": relations,
    }


def _canonical_hash(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=_json_default)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_schema_catalog_projection(effective_mschema: dict, manifest: dict) -> dict:
    """从审核生效的 M-Schema 构造运行时精简视图。"""
    namespace = str(effective_mschema.get("namespace") or "default")
    tables: list[dict] = []
    for table_name, table in effective_mschema.get("tables", {}).items():
        columns: list[dict] = []
        for column_name, field in table.get("fields", {}).items():
            column = {
                "name": column_name,
                "type": field.get("type", ""),
                "comment": field.get("comment", ""),
                "raw_type": field.get("raw_type") or field.get("type", ""),
                "nullable": bool(field.get("nullable", True)),
                "primary_key": bool(field.get("primary_key", False)),
                "unique": bool(field.get("unique", False)),
                "indexed": bool(field.get("indexed", False)),
                "category": field.get("category", ""),
                "semantic_role": field.get("dim_or_meas", ""),
            }
            if field.get("time_granularity"):
                column["time_granularity"] = field["time_granularity"]
            if field.get("sensitive"):
                column["sensitive"] = True
            columns.append(column)
        tables.append({
            "name": table_name,
            "comment": table.get("comment", ""),
            "business_line": namespace,
            "shared": bool(table.get("shared", False)),
            "columns": columns,
        })

    return {
        "_meta": {
            "generated": True,
            "projection_version": CATALOG_PROJECTION_VERSION,
            "source": "effective-m-schema",
            "datasource": effective_mschema.get("db_id", ""),
            "m_schema_format_version": effective_mschema.get("format_version", ""),
            "snapshot_id": manifest.get("snapshot_id", ""),
            "semantic_hash": manifest.get("semantic_hash", ""),
            "generated_at": manifest.get("generated_at", ""),
        },
        namespace: {"tables": tables},
    }


def write_schema_catalog_projection(
    effective_mschema: dict,
    manifest: dict,
    *,
    catalog_dir: str | Path,
    m_schema_path: str | Path | None = None,
) -> Path:
    """将 M-Schema 投影原子写入 schema_catalog.yaml。"""
    import yaml

    projection = build_schema_catalog_projection(effective_mschema, manifest)
    if m_schema_path is not None:
        projection["_meta"]["m_schema_path"] = os.path.relpath(
            Path(m_schema_path).resolve(),
            Path(catalog_dir).resolve(),
        )
    path = Path(catalog_dir) / "schema_catalog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".yaml.tmp")
    temporary_path.write_text(
        yaml.safe_dump(
            projection,
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    temporary_path.replace(path)
    return path


def write_mschema_artifacts(
    raw_mschema: dict,
    effective_mschema: dict,
    *,
    artifact_root: str | Path,
    prompt_version: str = "enterprise-xiyan-v1",
) -> dict:
    """写入固定 latest 文件和内容寻址快照，返回 manifest。"""
    root = Path(artifact_root) / str(effective_mschema["db_id"])
    root.mkdir(parents=True, exist_ok=True)
    structure_view = {
        table: {
            "primary_keys": info.get("primary_keys"),
            "unique_keys": info.get("unique_keys"),
            "indexes": info.get("indexes"),
            "fields": {
                name: {
                    "type": field.get("type"),
                    "nullable": field.get("nullable"),
                    "default": field.get("default"),
                }
                for name, field in info.get("fields", {}).items()
            },
        }
        for table, info in raw_mschema.get("tables", {}).items()
    }
    structure_hash = _canonical_hash({"tables": structure_view, "relations": raw_mschema.get("relations", [])})
    semantic_hash = _canonical_hash(effective_mschema)
    snapshot_id = semantic_hash[:16]
    manifest = {
        "format_version": FORMAT_VERSION,
        "snapshot_id": snapshot_id,
        "structure_hash": structure_hash,
        "semantic_hash": semantic_hash,
        "prompt_version": prompt_version,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "table_count": len(effective_mschema.get("tables", {})),
        "column_count": sum(
            len(table.get("fields", {}))
            for table in effective_mschema.get("tables", {}).values()
        ),
    }
    snapshot_dir = root / "snapshots" / snapshot_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "raw-m-schema.json": raw_mschema,
        "effective-m-schema.json": effective_mschema,
        "manifest.json": manifest,
    }
    for name, payload in payloads.items():
        (snapshot_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
    # 固定路径供运行时和外部工具读取；m-schema.json 始终指向审核生效版本。
    (root / "m-schema.json").write_text(
        json.dumps(effective_mschema, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest
