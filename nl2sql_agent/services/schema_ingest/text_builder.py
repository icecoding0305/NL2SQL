"""从 effective M-Schema 构建表级、字段级和关系级向量文档。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nl2sql_agent.services.vector_store.base import VectorStoreAdapter

COLLECTION_TABLE = "schema_table"
COLLECTION_COLUMN = "schema_column"
COLLECTION_RELATION = "schema_relation"


def load_mschema_vector_source(metadata: dict) -> tuple[dict, dict] | None:
    """根据 catalog 来源元数据加载 effective M-Schema 与同目录 manifest。"""
    source_path = metadata.get("m_schema_path")
    if metadata.get("source") != "effective-m-schema" or not source_path:
        return None
    path = Path(source_path)
    manifest_path = path.with_name("manifest.json")
    if not path.exists() or not manifest_path.exists():
        return None
    try:
        mschema = json.loads(path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if metadata.get("semantic_hash") != manifest.get("semantic_hash"):
        return None
    return mschema, manifest


def _table_semantic_hash(table: dict, relations: list[dict]) -> str:
    payload = json.dumps(
        {"table": table, "relations": relations},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_table_level_text(
    table_name: str,
    table: dict,
    relations: list[dict],
    key_columns: int = 8,
) -> str:
    all_fields = list(table.get("fields", {}).items())
    foreign_key_columns = {
        column
        for relation in relations
        for column in relation.get("source_columns", [])
    }

    def field_priority(item: tuple[str, dict]) -> tuple[int, int]:
        name, field = item
        score = 0
        score += 100 if field.get("primary_key") else 0
        score += 90 if name in foreign_key_columns else 0
        score += 70 if field.get("dim_or_meas") == "measure" else 0
        score += 60 if field.get("category") == "datetime" else 0
        score += 30 if field.get("unique") else 0
        score += 20 if field.get("indexed") else 0
        score += 10 if field.get("comment") else 0
        return score, -all_fields.index(item)

    fields = sorted(all_fields, key=field_priority, reverse=True)[:key_columns]
    field_text = ", ".join(
        f"{name}[{field.get('dim_or_meas') or 'unknown'}/{field.get('category') or 'unknown'}]"
        f"({field.get('comment') or ''})"
        for name, field in fields
    )
    relation_text = "; ".join(
        f"{','.join(relation.get('source_columns', []))} -> "
        f"{relation.get('target_table', '')}.{','.join(relation.get('target_columns', []))}"
        for relation in relations
    ) or "无显式外键"
    return (
        f"表名:{table_name}\n说明:{table.get('comment') or ''}\n"
        f"数据量估计:{table.get('row_count_estimate')}\n"
        f"主键:{','.join(table.get('primary_keys', [])) or '无'}\n"
        f"外键关系:{relation_text}\n核心字段:{field_text}\n"
        f"全部字段:{','.join(name for name, _ in all_fields)}"
    )


def build_column_level_text(table_name: str, fields: list[tuple[str, dict]]) -> str:
    lines = [
        f"{name}({field.get('raw_type') or field.get('type') or ''}) "
        f"角色:{field.get('dim_or_meas') or 'unknown'} "
        f"类别:{field.get('category') or 'unknown'} "
        f"PK:{bool(field.get('primary_key'))} UNIQUE:{bool(field.get('unique'))} "
        f"NULLABLE:{bool(field.get('nullable', True))} "
        f"说明:{field.get('comment') or ''} 样例:{field.get('examples') or []}"
        for name, field in fields
    ]
    return f"表:{table_name}\n" + "\n".join(lines)


def write_mschema_table_embeddings(
    store: VectorStoreAdapter,
    effective_mschema: dict,
    table_name: str,
    manifest: dict,
    columns_per_chunk: int = 15,
) -> None:
    """仅以 effective M-Schema 为输入，重建一张表的全部向量文档。"""
    table = effective_mschema.get("tables", {}).get(table_name)
    if table is None:
        raise KeyError(f"M-Schema 中不存在表: {table_name}")

    relations = [
        relation
        for relation in effective_mschema.get("relations", [])
        if relation.get("source_table") == table_name
    ]
    remove_table_from_store(store, table_name, columns_per_chunk)
    common_metadata = {
        "datasource": effective_mschema.get("db_id", ""),
        "business_line": effective_mschema.get("namespace", ""),
        "table_name": table_name,
        "source": "effective-m-schema",
        "m_schema_format_version": effective_mschema.get("format_version", ""),
        "snapshot_id": manifest.get("snapshot_id", ""),
        "semantic_hash": manifest.get("semantic_hash", ""),
        "table_semantic_hash": _table_semantic_hash(table, relations),
    }
    store.upsert(
        COLLECTION_TABLE,
        table_name,
        build_table_level_text(table_name, table, relations),
        dict(common_metadata),
    )

    groups: dict[tuple[str, str], list[tuple[str, dict]]] = {}
    for column_name, field in table.get("fields", {}).items():
        key = (field.get("dim_or_meas") or "unknown", field.get("category") or "unknown")
        groups.setdefault(key, []).append((column_name, field))
    chunk_no = 0
    for (role, category), fields in sorted(groups.items()):
        for index in range(0, len(fields), columns_per_chunk):
            chunk = fields[index:index + columns_per_chunk]
            metadata = {
                **common_metadata,
                "column_names": [name for name, _ in chunk],
                "semantic_role": role,
                "category": category,
            }
            store.upsert(
                COLLECTION_COLUMN,
                f"{table_name}#col#{chunk_no}",
                build_column_level_text(table_name, chunk),
                metadata,
            )
            chunk_no += 1

    for relation_no, relation in enumerate(relations):
        relation_text = (
            f"关系:{relation.get('source_table')}.{','.join(relation.get('source_columns', []))} "
            f"关联 {relation.get('target_table')}.{','.join(relation.get('target_columns', []))} "
            f"类型:{relation.get('relation_type') or 'foreign_key'}"
        )
        store.upsert(
            COLLECTION_RELATION,
            f"{table_name}#rel#{relation_no}",
            relation_text,
            {**common_metadata, "target_table": relation.get("target_table", "")},
        )


def remove_table_from_store(
    store: VectorStoreAdapter, table_name: str, columns_per_chunk: int = 15
) -> None:
    """删除一张表的表级、字段级和关系级向量条目。"""
    if hasattr(store, "remove_table"):
        store.remove_table(table_name, columns_per_chunk)
