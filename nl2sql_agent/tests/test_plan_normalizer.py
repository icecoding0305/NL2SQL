from nl2sql_agent.nodes.m6_plan_validation import validate_plan
from nl2sql_agent.nodes.m5b_plan_generation import make_plan_generation_node
from nl2sql_agent.services.plan_normalizer import normalize_structural_coverage
from nl2sql_agent.services.semantic_parser import build_semantic_graph
from nl2sql_agent.state import NL2SQLState, QueryPlan, SchemaHit, SemanticGraph, SemanticPredicate


def test_positive_exists_parent_is_attached_to_join_and_covered(deps):
    graph = build_semantic_graph(
        "统计贷款金额超过1000且有逾期的客户",
        deps.loader.load("business_predicates.yaml"),
    )
    raw = QueryPlan(
        target_tables=["loan", "customer"],
        join_logic=[{
            "left_table": "loan", "left_column": "CUST_ID",
            "right_table": "customer", "right_column": "CUST_ID",
            "source_atom_ids": [],
        }],
        filters=[
            {
                "table": "loan", "column": "LOAN_AMT", "operator": ">", "value": 1000,
                "source_atom_ids": ["atom_1"],
            },
            {
                "table": "loan", "column": "OVD_BAL", "operator": ">", "value": 0,
                "source_atom_ids": ["atom_2_status"],
            },
        ],
        covered_atom_ids=["atom_1", "atom_2_status"],
    )

    normalized, changes = normalize_structural_coverage(raw, graph)

    assert normalized.join_logic[0].source_atom_ids == ["atom_2"]
    assert set(normalized.covered_atom_ids) == {"atom_1", "atom_2", "atom_2_status"}
    assert changes == ["atom_2 自动绑定到关联操作"]
    schema = [
        SchemaHit(table_name="loan", columns=[
            {"name": "CUST_ID"}, {"name": "LOAN_AMT"}, {"name": "OVD_BAL"},
        ]),
        SchemaHit(table_name="customer", columns=[{"name": "CUST_ID"}]),
    ]
    assert validate_plan(
        normalized, schema, deps.term_mapping, ["risk_mart"], semantic_graph=graph
    ) == []


def test_negative_exists_is_never_inferred():
    graph = SemanticGraph(predicate=SemanticPredicate(
        atom_id="missing_event",
        predicate_type="not_exists",
        children=[SemanticPredicate(
            atom_id="status_filter", predicate_type="status",
            concept="逾期", operator="=", value=True,
        )],
    ))
    raw = QueryPlan(
        target_tables=["customer"],
        filters=[{
            "table": "customer", "column": "STATUS", "operator": "=", "value": True,
            "source_atom_ids": ["status_filter"],
        }],
        covered_atom_ids=["status_filter"],
    )

    normalized, changes = normalize_structural_coverage(raw, graph)

    assert changes == []
    assert normalized.covered_atom_ids == ["status_filter"]
    assert "missing_event" not in normalized.filters[0].source_atom_ids


def test_confirmed_text_value_binding_overrides_model_filter_value():
    graph = SemanticGraph(predicate=SemanticPredicate(
        atom_id="address_filter", predicate_type="comparison",
        concept="户籍地址", operator="=", value="上海",
    ))
    raw = QueryPlan(
        target_tables=["customer"],
        filters=[{
            "table": "customer", "column": "HHDIST", "operator": "=", "value": "上海",
            "source_atom_ids": ["address_filter"],
        }],
    )

    normalized, changes = normalize_structural_coverage(
        raw,
        graph,
        semantic_bindings={
            "address_filter": {
                "table_name": "customer", "column_name": "HHDIST",
                "operator": "=", "value": "上海市",
            },
        },
    )

    assert normalized.filters[0].value == "上海市"
    assert changes == ["address_filter 已采用确认的 Schema 字段和值绑定"]
    schema = [SchemaHit(table_name="customer", columns=[{"name": "HHDIST"}])]
    assert validate_plan(
        normalized,
        schema,
        term_mapping=None,
        data_scope=["test"],
        semantic_graph=graph,
        semantic_bindings={
            "address_filter": {
                "table_name": "customer", "column_name": "HHDIST",
                "operator": "=", "value": "上海市",
            },
        },
    ) == []


def test_plan_generation_node_normalizes_exists_before_validation(deps):
    query = "统计贷款金额超过1000且有逾期的客户信息"
    graph = build_semantic_graph(query, deps.loader.load("business_predicates.yaml"))
    deps.llm.add_plan(query, {
        "target_tables": ["loan", "customer"],
        "join_logic": [{
            "left_table": "loan", "left_column": "CUST_ID",
            "right_table": "customer", "right_column": "CUST_ID",
            "source_atom_ids": [],
        }],
        "filters": [
            {
                "table": "loan", "column": "LOAN_AMT", "operator": ">", "value": 1000,
                "source_atom_ids": ["atom_1"],
            },
            {
                "table": "loan", "column": "OVD_BAL", "operator": ">", "value": 0,
                "source_atom_ids": ["atom_2_status"],
            },
        ],
        "covered_atom_ids": ["atom_1", "atom_2_status"],
    })
    state = NL2SQLState(
        user_query=query, user_id="u1", data_scope=["risk_mart"], semantic_graph=graph,
        retrieved_schema=[
            SchemaHit(table_name="loan", columns=[
                {"name": "CUST_ID"}, {"name": "LOAN_AMT"}, {"name": "OVD_BAL"},
            ]),
            SchemaHit(table_name="customer", columns=[{"name": "CUST_ID"}]),
        ],
    )

    out = make_plan_generation_node(deps)(state)

    assert out["query_plan"].join_logic[0].source_atom_ids == ["atom_2"]
    assert out["plan_normalizations"] == ["atom_2 自动绑定到关联操作"]
