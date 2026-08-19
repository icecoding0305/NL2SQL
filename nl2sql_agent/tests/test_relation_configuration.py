from nl2sql_agent.services.relation_store import DatabaseRelationStore
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
