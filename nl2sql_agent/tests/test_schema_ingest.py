"""表结构入库验收测试(不连真库,FakeSchemaExecutor 模拟 information_schema)。

验收:
1. 注释齐全的表 → 直接入库,不进审核队列
2. 注释缺失/覆盖率不足 → 进审核队列,不入向量库
3. approve → 覆盖层写入,重新入库用覆盖注释
4. 含身份证字段的表 → 生成注释用的样例值已脱敏
5. 改一个字段注释 → 增量只重处理该表
6. 删表 → 向量条目 + 快照被清理(无幽灵表)
"""

from __future__ import annotations

import json
import re

import yaml

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.deps import CONFIG_DIR
from nl2sql_agent.services.schema_ingest.comment_generator import (
    fetch_masked_sample_values,
    has_sufficient_comments,
    validate_comment_draft,
)
from nl2sql_agent.services.schema_ingest.diff_sync import sync
from nl2sql_agent.services.schema_ingest.mysql_fetcher import (
    ColumnMeta,
    TableMeta,
    fetch_information_schema,
)
from nl2sql_agent.services.schema_ingest.review_queue import ReviewStore
from nl2sql_agent.services.schema_ingest.text_builder import write_mschema_table_embeddings
from nl2sql_agent.services.schema_ingest.text_builder import build_table_level_text
from nl2sql_agent.testing import build_test_deps


class FakeSchemaExecutor:
    """模拟 information_schema 查询 + 数据取样。tables: {name: {comment, columns}}"""

    def __init__(self, tables: dict):
        self.tables = tables
        self.samples: dict[str, list[dict]] = {}
        self.constraints: list[dict] = []
        self.indexes: list[dict] = []

    def execute(self, sql: str, timeout_seconds: int = 30, params: tuple | None = None):
        up = sql.upper()
        if "INFORMATION_SCHEMA.TABLES" in up:
            return [
                {"TABLE_NAME": n, "TABLE_COMMENT": m["comment"], "TABLE_ROWS": 10}
                for n, m in self.tables.items()
            ]
        if "INFORMATION_SCHEMA.KEY_COLUMN_USAGE" in up:
            return self.constraints
        if "INFORMATION_SCHEMA.STATISTICS" in up:
            return self.indexes
        if "INFORMATION_SCHEMA.COLUMNS" in up:
            out = []
            for n, m in self.tables.items():
                for c in m["columns"]:
                    out.append({
                        "TABLE_NAME": n, "COLUMN_NAME": c["name"],
                        "DATA_TYPE": c["type"], "COLUMN_COMMENT": c.get("comment", ""),
                        "COLUMN_TYPE": c.get("raw_type", c["type"]),
                        "IS_NULLABLE": c.get("nullable", "YES"),
                        "COLUMN_DEFAULT": c.get("default"),
                        "COLUMN_KEY": c.get("key", ""),
                        "ORDINAL_POSITION": c.get("ordinal", 0),
                    })
            return out
        # 数据取样:SELECT ... FROM `table`
        m = re.search(r"FROM\s+`?(\w+)`?", sql, re.IGNORECASE)
        return self.samples.get(m.group(1) if m else "", [])

    def explain(self, sql: str):
        raise NotImplementedError


def _good_table():
    return {"comment": "贷款借据信息表", "columns": [
        {"name": "LOAN_NO", "type": "varchar", "comment": "借据编号"},
        {"name": "LOAN_AMT", "type": "decimal", "comment": "贷款金额"},
        {"name": "PLATFORM_CODE", "type": "varchar", "comment": "平台代码"},
    ]}


def _bad_table():
    return {"comment": "", "columns": [
        {"name": "A", "type": "int", "comment": ""},
        {"name": "B", "type": "varchar", "comment": ""},
    ]}


def _config():
    return ConfigLoader(CONFIG_DIR).load("schema_ingest.yaml") or {}


def _make_deps(executor, monkeypatch=None, fake_llm=None):
    deps = build_test_deps(executor=executor)
    if monkeypatch is not None and fake_llm is not None:
        from nl2sql_agent.services import llm

        monkeypatch.setattr(llm, "get_model_for_node", lambda node: fake_llm)
    return deps


# ---------------- 验收 1/2:质量判断 ----------------

