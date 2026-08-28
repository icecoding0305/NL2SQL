"""Repair corrupt M-Schema comments from authoritative database metadata.

Only empty or irreversibly mojibaked semantic text is replaced. Valid manual
overrides remain untouched. A new content-addressed snapshot and vector cache
are produced; business tables are queried only through information_schema.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.deps import (
    CONFIG_DIR,
    PROJECT_ROOT,
    build_executor_from_url,
    build_vector_store,
    load_env,
)
from nl2sql_agent.services.schema_catalog import SchemaCatalog
from nl2sql_agent.services.schema_ingest.mschema import write_mschema_artifacts
from nl2sql_agent.services.schema_ingest.schema_fetcher import fetch_schema
from nl2sql_agent.services.text_encoding import clean_semantic_text, is_likely_mojibake


def _needs_repair(value: str) -> bool:
    text = str(value or "")
    return not text.strip() or is_likely_mojibake(text)


def _clean_known_semantic_fields(mschema: dict) -> int:
    changed = 0
    if is_likely_mojibake(str(mschema.get("db_info") or "")):
        mschema["db_info"] = ""
        changed += 1
    for table in (mschema.get("tables") or {}).values():
        for key in ("comment", "preliminary_description"):
            value = str(table.get(key) or "")
            cleaned = clean_semantic_text(value)
            if cleaned != value:
                table[key] = cleaned
                changed += 1
        for field in (table.get("fields") or {}).values():
            value = str(field.get("comment") or "")
            cleaned = clean_semantic_text(value)
            if cleaned != value:
                field["comment"] = cleaned
                changed += 1
    return changed


def repair(mschema_path: Path, database_url: str) -> dict:
    current = json.loads(mschema_path.read_text(encoding="utf-8"))
    repaired = copy.deepcopy(current)
    cleaned_count = _clean_known_semantic_fields(repaired)

    dialect = urlsplit(database_url).scheme.lower()
    dialect = "postgres" if dialect.startswith("postgres") else dialect
    schema_name = str(repaired.get("schema") or urlsplit(database_url).path.strip("/"))
    executor = build_executor_from_url(database_url)
    source_tables = fetch_schema(executor, schema_name, dialect)

    restored_tables = 0
    restored_columns = 0
    for source in source_tables:
        target = (repaired.get("tables") or {}).get(source.table_name)
        if target is None:
            continue
        if source.table_comment and _needs_repair(target.get("comment", "")):
            target["comment"] = source.table_comment
            target["description_source"] = "origin"
            target["description_confidence"] = 1.0
            restored_tables += 1
        source_columns = {column.name: column for column in source.columns}
        for name, field in (target.get("fields") or {}).items():
            source_column = source_columns.get(name)
            if source_column is None or not source_column.comment:
                continue
            if _needs_repair(field.get("comment", "")):
                field["comment"] = source_column.comment
                field["description_source"] = "origin"
                field["description_confidence"] = 1.0
                restored_columns += 1

    manifest = write_mschema_artifacts(
        repaired,
        repaired,
        artifact_root=mschema_path.parent.parent,
        prompt_version="metadata-comment-repair-v1",
    )
    loader = ConfigLoader(CONFIG_DIR)
    catalog = SchemaCatalog(loader, m_schema_path=mschema_path)
    vector_store = build_vector_store(loader, catalog)
    vector_store.rebuild_index()
    return {
        "cleaned_corrupt_values": cleaned_count,
        "restored_table_comments": restored_tables,
        "restored_column_comments": restored_columns,
        "snapshot_id": manifest["snapshot_id"],
        "m_schema_path": str(mschema_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--m-schema", required=True)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    args = parser.parse_args()
    load_env()
    database_url = os.getenv(args.database_url_env, "")
    if not database_url:
        raise RuntimeError(f"环境变量 {args.database_url_env} 未配置")
    path = Path(args.m_schema).resolve()
    if PROJECT_ROOT not in path.parents:
        raise RuntimeError("M-Schema 必须位于当前项目目录内")
    print(json.dumps(repair(path, database_url), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
