from nl2sql_agent.nodes.m6_plan_validation import make_plan_validation_node, validate_plan
from nl2sql_agent.nodes.m2_query_resolution import _prefer_complete_graph
from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node
from nl2sql_agent.services.plan_normalizer import normalize_structural_coverage
from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.services.schema_planner import (
    build_schema_plan,
    ground_output_bindings,
    rank_field_candidates,
)
from nl2sql_agent.services.semantic_parser import (
    build_semantic_graph,
    semantic_graph_to_query_intent,
)
from nl2sql_agent.state import FieldCandidate, NL2SQLState, QueryPlan, SchemaHit, SemanticGraph, SemanticOutput


QUERY = "计算代偿金额超过1000的客户的姓名和地址"


def _graph_and_intent():
    graph = build_semantic_graph(QUERY)
    return graph, semantic_graph_to_query_intent(graph, QUERY)


def test_explicit_result_items_become_required_semantic_outputs():
    graph, intent = _graph_and_intent()

    assert [output.concept for output in graph.outputs] == ["姓名", "地址"]
    assert all(output.required for output in graph.outputs)
    assert [slot.text for slot in intent.attributes] == ["姓名", "地址"]


def test_overdue_customer_name_and_address_keeps_both_explicit_outputs():
    query = "统计有逾期的客户姓名及地址"
    graph = build_semantic_graph(query)

    assert [output.concept for output in graph.outputs] == ["客户姓名", "地址"]
    assert all(output.required for output in graph.outputs)


def test_model_canonical_names_do_not_duplicate_outputs_or_weaken_overdue_predicate(deps):
    query = "统计有逾期的客户姓名及地址"
    fallback = build_semantic_graph(
        query, deps.loader.load("business_predicates.yaml")
    )
    candidate = SemanticGraph.model_validate({
        "subjects": [{"id": "1", "kind": "entity", "concept": "客户"}],
        "outputs": [
            {"id": "out1", "subject_id": "1", "concept": "客户姓名", "grounding_concept": "customer.name", "source_text": "姓名"},
            {"id": "out2", "subject_id": "1", "concept": "客户地址", "grounding_concept": "customer.address", "source_text": "地址"},
        ],
        "predicate": {
            "atom_id": "atom1", "predicate_type": "exists", "subject_id": "1",
            "concept": "存在逾期", "source_text": "有逾期", "materiality": "high",
        },
    })

    merged = _prefer_complete_graph(candidate, fallback)

    assert [item.source_text for item in merged.outputs] == ["客户姓名", "地址"]
    assert merged.predicate is not None
    assert any(item.predicate_type == "status" for item in merged.predicate.children)


def test_output_slots_drive_schema_projection_and_physical_bindings():
    graph, intent = _graph_and_intent()
    tables = [
        TableDef("claim", "代偿明细表", "risk_mart", [
            {"name": "LOAN_NO", "type": "varchar", "comment": "借据编号"},
            {"name": "DC_ALL_BAL", "type": "decimal", "comment": "代偿金额", "semantic_role": "measure"},
        ]),
        TableDef("customer", "客户信息表", "risk_mart", [
            {"name": "CUST_ID", "type": "varchar", "comment": "客户编号", "primary_key": True},
            {"name": "NAME", "type": "varchar", "comment": "姓名"},
            {"name": "RESIADDR", "type": "varchar", "comment": "地址"},
        ]),
    ]
    candidates = rank_field_candidates(intent, tables)
    plan = build_schema_plan(intent, tables, candidates, [])
    bindings = ground_output_bindings(graph, candidates)

    customer = next(table for table in plan.dimension_tables if table.table_name == "customer")
    assert {"NAME", "RESIADDR"} <= set(customer.selected_columns)
    assert {
        output_id: (binding["table_name"], binding["column_name"])
        for output_id, binding in bindings.items()
    } == {
        "output_1": ("customer", "NAME"),
        "output_2": ("customer", "RESIADDR"),
    }


