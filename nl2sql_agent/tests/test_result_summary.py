from nl2sql_agent.nodes.m11_result_interpretation import make_result_interpretation_node
from nl2sql_agent.state import NL2SQLState, QueryPlan


def _state(rows, plan=None):
    return NL2SQLState(
        user_query="查询代偿客户姓名和地址",
        user_id="u1",
        data_scope=["risk_mart"],
        execution_result=rows,
        query_plan=plan,
    )


def test_detail_result_has_business_summary_without_raw_row_dump(deps):
    plan = QueryPlan(
        target_tables=["customer"],
        output_fields=[
            {"concept": "客户姓名", "table": "customer", "column": "NAME"},
            {"concept": "居住地址", "table": "customer", "column": "RESIADDR"},
        ],
        output_grain={"level": "entity", "entity": "客户", "keys": ["customer.CUST_ID"]},
    )
    rows = [
        {"NAME": "张三", "RESIADDR": "地址A"},
        {"NAME": "李四", "RESIADDR": "地址B"},
    ]

    out = make_result_interpretation_node(deps)(_state(rows, plan))

    summary = out["result_summary"]
    assert summary.headline == "已完成查询，共返回 2 行结果"
    assert "每行代表一个客户" in summary.overview
    assert summary.key_findings == ["结果包含：客户姓名、居住地址。"]
    assert "张三" not in out["final_answer"]
    assert "{'NAME'" not in out["final_answer"]


def test_empty_result_explains_meaning_and_next_checks(deps):
    out = make_result_interpretation_node(deps)(_state([]))

    summary = out["result_summary"]
    assert summary.status == "empty"
    assert summary.headline == "未找到符合条件的数据"
    assert any("筛选条件" in item for item in summary.caveats)


def test_aggregate_falls_back_to_factual_structured_summary(deps):
    plan = QueryPlan(
        target_tables=["loan"],
        output_fields=[{
            "concept": "逾期客户数", "table": "loan", "column": "CUST_ID",
            "alias": "overdue_count", "aggregation": "count_distinct",
        }],
        output_grain={"level": "global", "keys": []},
    )

    out = make_result_interpretation_node(deps)(_state([{"overdue_count": 12}], plan))

    summary = out["result_summary"]
    assert summary.status == "success"
    assert any("逾期客户数为 12" in item for item in summary.key_findings)
    assert summary.row_count == 1