def test_ingest_quality_gate(tmp_path, monkeypatch):
    ex = FakeSchemaExecutor({"good_table": _good_table(), "bad_table": _bad_table()})
    llm = _FakeCommentLLM()
    deps = _make_deps(ex, monkeypatch, llm)
    store = ReviewStore(tmp_path / "review.db")
    report = sync("ds", "s", deps, _config(), store, mode="full", catalog_dir=tmp_path)

    assert report.ingested == 1  # good_table 直接入库
    assert report.queued == 1     # bad_table 进审核

    vs = deps.vector_store
    table_coll = vs._store.get(vs.COLLECTION_TABLE, {})  # noqa: SLF001
    assert "good_table" in table_coll
    assert "bad_table" not in table_coll  # 未入库
    manifest = json.loads(
        (tmp_path / "schema_artifacts" / "ds" / "manifest.json").read_text(encoding="utf-8")
    )
    vector_item = table_coll["good_table"]
    assert vector_item["metadata"]["source"] == "effective-m-schema"
    assert vector_item["metadata"]["snapshot_id"] == report.snapshot_id
    assert vector_item["metadata"]["semantic_hash"] == manifest["semantic_hash"]
    assert vector_item["metadata"]["table_semantic_hash"]
    assert vector_item["text"].startswith("表名:good_table")

    from nl2sql_agent.services.schema_catalog import SchemaCatalog
    from nl2sql_agent.services.vector_store.memory import InMemoryVectorStore

    runtime_catalog = SchemaCatalog(ConfigLoader(tmp_path))
    embed_calls = 0

    def counting_embed(texts):
        nonlocal embed_calls
        embed_calls += 1
        return [[1.0, 0.0] for _ in texts]

    restarted_store = InMemoryVectorStore(
        runtime_catalog,
        embed=counting_embed,
    )
    restarted_store._ensure_indexed()  # noqa: SLF001
    restarted_tables = restarted_store._store[restarted_store.COLLECTION_TABLE]  # noqa: SLF001
    assert set(restarted_tables) == {"good_table"}
    assert restarted_tables["good_table"]["metadata"]["source"] == "effective-m-schema"
    assert embed_calls == 0  # 语义哈希与模型签名一致时直接加载磁盘向量缓存
    assert store.pending_count("ds") == 3  # 表级 + 2 字段
    assert llm.calls >= 4  # 数据库理解 + 表理解 + 同类字段辨析 + 最终描述
    review = store.list_reviews()[0]
    assert isinstance(review["evidence_json"], dict)
    assert review["draft_confidence"] > 0


# ---------------- 验收 3:approve → 覆盖层 → 重新入库 ----------------

def test_approve_writes_override_and_reingest(tmp_path, monkeypatch):
    ex = FakeSchemaExecutor({"bad_table": _bad_table()})
    deps = _make_deps(ex, monkeypatch, _FakeCommentLLM())
    store = ReviewStore(tmp_path / "review.db")
    sync("ds", "s", deps, _config(), store, mode="full", catalog_dir=tmp_path)
    assert store.pending_count("ds") >= 1

    # 审核通过所有条目(草稿 → 覆盖层)
    for rec in store.list_reviews(status="pending"):
        assert store.approve(rec["id"], rec["draft_comment"], "reviewer")

    # 重新全量入库:bad_table 现在用覆盖层注释,质量达标 → 入库
    report = sync("ds", "s", deps, _config(), store, mode="full", catalog_dir=tmp_path)
    vs = deps.vector_store
    table_coll = vs._store.get(vs.COLLECTION_TABLE, {})  # noqa: SLF001
    assert "bad_table" in table_coll
    text = table_coll["bad_table"]["text"]
    assert "坏表说明" in text  # 用的是覆盖后的注释,而非原生空注释


# ---------------- 验收 4:脱敏 ----------------

def test_mask_sensitive_samples():
    ex = FakeSchemaExecutor({"t": {"comment": "x", "columns": [
        {"name": "IDNUM", "type": "varchar", "comment": "证件号码"},
        {"name": "NAME", "type": "varchar", "comment": "姓名"},
    ]}})
    ex.samples["t"] = [{"IDNUM": "110101199001011234", "NAME": "张三"}]
    table = TableMeta(
        table_name="t", table_comment="x",
        columns=[
            ColumnMeta("IDNUM", "varchar", "证件号码", sensitive=True),
            ColumnMeta("NAME", "varchar", "姓名", sensitive=False),
        ],
    )
    samples = fetch_masked_sample_values(ex, table, limit=3)
    # 前3后4保留,中间打码:110***********1234(真实中间段不泄露)
    assert samples["IDNUM"][0] == "110***********1234"
    assert "199001011234" not in samples["IDNUM"][0]
    assert samples["NAME"][0] == "张三"