def test_grouping_entity_prefers_non_null_primary_identifier():
    graph = SemanticGraph(
        outputs=[SemanticOutput(
            id="customer_output",
            subject_id="customer",
            concept="客户",
            grounding_concept="客户",
            source_text="客户",
        )],
        group_by=["客户"],
    )
    candidates = [
        FieldCandidate(
            table_name="customer", column_name="CORE_NO",
            column_comment="源系统客户编码", query_slot="客户", final_score=0.9,
        ),
        FieldCandidate(
            table_name="customer", column_name="CUST_ID",
            column_comment="ECIF客户编号", query_slot="客户", final_score=0.8,
        ),
    ]
    tables = [TableDef("customer", "客户信息", "risk", [
        {"name": "CORE_NO", "comment": "源系统客户编码", "nullable": True},
        {
            "name": "CUST_ID", "comment": "ECIF客户编号",
            "primary_key": True, "unique": True, "nullable": False,
        },
    ])]

    binding = ground_output_bindings(graph, candidates, tables=tables)["customer_output"]

    assert binding["column_name"] == "CUST_ID"


def test_plan_cannot_pass_when_explicit_outputs_are_missing(deps):
    graph, _ = _graph_and_intent()
    schema = [
        SchemaHit(table_name="claim", columns=[
            {"name": "CUST_ID"}, {"name": "DC_ALL_BAL"},
        ]),
        SchemaHit(table_name="customer", columns=[
            {"name": "CUST_ID"}, {"name": "NAME"}, {"name": "RESIADDR"},
        ]),
    ]
    bindings = {
        "output_1": {"table_name": "customer", "column_name": "NAME"},
        "output_2": {"table_name": "customer", "column_name": "RESIADDR"},
    }
    incomplete = QueryPlan(
        target_tables=["claim", "customer"],
        join_logic=[{
            "left_table": "claim", "left_column": "CUST_ID",
            "right_table": "customer", "right_column": "CUST_ID",
        }],
        filters=[{
            "table": "claim", "column": "DC_ALL_BAL", "operator": ">", "value": 1000,
            "source_atom_ids": ["atom_1"],
        }],
        output_fields=[{
            "concept": "客户编号", "table": "customer", "column": "CUST_ID",
        }],
        covered_atom_ids=["atom_1"],
    )

    errors = validate_plan(
        incomplete, schema, deps.term_mapping, ["risk_mart"],
        semantic_graph=graph, output_bindings=bindings,
    )

    assert any("遗漏用户明确要求的返回内容" in error for error in errors)

    complete = QueryPlan.model_validate({**incomplete.model_dump(),
        "output_fields": [
            {"concept": "姓名", "table": "customer", "column": "NAME", "source_output_ids": ["output_1"]},
            {"concept": "地址", "table": "customer", "column": "RESIADDR", "source_output_ids": ["output_2"]},
        ],
        "covered_output_ids": ["output_1", "output_2"],
    })
    assert validate_plan(
        complete, schema, deps.term_mapping, ["risk_mart"],
        semantic_graph=graph, output_bindings=bindings,
    ) == []


def test_output_trace_ids_are_normalized_only_for_matching_bindings():
    graph, _ = _graph_and_intent()
    raw = QueryPlan(
        target_tables=["customer"],
        output_fields=[
            {"concept": "姓名", "table": "customer", "column": "NAME"},
            {"concept": "地址", "table": "customer", "column": "RESIADDR"},
        ],
    )
    normalized, changes = normalize_structural_coverage(raw, graph, {
        "output_1": {"table_name": "customer", "column_name": "NAME"},
        "output_2": {"table_name": "customer", "column_name": "RESIADDR"},
    })

    assert normalized.covered_output_ids == ["output_1", "output_2"]
    assert normalized.output_fields[0].source_output_ids == ["output_1"]
    assert normalized.output_fields[1].source_output_ids == ["output_2"]
    assert len(changes) == 3
    assert changes[-1] == "按用户语义输出契约重建返回字段"


