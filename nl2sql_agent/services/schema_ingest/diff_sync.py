"""表结构入库编排:全量/增量同步、质量检查、审核队列、覆盖层、删表清理。

流程(每张表):
1. 用 override 补全注释后判断质量
2. 达标 → 标记为可入库，待 effective M-Schema 落盘后构建向量
3. 不达标 → LLM 生成候选注释草稿(样例值已脱敏)→ 进审核队列(不入向量库)
4. 增量模式:structure_hash 变化或该表有覆盖层才处理;被删的表从向量库清理
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from nl2sql_agent.services.schema_ingest.comment_generator import (
    build_review_entries,
    fetch_masked_sample_values,
    generate_database_context,
    generate_comment_draft,
    has_sufficient_comments,
)
from nl2sql_agent.services.schema_ingest.mysql_fetcher import TableMeta
from nl2sql_agent.services.schema_ingest.mschema import (
    build_mschema,
    hydrate_enrichment,
    write_mschema_artifacts,
)
from nl2sql_agent.services.schema_ingest.profiler import classify_column, enrich_table
from nl2sql_agent.services.schema_ingest.schema_fetcher import fetch_schema
from nl2sql_agent.services.schema_ingest.review_queue import ReviewStore
from nl2sql_agent.services.schema_ingest.text_builder import (
    remove_table_from_store,
    write_mschema_table_embeddings,
)


@dataclass
class SyncReport:
    ingested: int = 0      # 直接入库
    queued: int = 0        # 进审核队列
    skipped: int = 0       # 增量未变化
    removed: int = 0       # 删除的幽灵表
    errors: list[str] = field(default_factory=list)
    mschema_path: str = ""
    snapshot_id: str = ""


def compute_structure_hash(table: TableMeta) -> str:
    payload = json.dumps(
        [
            table.table_name,
            table.table_comment,
            table.primary_keys,
            table.unique_keys,
            [(i.name, i.columns, i.unique) for i in table.indexes],
            [
                (r.source_columns, r.target_schema, r.target_table, r.target_columns)
                for r in table.relations
            ],
            [
                (
                    c.name, c.type, c.raw_type, c.comment, c.sensitive,
                    c.nullable, c.default, c.primary_key, c.unique, c.indexed,
                )
                for c in table.columns
            ],
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_override(table: TableMeta, override: dict) -> TableMeta:
    """用覆盖层补全注释(原生为空时用 override 的最终注释)。"""
    cols = []
    for c in table.columns:
        final = override.get((table.table_name, c.name))
        cols.append(replace(c, comment=final or c.comment, profile=dict(c.profile)))
    table_comment = override.get((table.table_name, None)) or table.table_comment
    return replace(table, table_comment=table_comment, columns=cols)


def process_table(
    table: TableMeta,
    datasource: str,
    business_line: str,
    deps,
    review_store: ReviewStore,
    config: dict,
    override: dict,
    llm=None,
    database_context: str = "",
) -> str:
    """单表质量处理:达标则等待 M-Schema 入库，否则生成草稿进入审核队列。

    返回 'ingested' / 'queued'。
    """
    effective = apply_override(table, override)
    if has_sufficient_comments(effective, config):
        return "ingested"

    # 不达标:LLM 生成候选注释(样例脱敏)→ 进审核队列,人工 approve 后写入覆盖层
    try:
        samples = fetch_masked_sample_values(
            deps.executor,
            table,
            limit=int(config.get("sample_limit", 3)),
            dialect=deps.config.dialect,
        )
        if llm is None:
            from nl2sql_agent.services.llm import get_model_for_node

            llm = get_model_for_node("schema_comment_generation")
        draft = generate_comment_draft(effective, samples, llm, database_context, prompts=deps.prompts)
    except Exception as e:  # noqa: BLE001
        draft = {"table_comment": "", "columns": {}}
        __import__("logging").getLogger(__name__).warning(
            "注释生成失败(%s): %s", table.table_name, e
        )

    entries = build_review_entries(effective, draft, config)
    if not entries:
        review_store.add_review(
            datasource, table.table_name, None,
            "(LLM 未生成草稿,请人工补充表/字段注释)",
            confidence=float(draft.get("confidence", 0.0)),
            validation_errors=draft.get("validation_errors", []),
            structure_hash=compute_structure_hash(table),
        )
    else:
        for column_name, comment in entries:
            column = next((c for c in effective.columns if c.name == column_name), None)
            review_store.add_review(
                datasource,
                table.table_name,
                column_name,
                comment,
                confidence=float(draft.get("confidence", 0.0)),
                evidence={
                    "category": getattr(column, "category", None),
                    "semantic_role": getattr(column, "semantic_role", None),
                    "profile": getattr(column, "profile", {}) if column else {},
                    "preliminary_description": draft.get("preliminary_description", ""),
                },
                validation_errors=draft.get("validation_errors", []),
                structure_hash=compute_structure_hash(table),
            )
    return "queued"


def sync(
    datasource: str,
    schema_name: str,
    deps,
    config: dict,
    review_store: ReviewStore,
    mode: str = "full",
    business_line: str = "risk_mart",
    catalog_dir=None,
) -> SyncReport:
    """全量/增量同步。datasource 用于元数据分片;schema_name 是 MySQL 库名。

    catalog_dir:测试时用于隔离 M-Schema 产物目录；生产默认写入 data/schema。
    """
    executor = deps.executor
    patterns = tuple(config.get("sensitive_patterns", []))
    tables = fetch_schema(
        executor,
        schema_name,
        deps.config.dialect,
        patterns or None,
    )
    snapshot = review_store.load_snapshot(datasource)
    override = review_store.overrides(datasource)
    override_tables = {t for (t, _) in override}
    report = SyncReport()

    # M-Schema 输出位置:测试跟随 catalog_dir；生产默认 data/schema。
    if catalog_dir is not None:
        artifact_root = Path(catalog_dir) / "schema_artifacts"
    else:
        from nl2sql_agent.services.deps import PROJECT_ROOT

        configured_root = Path(config.get("artifact_dir", "data/schema"))
        artifact_root = configured_root if configured_root.is_absolute() else PROJECT_ROOT / configured_root
    latest_mschema_path = artifact_root / datasource / "m-schema.json"
    if mode == "incremental":
        hydrate_enrichment(tables, latest_mschema_path)

    hashes = {table.table_name: compute_structure_hash(table) for table in tables}
    changed_names = {
        table.table_name
        for table in tables
        if mode == "full"
        or snapshot.get(table.table_name) != hashes[table.table_name]
        or table.table_name in override_tables
    }
    if mode == "incremental" and hasattr(deps.vector_store, "prepare_incremental"):
        deps.vector_store.prepare_incremental()
    # 变化表做一次受限画像；未变化表复用上一版本画像。分类规则对所有表重算，成本可忽略。
    ready_for_vector: list[str] = []
    for table in tables:
        if table.table_name in changed_names:
            enrich_table(executor, table, config, dialect=deps.config.dialect)
        else:
            for column in table.columns:
                classify_column(
                    column,
                    table,
                    enum_max_distinct=int(config.get("classification", {}).get("enum_max_distinct", 20)),
                )

    effective_before_generation = [apply_override(table, override) for table in tables]
    needs_generation = any(
        not has_sufficient_comments(table, config) for table in effective_before_generation
    )
    llm = None
    database_context = ""
    if needs_generation:
        try:
            from nl2sql_agent.services.llm import get_model_for_node

            llm = get_model_for_node("schema_comment_generation")
            database_context = generate_database_context(effective_before_generation, llm, prompts=deps.prompts)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"database_context: {e}")

    for table in tables:
        h = hashes[table.table_name]
        changed = table.table_name in changed_names
        if not changed:
            report.skipped += 1
            continue
        try:
            status = process_table(
                table, datasource, business_line, deps, review_store, config, override,
                llm=llm, database_context=database_context,
            )
            if status == "ingested":
                ready_for_vector.append(table.table_name)
            else:
                # 当前版本未通过质量门禁时，不能继续提供上一版本的陈旧向量。
                remove_table_from_store(deps.vector_store, table.table_name)
                report.queued += 1
                review_store.update_snapshot(datasource, table.table_name, h)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{table.table_name}: {e}")

    # 清理被删除的表(幽灵表)
    current_names = {t.table_name for t in tables}
    for name in set(snapshot) - current_names:
        remove_table_from_store(deps.vector_store, name)
        review_store.delete_snapshot(datasource, name)
        report.removed += 1

    # effective M-Schema 是唯一事实源；catalog 在其落盘后做单向投影。
    effective_tables = [apply_override(table, override) for table in tables]
    raw_mschema = build_mschema(
        tables,
        datasource=datasource,
        schema_name=schema_name,
        namespace=business_line,
        database_context=database_context,
    )
    effective_mschema = build_mschema(
        effective_tables,
        raw_tables=tables,
        datasource=datasource,
        schema_name=schema_name,
        namespace=business_line,
        database_context=database_context,
    )
    for table in effective_tables:
        effective_mschema["tables"][table.table_name]["retrieval_eligible"] = (
            has_sufficient_comments(table, config)
        )
    manifest = write_mschema_artifacts(
        raw_mschema,
        effective_mschema,
        artifact_root=artifact_root,
        prompt_version=str(config.get("prompt_version", "enterprise-xiyan-v1")),
    )
    # 向量文档只允许从已落盘的 effective M-Schema 构建。写入成功后才推进
    # 表结构快照，确保向量后端失败时下次增量同步能够自动重试。
    for table_name in ready_for_vector:
        try:
            write_mschema_table_embeddings(
                deps.vector_store,
                effective_mschema,
                table_name,
                manifest,
                columns_per_chunk=int(config.get("columns_per_chunk", 15)),
            )
            review_store.update_snapshot(datasource, table_name, hashes[table_name])
            report.ingested += 1
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"{table_name}: vector_write: {e}")
    if not any("vector_write:" in error for error in report.errors) and hasattr(
        deps.vector_store, "persist_cache"
    ):
        deps.vector_store.persist_cache(latest_mschema_path, manifest)
    report.mschema_path = str(latest_mschema_path)
    report.snapshot_id = manifest["snapshot_id"]
    return report
