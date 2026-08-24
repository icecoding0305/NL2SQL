from nl2sql_agent.services.relation_store import DatabaseRelationStore
from nl2sql_agent.services.schema_ingest.mysql_fetcher import ColumnMeta, TableMeta
from nl2sql_agent.services.schema_ingest.relation_discovery import (
    discover_relation_candidates,
    tables_from_mschema,
)
from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.services.schema_planner import build_schema_plan, parse_query_intent
from nl2sql_agent.state import FieldCandidate, IntentSlot, QueryIntent


def test_relation_store_crud_and_runtime_overlay(tmp_path):
    store = DatabaseRelationStore(tmp_path / "relations.db")
    created = store.create("db-a", {
        "source_table": "loan",
        "source_columns": ["cust_id"],
        "target_table": "customer",
        "target_columns": ["cust_id"],
        "cardinality": "many_to_one",
        "preferred_join_type": "inner",
        "description": "贷款关联客户",
        "enabled": True,
    })

    runtime = store.runtime_relations("db-a")
    assert len(runtime) == 1
    assert runtime[0]["status"] == "verified"
    assert runtime[0]["relation_type"] == "user_defined"
    assert runtime[0]["source_columns"] == ["cust_id"]

    updated = store.update(created["id"], {"enabled": False})
    assert updated and updated["enabled"] is False
    assert store.runtime_relations("db-a") == []
    assert store.delete(created["id"]) is True


def test_relation_discovery_uses_keys_types_and_safe_samples():
    customer_id = ColumnMeta(
        name="cust_id", type="varchar", comment="客户编号",
        primary_key=True, unique=True,
        profile={"examples": ["C001", "C002"]},
    )
    loan_customer_id = ColumnMeta(
        name="cust_id", type="varchar", comment="客户编号", indexed=True,
        profile={"examples": ["C001"]},
    )
    candidates = discover_relation_candidates([
        TableMeta("customer", "客户表", columns=[customer_id]),
        TableMeta("loan", "贷款表", columns=[loan_customer_id]),
    ])

    assert len(candidates) == 1
    assert candidates[0]["source_table"] == "loan"
    assert candidates[0]["target_table"] == "customer"
    assert candidates[0]["status"] == "inferred"
    assert candidates[0]["confidence"] >= 0.9
    assert candidates[0]["enabled"] is False


def test_active_discovery_restores_latest_schema_and_comment_overrides():
    tables = tables_from_mschema({
        "schema": "risk",
        "tables": {
            "customer": {
                "comment": "错误说明",
                "primary_keys": ["cust_id"],
                "unique_keys": [],
                "indexes": [{"name": "PRIMARY", "columns": ["cust_id"], "unique": True}],
                "fields": {
                    "cust_id": {
                        "type": "varchar", "raw_type": "varchar(64)",
                        "comment": "错误字段说明", "primary_key": True,
                        "unique": True, "indexed": True,
                        "profile": {"examples": ["C001"]},
                    }
                },
            },
            "loan": {
                "comment": "贷款表", "primary_keys": [], "unique_keys": [],
                "indexes": [{"name": "idx_cust", "columns": ["cust_id"], "unique": False}],
                "fields": {
                    "cust_id": {
                        "type": "varchar", "comment": "客户号",
                        "indexed": True, "profile": {"examples": ["C001"]},
                    }
                },
            },
        },
    }, {
        ("customer", None): "个人客户主数据表",
        ("customer", "cust_id"): "统一客户编号",
    })

    assert tables[0].table_comment == "个人客户主数据表"
    assert tables[0].columns[0].comment == "统一客户编号"
    assert discover_relation_candidates(tables)[0]["target_table"] == "customer"


def test_warehouse_discovery_uses_one_profile_anchor_without_keys():
    customer = ColumnMeta(
        name="cust_id", type="varchar", comment="客户编号", indexed=True,
        profile={"non_null_count": 100, "approx_distinct": 100, "examples": ["C1", "C2"]},
    )
    loan = ColumnMeta(
        name="cust_id", type="varchar", comment="客户编号", indexed=True,
        profile={"non_null_count": 100, "approx_distinct": 60, "examples": ["C1"]},
    )
    repay = ColumnMeta(
        name="cust_id", type="varchar", comment="客户编号", indexed=True,
        profile={"non_null_count": 100, "approx_distinct": 30, "examples": ["C2"]},
    )

    candidates = discover_relation_candidates([
        TableMeta("dwd_ip_indv_cust_info", "个人客户主数据", columns=[customer]),
        TableMeta("dwd_ar_loan_info", "借据信息", columns=[loan]),
        TableMeta("dwd_ev_repay_detail", "还款明细", columns=[repay]),
    ])

    assert len(candidates) == 2
    assert {item["target_table"] for item in candidates} == {"dwd_ip_indv_cust_info"}
    assert {item["source_table"] for item in candidates} == {
        "dwd_ar_loan_info", "dwd_ev_repay_detail",
    }
    assert all(item["status"] == "candidate" for item in candidates)
    assert all(item["validation_summary"]["discovery_mode"] == "profile_anchor" for item in candidates)