def test_sql_validation_rejects_missing_planned_projection(deps):
    plan = QueryPlan(
        target_tables=["customer"],
        output_fields=[
            {"concept": "姓名", "table": "customer", "column": "NAME"},
            {"concept": "地址", "table": "customer", "column": "RESIADDR"},
        ],
    )
    state = NL2SQLState(
        user_query="查询客户姓名和地址",
        user_id="u1",
        data_scope=["risk_mart"],
        query_plan=plan,
        generated_sql="SELECT customer.CUST_ID FROM customer",
        used_tables=["customer"],
        retrieved_schema=[SchemaHit(table_name="customer", columns=[
            {"name": "CUST_ID"}, {"name": "NAME"}, {"name": "RESIADDR"},
        ])],
    )

    out = make_static_validation_node(deps)(state)

    assert any("SQL SELECT 遗漏计划返回字段 customer.NAME" in error for error in out["validation_errors"])
    assert any("SQL SELECT 遗漏计划返回字段 customer.RESIADDR" in error for error in out["validation_errors"])


def test_truncated_plan_is_terminal_error_without_graph_retry(deps):
    state = NL2SQLState(
        user_query="统计有逾期的客户姓名及地址",
        user_id="u1",
        plan_generation_error_kind="output_truncated",
        plan_validation_errors=["计划生成失败（模型输出被截断）"],
    )

    out = make_plan_validation_node(deps)(state)

    assert out["plan_retry_count"] == state.max_plan_retries
    assert out["terminal_status"] == "error"
    assert "输出被截断" in out["final_answer"]


def test_broad_address_output_expands_only_customer_own_full_addresses(deps):
    graph = SemanticGraph(outputs=[SemanticOutput(
        id="output_1", subject_id="subject_1", concept="地址", grounding_concept="地址",
        source_text="地址", required=True, confidence=0.9,
    )])
    candidates = [
        FieldCandidate(table_name="customer", column_name="HOUSEADD", column_comment="户籍地址", query_slot="地址", final_score=0.8, phrase_coverage=1.0),
        FieldCandidate(table_name="customer", column_name="MAILADDR", column_comment="通讯地址", query_slot="地址", final_score=0.79, phrase_coverage=1.0),
        FieldCandidate(table_name="customer", column_name="RESIADDR", column_comment="居住地址", query_slot="地址", final_score=0.78, phrase_coverage=1.0),
        FieldCandidate(table_name="customer", column_name="WORK_ADDR", column_comment="单位地址", query_slot="地址", final_score=0.78, phrase_coverage=1.0),
        FieldCandidate(table_name="customer", column_name="SPOUSE_ADDR", column_comment="配偶地址", query_slot="地址", final_score=0.77, phrase_coverage=1.0),
        FieldCandidate(table_name="customer", column_name="RESI_PROV", column_comment="居住地址省份", query_slot="地址", final_score=0.76, phrase_coverage=1.0),
    ]
    binding = ground_output_bindings(graph, candidates)["output_1"]

    assert binding["binding_mode"] == "expanded"
    assert [item["column_name"] for item in binding["bindings"]] == [
        "HOUSEADD", "MAILADDR", "RESIADDR",
    ]

    raw = QueryPlan(
        target_tables=["customer"],
        output_fields=[
            {"concept": "地址", "table": "customer", "column": "HOUSEADD"},
            {"concept": "地址", "table": "customer", "column": "MAILADDR"},
            {"concept": "地址", "table": "customer", "column": "RESIADDR"},
        ],
    )
    normalized, _ = normalize_structural_coverage(
        raw, graph, {"output_1": binding}
    )
    assert [field.alias for field in normalized.output_fields] == [
        "户籍地址", "通讯地址", "居住地址",
    ]
    assert all(field.source_output_ids == ["output_1"] for field in normalized.output_fields)

    schema = [SchemaHit(table_name="customer", columns=[
        {"name": "HOUSEADD"}, {"name": "MAILADDR"}, {"name": "RESIADDR"},
    ])]
    incomplete = QueryPlan(
        target_tables=["customer"],
        output_fields=[
            {"concept": "户籍地址", "table": "customer", "column": "HOUSEADD", "source_output_ids": ["output_1"]},
            {"concept": "通讯地址", "table": "customer", "column": "MAILADDR", "source_output_ids": ["output_1"]},
        ],
        covered_output_ids=["output_1"],
    )
    errors = validate_plan(
        incomplete, schema, deps.term_mapping, ["risk_mart"],
        semantic_graph=graph, output_bindings={"output_1": binding},
    )
    assert any("customer.RESIADDR" in error for error in errors)
