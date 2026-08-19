from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.services.schema_planner import (
    build_schema_plan,
    parse_query_intent,
    plan_table_names,
    rank_field_candidates,
)


QUERY = (
    "查询贷款金额超过 1000 元且逾期本金余额大于 0 的客户姓名、"
    "借据编号、贷款金额和逾期本金余额"
)


def _tables() -> list[TableDef]:
    return [
        TableDef("dwd_ar_loan_info", "贷款借据信息表", "test", [
            {"name": "CUST_ID", "type": "varchar", "comment": "客户编号"},
            {"name": "NAME", "type": "varchar", "comment": "客户姓名"},
            {"name": "LOAN_NO", "type": "varchar", "comment": "借据编号"},
            {
                "name": "LOAN_AMT", "type": "decimal", "comment": "贷款金额",
                "semantic_role": "measure",
            },
            {
                "name": "OVD_BAL", "type": "decimal", "comment": "逾期本金余额",
                "semantic_role": "measure",
            },
        ]),
        TableDef("dwd_ip_indv_cust_info", "个人客户基本信息表", "test", [
            {
                "name": "CUST_ID", "type": "varchar", "comment": "客户编号",
                "primary_key": True,
            },
            {"name": "NAME", "type": "varchar", "comment": "客户姓名"},
            {"name": "ADDRESS", "type": "varchar", "comment": "客户地址"},
        ]),
    ]


def test_comparison_parser_accepts_spaces_units_and_chained_conditions():
    intent = parse_query_intent(QUERY)

    assert [(item.text, item.operator, item.value) for item in intent.filters] == [
        ("贷款金额", ">", 1000),
        ("逾期本金余额", ">", 0),
    ]
    assert [item.text for item in intent.attributes] == [
        "客户姓名", "借据编号", "贷款金额", "逾期本金余额",
    ]


def test_explicit_outputs_reuse_fact_table_without_self_relation():
    tables = _tables()
    intent = parse_query_intent(QUERY)
    candidates = rank_field_candidates(intent, tables)
    plan = build_schema_plan(intent, tables, candidates, [])

    assert plan.unresolved_slots == []
    assert plan_table_names(plan) == ["dwd_ar_loan_info"]
    assert plan.dimension_tables == []
    assert set(plan.anchor_tables[0].selected_columns) == {
        "NAME", "LOAN_NO", "LOAN_AMT", "OVD_BAL",
    }