def test_warehouse_discovery_does_not_create_pairwise_relation_explosion():
    tables = []
    for index in range(8):
        tables.append(TableMeta(
            f"fact_{index}", "事实表",
            columns=[ColumnMeta(
                name="loan_no", type="varchar", comment="借据编号", indexed=True,
                profile={
                    "non_null_count": 100,
                    "approx_distinct": 100 if index == 0 else 20,
                    "examples": [f"L{index}"],
                },
            )],
        ))
    tables[0].table_name = "dwd_ar_loan_info"

    candidates = discover_relation_candidates(tables)

    assert len(candidates) == 7
    assert {item["target_table"] for item in candidates} == {"dwd_ar_loan_info"}


def test_temporal_identifier_is_not_treated_as_entity_relation():
    columns = [ColumnMeta(
        name="month_id", type="varchar", comment="统计月份", indexed=True,
        profile={"non_null_count": 12, "approx_distinct": 12},
    )]

    assert discover_relation_candidates([
        TableMeta("month_info", "月份", columns=columns),
        TableMeta("loan_month", "贷款月报", columns=columns),
    ]) == []


def test_discovered_relation_is_not_runtime_fact_until_verified(tmp_path):
    store = DatabaseRelationStore(tmp_path / "relations.db")
    candidate = {
        "source_table": "loan", "source_columns": ["cust_id"],
        "target_table": "customer", "target_columns": ["cust_id"],
        "cardinality": "many_to_one", "preferred_join_type": "inner",
        "status": "inferred", "confidence": 0.92,
        "evidence": ["字段名一致"], "validation_summary": {"target_unique": True},
    }

    assert store.replace_discovered("db-a", [candidate]) == 1
    discovered = store.list("db-a")[0]
    assert discovered["status"] == "inferred"
    assert store.runtime_relations("db-a") == []

    store.update(discovered["id"], {"status": "verified", "enabled": True})
    assert len(store.runtime_relations("db-a")) == 1


def test_relation_candidates_can_be_decided_in_one_batch(tmp_path):
    store = DatabaseRelationStore(tmp_path / "relations.db")
    candidates = []
    for index in range(3):
        candidate = {
            "source_table": f"loan_{index}", "source_columns": ["cust_id"],
            "target_table": "customer", "target_columns": ["cust_id"],
            "cardinality": "many_to_one", "preferred_join_type": "inner",
            "status": "candidate", "confidence": 0.8,
        }
        candidates.append(candidate)
    store.replace_discovered("db-a", candidates)
    pending = store.list("db-a")

    updated = store.decide_many(
        "db-a", [pending[0]["id"], pending[1]["id"]], "verified"
    )

    assert len(updated) == 2
    rows = {item["id"]: item for item in store.list("db-a")}
    assert all(rows[item]["status"] == "verified" for item in updated)
    assert all(rows[item]["enabled"] is True for item in updated)
    assert sum(item["status"] == "candidate" for item in rows.values()) == 1


def test_customer_information_is_recognized_as_vague_projection():
    intent = parse_query_intent("统计贷款金额超过1000且有逾期的客户信息")
    assert any(item.text == "客户信息" for item in intent.attributes)


def test_verified_relation_guides_entity_table_selection():
    loan = TableDef(
        name="dws_ar_loan_info",
        comment="贷款借据汇总表",
        business_line="risk_mart",
        columns=[
            {"name": "cust_id", "comment": "客户编号", "type": "varchar"},
            {"name": "loan_amt", "comment": "贷款金额", "type": "decimal"},
            {"name": "ovd_bal", "comment": "逾期本金余额", "type": "decimal"},
        ],
    )
    wrong_customer = TableDef(
        name="app_custl_credit_info",
        comment="客户信用信息汇总表",
        business_line="risk_mart",
        columns=[
            {"name": "cust_id", "comment": "客户编号", "type": "varchar"},
            {"name": "credit_amt", "comment": "授信金额", "type": "decimal"},
        ],
    )
    customer = TableDef(
        name="dwd_ip_indv_cust_info",
        comment="个人客户信息表",
        business_line="risk_mart",
        columns=[
            {"name": "cust_id", "comment": "客户编号", "type": "varchar", "primary_key": True},
            {"name": "name", "comment": "姓名", "type": "varchar"},
            {"name": "phone_no", "comment": "手机号码", "type": "varchar"},
            {"name": "resiaddr", "comment": "居住地址", "type": "varchar"},
        ],
    )
    intent = QueryIntent(
        query_type="fact_filter",
        entities=[IntentSlot(text="客户", role="entity")],
        measures=[IntentSlot(text="贷款金额", role="measure")],
        attributes=[IntentSlot(text="客户信息", role="attribute")],
        filters=[IntentSlot(text="贷款金额", role="measure", operator=">", value=1000)],
    )
    candidates = [FieldCandidate(
        table_name=loan.name,
        column_name="loan_amt",
        column_comment="贷款金额",
        query_slot="贷款金额",
        data_type="decimal",
        final_score=0.95,
        phrase_coverage=1.0,
    )]
    relations = [{
        "source_table": loan.name,
        "source_columns": ["cust_id"],
        "target_table": customer.name,
        "target_columns": ["cust_id"],
        "relation_type": "user_defined",
        "status": "verified",
    }]

    plan = build_schema_plan(
        intent, [wrong_customer, loan, customer], candidates, relations
    )

    assert [item.table_name for item in plan.dimension_tables] == [customer.name]
    assert plan.relations == relations
    assert not any("关联路径" in slot for slot in plan.unresolved_slots)