# ---------------- 验收 5:增量只处理变化的表 ----------------

def test_incremental_only_processes_changed(tmp_path):
    tables = {"t1": _good_table(), "t2": _good_table()}
    ex = FakeSchemaExecutor(tables)
    deps = _make_deps(ex)
    store = ReviewStore(tmp_path / "review.db")
    sync("ds", "s", deps, _config(), store, mode="full", catalog_dir=tmp_path)

    # 改 t2 的一个字段注释(模拟 DDL 变更)
    tables["t2"]["columns"][1]["comment"] = "贷款金额(改)"
    ex = FakeSchemaExecutor(tables)
    deps = _make_deps(ex)
    report = sync("ds", "s", deps, _config(), store, mode="incremental", catalog_dir=tmp_path)
    assert report.ingested == 1   # 只有 t2 被重处理
    assert report.skipped == 1    # t1 未变化


# ---------------- 验收 6:删表清理幽灵表 ----------------

def test_incremental_removes_dropped_table(tmp_path):
    tables = {"t1": _good_table(), "t2": _good_table()}
    ex = FakeSchemaExecutor(tables)
    deps = _make_deps(ex)
    store = ReviewStore(tmp_path / "review.db")
    sync("ds", "s", deps, _config(), store, mode="full", catalog_dir=tmp_path)
    assert len(deps.vector_store._store.get(deps.vector_store.COLLECTION_TABLE, {})) == 2  # noqa: SLF001

    # 删除 t2(模拟库中表被删)
    tables.pop("t2")
    ex = FakeSchemaExecutor(tables)
    deps = _make_deps(ex)
    report = sync("ds", "s", deps, _config(), store, mode="incremental", catalog_dir=tmp_path)
    assert report.removed == 1
    table_coll = deps.vector_store._store.get(deps.vector_store.COLLECTION_TABLE, {})  # noqa: SLF001
    assert "t2" not in table_coll  # 向量条目被清理,无幽灵表
    assert "t2" not in store.load_snapshot("ds")


def test_mschema_vector_rewrite_removes_stale_chunks():
    deps = _make_deps(FakeSchemaExecutor({}))
    mschema = {
        "format_version": "1.0",
        "db_id": "ds",
        "namespace": "risk_mart",
        "tables": {
            "t": {
                "comment": "测试表",
                "fields": {
                    "A": {"type": "int", "dim_or_meas": "measure", "category": "numeric"},
                    "B": {"type": "int", "dim_or_meas": "measure", "category": "numeric"},
                },
            }
        },
        "relations": [],
    }
    manifest = {"snapshot_id": "s1", "semantic_hash": "h1"}
    write_mschema_table_embeddings(deps.vector_store, mschema, "t", manifest, columns_per_chunk=1)
    column_store = deps.vector_store._store[deps.vector_store.COLLECTION_COLUMN]  # noqa: SLF001
    assert set(column_store) == {"t#col#0", "t#col#1"}

    mschema["tables"]["t"]["fields"].pop("B")
    write_mschema_table_embeddings(deps.vector_store, mschema, "t", manifest, columns_per_chunk=1)
    assert set(column_store) == {"t#col#0"}


def test_wide_table_text_keeps_all_fields_and_prioritizes_keys():
    fields = {
        f"C{index}": {"type": "varchar", "comment": f"字段{index}"}
        for index in range(10)
    }
    fields["C9"]["primary_key"] = True
    text = build_table_level_text(
        "wide_table", {"fields": fields, "primary_keys": ["C9"]}, []
    )
    assert "核心字段:C9" in text
    assert "全部字段:C0,C1,C2,C3,C4,C5,C6,C7,C8,C9" in text


def test_vector_failure_does_not_advance_incremental_snapshot(tmp_path, monkeypatch):
    ex = FakeSchemaExecutor({"good_table": _good_table()})
    deps = _make_deps(ex)
    store = ReviewStore(tmp_path / "review.db")

    def fail_upsert(*args, **kwargs):
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr(deps.vector_store, "upsert", fail_upsert)
    report = sync("ds", "s", deps, _config(), store, mode="full", catalog_dir=tmp_path)
    assert report.ingested == 0
    assert any("vector_write" in error for error in report.errors)
    assert "good_table" not in store.load_snapshot("ds")


