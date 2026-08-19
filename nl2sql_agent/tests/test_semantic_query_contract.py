from nl2sql_agent.services.plan_normalizer import normalize_structural_coverage
from nl2sql_agent.services.projection_resolver import _fallback_topic_decision
from nl2sql_agent.services.semantic_coverage import ensure_semantic_coverage
from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.services.schema_planner import ground_output_bindings
from nl2sql_agent.services.semantic_parser import build_semantic_graph, semantic_graph_to_query_intent
from nl2sql_agent.services.semantic_query import enrich_semantic_graph
from nl2sql_agent.services.sql_compiler import compile_query_plan
from nl2sql_agent.state import (
    JoinSpec, OutputFieldSpec, OutputGrain, QueryPlan, SemanticGraph,
    SemanticPredicate, SemanticSubject,
)


def test_explicit_outputs_order_and_top_n_are_preserved():
    query = "查询贷款金额最高的前5笔借据，返回借据编号、产品编码和贷款金额"
    graph = enrich_semantic_graph(query, SemanticGraph())

    assert [item.concept for item in graph.outputs] == ["借据编号", "产品编码", "贷款金额"]
    assert graph.limit == 5
    assert graph.order_by[0].concept == "贷款金额"
    assert graph.order_by[0].direction == "desc"


def test_grouped_metrics_become_base_concepts_and_aggregations():
    query = "统计每个产品的贷款笔数、贷款总金额、平均贷款金额和剩余本金"
    graph = build_semantic_graph(query)
    intent = semantic_graph_to_query_intent(graph, query)
    outputs = {item.concept: item for item in graph.outputs}

    assert graph.group_by == ["产品"]
    assert outputs["贷款笔数"].aggregation == "count_distinct"
    assert outputs["贷款总金额"].grounding_concept == "贷款金额"
    assert outputs["贷款总金额"].aggregation == "sum"
    assert outputs["平均贷款金额"].grounding_concept == "贷款金额"
    assert outputs["平均贷款金额"].aggregation == "avg"
    assert outputs["剩余本金"].grounding_concept == "贷款本金余额"
    assert outputs["剩余本金"].aggregation == "sum"
    assert [item.text for item in intent.dimensions] == ["产品"]
    assert "贷款笔数" not in [item.text for item in intent.attributes]


def test_count_distinct_uses_entity_identifier_not_similar_attribute():
    graph = enrich_semantic_graph("统计贷款笔数", SemanticGraph())
    # No explicit output marker: use a minimal model-produced output graph.
    graph = build_semantic_graph("统计贷款笔数")
    if not graph.outputs:
        from nl2sql_agent.state import SemanticOutput, SemanticSubject
        subject = SemanticSubject(id="loan", kind="event", concept="贷款")
        graph = enrich_semantic_graph("统计贷款笔数", SemanticGraph(
            subjects=[subject],
            outputs=[SemanticOutput(
                id="output_1", subject_id="loan", concept="贷款笔数",
                grounding_concept="贷款笔数", source_text="贷款笔数", required=True,
            )],
        ))
    table = TableDef("loan", "贷款借据", "risk", [
        {"name": "LOAN_NO", "comment": "借据编号", "primary_key": True},
        {"name": "CANCEL_REASON", "comment": "贷款撤销原因"},
    ])

    bindings = ground_output_bindings(graph, [], tables=[table])

    output = next(item for item in graph.outputs if item.concept == "贷款笔数")
    assert bindings[output.id]["column_name"] == "LOAN_NO"
    assert bindings[output.id]["aggregation"] == "count_distinct"


def test_contract_normalization_and_compiler_keep_sum_avg_order_and_limit():
    query = "查询贷款金额最高的前5笔借据，返回借据编号、产品编码和贷款金额"
    graph = enrich_semantic_graph(query, SemanticGraph())
    bindings = {}
    physical = {
        "借据编号": "LOAN_NO",
        "产品编码": "PRD_CODE",
        "贷款金额": "LOAN_AMT",
    }
    for output in graph.outputs:
        bindings[output.id] = {
            "table_name": "loan", "column_name": physical[output.concept],
            "bindings": [{"table_name": "loan", "column_name": physical[output.concept]}],
        }
    raw = QueryPlan(target_tables=["loan"], output_fields=[])

    normalized, _ = normalize_structural_coverage(raw, graph, bindings)
    sql, _ = compile_query_plan(normalized, "mysql")

    assert "PRD_CODE" in sql
    assert "ORDER BY" in sql and "DESC" in sql
    assert "LIMIT 5" in sql


def test_multi_fact_metrics_are_aggregated_before_joining():
    plan = QueryPlan(
        target_tables=["loan", "claim", "customer"],
        join_logic=[
            JoinSpec(
                left_table="claim", right_table="loan",
                left_column="LOAN_NO", right_column="LOAN_NO",
            ),
            JoinSpec(
                left_table="loan", right_table="customer",
                left_column="CUST_ID", right_column="CUST_ID",
            ),
        ],
        group_by=["customer.CUST_ID"],
        output_fields=[
            OutputFieldSpec(
                concept="客户", table="customer", column="CUST_ID", alias="客户",
            ),
            OutputFieldSpec(
                concept="累计贷款金额", table="loan", column="LOAN_AMT",
                aggregation="sum", alias="累计贷款金额",
            ),
            OutputFieldSpec(
                concept="累计代偿本金", table="claim", column="DC_BAL",
                aggregation="sum", alias="累计代偿本金",
            ),
        ],
        output_grain=OutputGrain(
            level="aggregate", entity="客户", keys=["customer.CUST_ID"],
        ),
    )

    sql, used_tables = compile_query_plan(plan, "mysql")

    assert sql.count("GROUP BY customer.CUST_ID") == 2
    assert "SUM(loan.LOAN_AMT)" in sql
    assert "SUM(claim.DC_BAL)" in sql
    assert "fact_agg_1" in sql and "fact_agg_2" in sql
    assert "SUM(loan.LOAN_AMT)" not in sql.split("FROM (", 1)[0]
    assert set(used_tables) == {"loan", "claim", "customer"}


