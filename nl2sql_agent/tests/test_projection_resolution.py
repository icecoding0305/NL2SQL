from nl2sql_agent.services.projection_resolver import (
    _candidate_fields,
    _validated_decision,
    materialize_projection_decision,
    vague_projection_request,
)
from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.services.schema_planner import build_schema_plan, rank_field_candidates
from nl2sql_agent.state import (
    IntentSlot,
    PlannedTable,
    ProjectionDecision,
    ProjectionFieldSelection,
    QueryIntent,
    SchemaPlan,
    SemanticGraph,
    SemanticOutput,
    SemanticSubject,
)


def _raw_decision(*columns: str) -> ProjectionDecision:
    return ProjectionDecision(
        request="基本信息",
        target_entity="客户",
        understood_description="返回客户姓名、电话和地址",
        selected_fields=[
            ProjectionFieldSelection(
                table_name="customer",
                column_name=column,
                business_label=column,
                reason="属于客户基本信息",
            )
            for column in columns
        ],
        confidence=0.9,
    )


def _candidates():
    return [
        {"table_name": "customer", "column_name": "CUST_ID", "business_label": "客户编号", "primary_key": True},
        {"table_name": "customer", "column_name": "NAME", "business_label": "姓名", "primary_key": False},
        {"table_name": "customer", "column_name": "PHONE", "business_label": "联系电话", "primary_key": False},
        {"table_name": "customer", "column_name": "ADDRESS", "business_label": "居住地址", "primary_key": False},
        {"table_name": "customer", "column_name": "IDNUM", "business_label": "证件号码", "primary_key": False},
    ]


def test_vague_projection_validates_fields_and_excludes_unrequested_identifier():
    decision = _validated_decision(
        _raw_decision("CUST_ID", "NAME", "PHONE", "ADDRESS", "IDNUM"),
        request="基本信息",
        target_entity="客户",
        query="查询有逾期客户的基本信息",
        candidates=_candidates(),
    )

    assert [item.column_name for item in decision.selected_fields] == [
        "CUST_ID", "NAME", "PHONE", "ADDRESS",
    ]
    assert any(item.business_label == "证件号码" for item in decision.excluded_fields)


def test_primary_key_alone_cannot_satisfy_basic_information():
    decision = _validated_decision(
        _raw_decision("CUST_ID"),
        request="基本信息",
        target_entity="客户",
        query="查询客户基本信息",
        candidates=_candidates(),
    )

    assert decision.selected_fields == []
    assert "仅返回实体主键" in decision.excluded_fields[0].reason


def test_projection_decision_becomes_required_outputs_and_schema_columns():
    decision = _raw_decision("NAME", "PHONE", "ADDRESS")
    graph = SemanticGraph(
        subjects=[SemanticSubject(id="subject_1", kind="entity", concept="客户")],
        outputs=[SemanticOutput(
            id="generic_output", subject_id="subject_1", concept="客户基本信息",
            grounding_concept="客户基本信息", source_text="客户的基本信息",
        )],
    )
    intent = QueryIntent(
        query_type="fact_filter",
        entities=[IntentSlot(text="客户", role="entity")],
        attributes=[IntentSlot(text="客户基本信息", role="attribute")],
    )
    plan = SchemaPlan(dimension_tables=[
        PlannedTable(
            table_name="customer",
            role="entity",
            selected_columns=["CUST_ID"],
            reason="客户实体表",
            score=0.9,
        )
    ])

    graph, intent, plan, bindings = materialize_projection_decision(
        decision, graph, intent, plan
    )

    assert [item.concept for item in graph.outputs] == ["NAME", "PHONE", "ADDRESS"]
    assert all(item.required for item in graph.outputs)
    assert [item.text for item in intent.attributes] == ["NAME", "PHONE", "ADDRESS"]
    assert set(plan.dimension_tables[0].selected_columns) == {
        "CUST_ID", "NAME", "PHONE", "ADDRESS",
    }
    assert {item["column_name"] for item in bindings.values()} == {
        "NAME", "PHONE", "ADDRESS",
    }


def test_customer_profile_candidates_exclude_fact_anchor_fields():
    plan = SchemaPlan(
        anchor_tables=[PlannedTable(
            table_name="loan", role="entity", selected_columns=["OVD_BAL"],
            reason="逾期筛选", score=0.9,
        )],
        dimension_tables=[PlannedTable(
            table_name="customer", role="entity", selected_columns=["CUST_ID"],
            reason="客户实体", score=0.9,
        )],
    )
    tables = [
        TableDef("loan", "贷款表", "risk_mart", [
            {"name": "OVD_BAL", "type": "decimal", "comment": "逾期本金余额"},
        ]),
        TableDef("customer", "客户表", "risk_mart", [
            {"name": "NAME", "type": "varchar", "comment": "姓名"},
            {"name": "PHONE", "type": "varchar", "comment": "联系电话"},
        ]),
    ]

    candidates = _candidate_fields(plan, tables)

    assert {(item["table_name"], item["column_name"]) for item in candidates} == {
        ("customer", "NAME"), ("customer", "PHONE"),
    }


def test_entity_qualified_basic_information_is_not_treated_as_missing_field():
    intent = QueryIntent(
        query_type="fact_filter",
        entities=[IntentSlot(text="客户", role="entity")],
        attributes=[IntentSlot(text="客户基本信息", role="attribute")],
    )
    tables = [TableDef("customer", "客户信息表", "risk_mart", [
        {"name": "CUST_ID", "type": "varchar", "comment": "客户编号", "primary_key": True},
        {"name": "NAME", "type": "varchar", "comment": "姓名"},
    ])]

    candidates = rank_field_candidates(intent, tables)
    plan = build_schema_plan(intent, tables, candidates, [])

    assert plan.unresolved_slots == []
    assert plan.dimension_tables[0].table_name == "customer"


def test_broad_semantic_output_keeps_original_topic_for_schema_resolution():
    graph = SemanticGraph(
        subjects=[SemanticSubject(id="customer", kind="entity", concept="客户")],
        outputs=[SemanticOutput(
            id="overdue",
            subject_id="customer",
            concept="逾期笔数",
            grounding_concept="逾期情况",
            source_text="逾期情况",
            aggregation="count_distinct",
            broad=True,
        )],
    )
    intent = QueryIntent(
        query_type="aggregation",
        entities=[IntentSlot(text="客户", role="entity")],
        measures=[IntentSlot(text="逾期笔数", role="measure")],
    )

    assert vague_projection_request(intent, graph) == "逾期情况"