def test_extract_constraints_and_generate_mschema(tmp_path):
    ex = FakeSchemaExecutor({"loan": {"comment": "贷款表", "columns": [
        {"name": "LOAN_NO", "type": "varchar", "comment": "借据编号", "key": "PRI", "nullable": "NO"},
        {"name": "CUST_ID", "type": "varchar", "comment": "客户编号", "key": "MUL"},
        {"name": "AMT", "type": "decimal", "comment": "贷款金额"},
    ]}})
    ex.constraints = [
        {"TABLE_NAME": "loan", "CONSTRAINT_NAME": "PRIMARY", "CONSTRAINT_TYPE": "PRIMARY KEY",
         "COLUMN_NAME": "LOAN_NO", "ORDINAL_POSITION": 1, "REFERENCED_TABLE_NAME": None},
        {"TABLE_NAME": "loan", "CONSTRAINT_NAME": "fk_loan_cust", "CONSTRAINT_TYPE": "FOREIGN KEY",
         "COLUMN_NAME": "CUST_ID", "ORDINAL_POSITION": 1, "REFERENCED_TABLE_SCHEMA": "s",
         "REFERENCED_TABLE_NAME": "customer", "REFERENCED_COLUMN_NAME": "CUST_ID"},
    ]
    ex.indexes = [
        {"TABLE_NAME": "loan", "INDEX_NAME": "idx_cust", "NON_UNIQUE": 1,
         "COLUMN_NAME": "CUST_ID", "SEQ_IN_INDEX": 1},
    ]
    tables = fetch_information_schema(ex, "s")
    assert tables[0].primary_keys == ["LOAN_NO"]
    assert tables[0].relations[0].target_table == "customer"
    assert tables[0].indexes[0].columns == ["CUST_ID"]

    deps = _make_deps(ex)
    store = ReviewStore(tmp_path / "review.db")
    report = sync("ds", "s", deps, _config(), store, mode="full", catalog_dir=tmp_path)
    path = tmp_path / "schema_artifacts" / "ds" / "m-schema.json"
    assert report.mschema_path == str(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["format_version"] == "1.0"
    assert data["tables"]["loan"]["primary_keys"] == ["LOAN_NO"]
    assert data["tables"]["loan"]["description_confidence"] == 1.0
    assert data["relations"][0]["target_table"] == "customer"
    assert data["tables"]["loan"]["fields"]["AMT"]["dim_or_meas"] == "measure"
    assert (tmp_path / "schema_artifacts" / "ds" / "manifest.json").exists()

    catalog = yaml.safe_load((tmp_path / "schema_catalog.yaml").read_text(encoding="utf-8"))
    manifest = json.loads(
        (tmp_path / "schema_artifacts" / "ds" / "manifest.json").read_text(encoding="utf-8")
    )
    assert catalog["_meta"]["source"] == "effective-m-schema"
    assert catalog["_meta"]["semantic_hash"] == manifest["semantic_hash"]
    projected = catalog["risk_mart"]["tables"][0]
    assert projected["name"] == "loan"
    assert projected["columns"][0]["primary_key"] is True

    from nl2sql_agent.services.schema_catalog import SchemaCatalog

    runtime_catalog = SchemaCatalog(ConfigLoader(tmp_path))
    assert runtime_catalog.metadata["snapshot_id"] == report.snapshot_id
    assert runtime_catalog.tables_for_scope(["risk_mart"])[0].name == "loan"


def test_quality_gate_does_not_allow_missing_twenty_percent():
    table = TableMeta(
        table_name="t",
        table_comment="有效表说明",
        columns=[
            ColumnMeta(f"C{i}", "varchar", "有效说明" if i < 4 else "")
            for i in range(5)
        ],
    )
    assert table.comment_coverage == 0.8
    assert has_sufficient_comments(table, _config()) is False


def test_generated_description_fact_and_sensitive_validation():
    table = TableMeta(
        table_name="t",
        table_comment="",
        columns=[ColumnMeta("IDNUM", "varchar", "", sensitive=True, category="code")],
        primary_keys=["IDNUM"],
    )
    cleaned, errors, confidence = validate_comment_draft(
        table,
        {
            "table_comment": "客户表",
            "columns": {
                "IDNUM": "身份证号码 110101199001011234",
                "NOT_EXISTS": "不存在字段",
            },
        },
    )
    assert cleaned["columns"] == {}
    assert any("不存在字段" in error for error in errors)
    assert any("敏感值" in error for error in errors)
    assert 0 <= confidence < 1


class _FakeCommentLLM:
    def __init__(self):
        self.calls = 0

    def complete_json(self, prompt, schema, retries=1):
        self.calls += 1
        return {"table_comment": "坏表说明", "columns": {"A": "字段A说明", "B": "字段B说明"}}
