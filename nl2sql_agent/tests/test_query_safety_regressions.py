import hashlib

import sqlglot

from nl2sql_agent.nodes.m6_plan_validation import validate_plan
from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node
from nl2sql_agent.services.field_labels import concise_business_label
from nl2sql_agent.services.sql_compiler import compile_query_plan
from nl2sql_agent.state import (
    NL2SQLState,
    QueryPlan,
    SchemaHit,
    SemanticGraph,
    SemanticPredicate,
)


def test_enum_comment_is_not_used_as_business_label():
    assert concise_business_label(
        "还款状态(00:正常还款,03:逾期,04:逾期还款)", "REPAY_STATUS"
    ) == "还款状态"


def test_compiler_quotes_and_shortens_descriptive_alias():
    plan = QueryPlan(
        target_tables=["repay"],
        output_fields=[{
            "concept": "还款状态",
            "table": "repay",
            "column": "REPAY_STATUS",
            "alias": "还款状态(00:正常还款,03:逾期)",
        }],
    )

    sql, _ = compile_query_plan(plan, "mysql")

    parsed = sqlglot.parse_one(sql, read="mysql")
    assert parsed.expressions[0].alias == "还款状态"
    assert "`还款状态`" in sql
    assert "00:正常还款" not in sql


def test_plan_rejects_filter_without_semantic_source(deps):
    graph = SemanticGraph(predicate=SemanticPredicate(
        atom_id="atom_1",
        predicate_type="comparison",
        concept="户籍地址",
        operator="=",
        value="上海",
        source_text="户籍地址为上海",
        materiality="high",
    ))
    plan = QueryPlan(
        target_tables=["customer", "repay"],
        filters=[
            {
                "table": "customer", "column": "HHDIST", "operator": "=",
                "value": "上海", "source_atom_ids": ["atom_1"],
            },
            {
                "table": "repay", "column": "REPAY_STATUS", "operator": "=",
                "value": "03", "source_atom_ids": [],
            },
        ],
        output_fields=[{
            "concept": "客户编号", "table": "customer", "column": "CUST_ID",
        }],
        covered_atom_ids=["atom_1"],
    )
    schema = [
        SchemaHit(table_name="customer", columns=[{"name": "CUST_ID"}, {"name": "HHDIST"}]),
        SchemaHit(table_name="repay", columns=[{"name": "REPAY_STATUS"}]),
    ]

    errors = validate_plan(
        plan, schema, deps.term_mapping, ["risk_mart"], semantic_graph=graph,
    )

    assert any("没有用户语义或可信规则来源" in error for error in errors)


def test_plan_rejects_mixed_aggregate_without_group_by(deps):
    plan = QueryPlan(
        target_tables=["loan", "repay"],
        output_fields=[
            {
                "concept": "逾期本金余额", "table": "loan", "column": "OVD_BAL",
                "aggregation": "sum",
            },
            {
                "concept": "还款状态", "table": "repay", "column": "REPAY_STATUS",
            },
        ],
    )
    schema = [
        SchemaHit(table_name="loan", columns=[{"name": "OVD_BAL"}]),
        SchemaHit(table_name="repay", columns=[{"name": "REPAY_STATUS"}]),
    ]

    errors = validate_plan(plan, schema, deps.term_mapping, ["risk_mart"])

    assert any("非聚合字段必须进入 GROUP BY" in error for error in errors)


def test_entity_aggregate_requires_returned_and_grouped_key(deps):
    plan = QueryPlan(
        target_tables=["customer", "loan"],
        output_fields=[{
            "concept": "逾期本金余额", "table": "loan", "column": "OVD_BAL",
            "aggregation": "sum",
        }],
        output_grain={
            "level": "entity", "entity": "客户", "keys": ["customer.CUST_ID"],
        },
    )
    schema = [
        SchemaHit(table_name="customer", columns=[{"name": "CUST_ID"}]),
        SchemaHit(table_name="loan", columns=[{"name": "OVD_BAL"}]),
    ]

    errors = validate_plan(plan, schema, deps.term_mapping, ["risk_mart"])

    assert any("结果粒度必须返回" in error for error in errors)
    assert any("实体/聚合粒度必须按粒度键" in error for error in errors)


def test_deterministic_invalid_sql_does_not_retry_same_plan(deps):
    state = NL2SQLState(
        user_query="查询客户", user_id="u1", generated_sql="SELECT ( FROM customer",
        used_tables=["customer"], sql_generation_source="deterministic",
    )

    out = make_static_validation_node(deps)(state)

    assert out["retry_count"] == state.max_retries
    assert out["terminal_status"] == "error"
    assert "确定性 SQL 编译结果未通过校验" in out["final_answer"]


def test_repeated_model_sql_is_not_retried_again(deps):
    sql = "SELECT ( FROM customer"
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    state = NL2SQLState(
        user_query="查询客户", user_id="u1", generated_sql=sql,
        used_tables=["customer"], sql_generation_source="model",
        failed_sql_hashes=[digest], retry_count=1,
    )

    out = make_static_validation_node(deps)(state)

    assert out["retry_count"] == state.max_retries
    assert out["terminal_status"] == "error"