def test_multi_fact_filters_are_scoped_inside_fact_preaggregation():
    from nl2sql_agent.state import FilterSpec

    plan = QueryPlan(
        target_tables=["loan", "claim", "customer"],
        join_logic=[
            JoinSpec(
                left_table="claim", right_table="loan",
                left_column="LOAN_NO", right_column="LOAN_NO",
            ),
            JoinSpec(
                left_table="loan", right_table="customer",
                left_column="CUST_ID", right_column="CUST_ID",
            ),
        ],
        filters=[FilterSpec(table="claim", column="STATUS", operator="=", value="PAID")],
        group_by=["customer.CUST_ID"],
        output_fields=[
            OutputFieldSpec(concept="客户", table="customer", column="CUST_ID"),
            OutputFieldSpec(
                concept="贷款金额", table="loan", column="LOAN_AMT", aggregation="sum",
            ),
            OutputFieldSpec(
                concept="代偿本金", table="claim", column="DC_BAL", aggregation="sum",
            ),
        ],
    )

    sql, _ = compile_query_plan(plan, "mysql")

    assert "WHERE claim.STATUS = 'PAID'" in sql
    assert "fact_agg_" in sql
    assert "NOT fact_agg_" in sql and "IS NULL" in sql


def test_broad_overdue_topic_is_repaired_and_grouped_before_schema_retrieval():
    query = "统计户籍地址为上海的客户的逾期情况"
    graph = SemanticGraph(subjects=[
        SemanticSubject(id="customer", kind="entity", concept="客户"),
    ])

    repaired, coverage = ensure_semantic_coverage(query, graph)

    assert repaired.query_action == "aggregate"
    assert repaired.group_by == ["客户"]
    assert any(item.concept == "逾期情况" and item.broad for item in repaired.outputs)
    assert any(item.concept == "客户" for item in repaired.outputs)
    assert coverage["repaired_mentions"] == ["逾期情况"]
    assert coverage["uncovered_mentions"] == []


def test_schema_driven_topic_fallback_selects_aggregate_fields():
    decision = _fallback_topic_decision(
        request="逾期情况",
        target_entity="客户",
        query="统计户籍地址为上海的客户的逾期情况",
        candidates=[
            {
                "table_name": "loan", "column_name": "OVD_BAL",
                "business_label": "逾期本金余额", "type": "decimal",
                "sensitive": False,
            },
            {
                "table_name": "loan", "column_name": "OVD_DAYS",
                "business_label": "最大逾期天数", "type": "int",
                "sensitive": False,
            },
            {
                "table_name": "customer", "column_name": "NAME",
                "business_label": "姓名", "type": "varchar",
                "sensitive": False,
            },
        ],
    )

    assert [(item.column_name, item.aggregation) for item in decision.selected_fields] == [
        ("OVD_BAL", "sum"), ("OVD_DAYS", "max"),
    ]


def test_aggregate_comparison_is_normalized_to_having_and_compiled():
    graph = SemanticGraph(
        subjects=[SemanticSubject(id="customer", kind="entity", concept="客户")],
        group_by=["客户"],
        predicate=SemanticPredicate(
            atom_id="aggregate_1",
            predicate_type="aggregate_comparison",
            subject_id="customer",
            concept="累计贷款金额",
            grounding_concept="贷款金额",
            operator=">",
            value=1000,
            scope="per_entity",
            source_text="累计贷款金额超过1000",
        ),
    )
    plan = QueryPlan(
        target_tables=["loan"],
        group_by=["loan.CUST_ID"],
        output_fields=[
            OutputFieldSpec(concept="客户", table="loan", column="CUST_ID"),
            OutputFieldSpec(
                concept="累计贷款金额", table="loan", column="LOAN_AMT",
                aggregation="sum", alias="累计贷款金额",
            ),
        ],
    )

    normalized, _ = normalize_structural_coverage(
        plan,
        graph,
        semantic_bindings={
            "aggregate_1": {
                "table_name": "loan", "column_name": "LOAN_AMT",
                "operator": ">", "value": 1000, "aggregation": "sum",
                "scope": "per_entity",
            },
        },
    )
    sql, _ = compile_query_plan(normalized, "mysql")

    assert not normalized.filters
    assert normalized.having[0].aggregation == "sum"
    assert "HAVING SUM(loan.LOAN_AMT) > 1000" in sql


def test_model_operator_aliases_are_normalized_before_value_grounding():
    from nl2sql_agent.state import FilterSpec

    predicate = SemanticPredicate(
        atom_id="address", predicate_type="comparison",
        concept="户籍地址", operator="equals", value="上海",
    )
    filter_spec = FilterSpec(
        table="customer", column="HHDIST", operator="equals", value="上海市",
    )

    assert predicate.operator == "="
    assert filter_spec.operator == "="
