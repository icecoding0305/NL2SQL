import sqlglot

from nl2sql_agent.nodes.m3_schema_retrieval import _ground_semantic_atoms
from nl2sql_agent.nodes.m8_static_validation import _plan_shape_errors
from nl2sql_agent.services.deterministic_planner import build_deterministic_query_plan
from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.services.schema_planner import (
    prefer_minimal_table_cover,
    prefer_primary_fact_fields,
)
from nl2sql_agent.state import (
    FieldCandidate, IntentSlot, NL2SQLState, PlannedTable, QueryIntent, SchemaPlan,
    SemanticGraph, SemanticOrder, SemanticOutput, SemanticPredicate,
)


def _binding(table: str, column: str, **extra):
    return {
        "table_name": table,
        "column_name": column,
        "confidence": 0.95,
        "binding_mode": "exact",
        "bindings": [{
            "table_name": table,
            "column_name": column,
            "confidence": 0.95,
            **extra,
        }],
        **extra,
    }


def test_builds_single_table_grouped_top_n_plan():
    graph = SemanticGraph(
        outputs=[
            SemanticOutput(
                id="output_1", subject_id="product", concept="产品",
                grounding_concept="产品", required=True, confidence=0.95,
            ),
            SemanticOutput(
                id="output_2", subject_id="product", concept="贷款总金额",
                grounding_concept="贷款金额", aggregation="sum",
                required=True, confidence=0.95,
            ),
        ],
        group_by=["产品"],
        order_by=[SemanticOrder(concept="贷款总金额", direction="desc")],
        limit=3,
        capabilities=["aggregation", "ordering", "top_n"],
    )
    state = NL2SQLState(
        user_query="按贷款总金额从高到低返回前3个产品",
        user_id="u1",
        data_scope=["risk_mart"],
        semantic_graph=graph,
        retrieval_confidence=0.95,
        schema_plan=SchemaPlan(
            anchor_tables=[PlannedTable(
                table_name="loan", role="primary_fact", score=0.95,
            )],
            confidence=0.95,
        ),
        output_bindings={
            "output_1": _binding("loan", "product_code"),
            "output_2": _binding("loan", "loan_amt"),
        },
    )

    plan = build_deterministic_query_plan(state)

    assert plan is not None
    assert plan.group_by == ["loan.product_code"]
    assert plan.order_by[0].source_output_id == "output_2"
    assert plan.order_by[0].aggregation == "sum"
    assert plan.limit == 3
    assert plan.covered_output_ids == ["output_1", "output_2"]


def test_builds_grounded_filter_and_trusted_one_hop_join():
    graph = SemanticGraph(
        outputs=[SemanticOutput(
            id="output_1", subject_id="customer", concept="客户姓名",
            grounding_concept="客户姓名", required=True, confidence=0.95,
        )],
        predicate=SemanticPredicate(
            atom_id="atom_1", predicate_type="comparison", concept="贷款金额",
            grounding_concept="贷款金额", operator=">", value=1000,
            confidence=0.95,
        ),
    )
    state = NL2SQLState(
        user_query="查询贷款金额超过1000的客户姓名",
        user_id="u1",
        data_scope=["risk_mart"],
        semantic_graph=graph,
        retrieval_confidence=0.95,
        schema_plan=SchemaPlan(
            anchor_tables=[PlannedTable(table_name="loan", role="primary_fact", score=0.95)],
            dimension_tables=[PlannedTable(table_name="customer", role="entity", score=0.95)],
            relations=[{
                "source_table": "loan", "source_columns": ["cust_id"],
                "target_table": "customer", "target_columns": ["cust_id"],
                "status": "verified",
            }],
            confidence=0.95,
        ),
        output_bindings={"output_1": _binding("customer", "name")},
        semantic_bindings={"atom_1": {
            "table_name": "loan", "column_name": "loan_amt", "operator": ">",
            "value": 1000, "confidence": 0.95,
        }},
    )

    plan = build_deterministic_query_plan(state)

    assert plan is not None
    assert plan.target_tables == ["loan", "customer"]
    assert plan.join_logic[0].left_column == "cust_id"
    assert plan.filters[0].source_atom_ids == ["atom_1"]


def test_falls_back_for_low_confidence_or_exists_semantics():
    base = NL2SQLState(
        user_query="查询客户",
        user_id="u1",
        data_scope=["risk_mart"],
        semantic_graph=SemanticGraph(outputs=[SemanticOutput(
            id="output_1", subject_id="customer", concept="客户",
            grounding_concept="客户", required=True, confidence=0.95,
        )]),
        retrieval_confidence=0.6,
        schema_plan=SchemaPlan(
            anchor_tables=[PlannedTable(table_name="customer", role="entity", score=0.95)],
            confidence=0.95,
        ),
        output_bindings={"output_1": _binding("customer", "cust_id")},
    )
    assert build_deterministic_query_plan(base) is None

    exists_state = base.model_copy(deep=True)
    exists_state.retrieval_confidence = 0.95
    exists_state.semantic_graph.predicate = SemanticPredicate(
        atom_id="atom_1", predicate_type="exists", concept="逾期记录", confidence=0.95,
    )
    exists_state.semantic_graph.capabilities = ["existence"]
    assert build_deterministic_query_plan(exists_state) is None


def test_sql_shape_must_preserve_plan_group_order_and_limit():
    state = NL2SQLState(
        user_query="按贷款总金额返回前3个产品",
        user_id="u1",
        data_scope=["risk_mart"],
        query_plan={
            "target_tables": ["loan"],
            "group_by": ["loan.product_code"],
            "order_by": [{
                "concept": "贷款总金额", "table": "loan", "column": "loan_amt",
                "direction": "desc", "source_output_id": "output_2",
            }],
            "limit": 3,
            "output_fields": [
                {"concept": "产品", "table": "loan", "column": "product_code"},
                {"concept": "贷款总金额", "table": "loan", "column": "loan_amt", "aggregation": "sum"},
            ],
        },
    )
    correct = sqlglot.parse_one(
        "SELECT product_code, SUM(loan_amt) AS total FROM loan "
        "GROUP BY product_code ORDER BY total DESC LIMIT 3",
        read="mysql",
    )
    assert _plan_shape_errors(correct, state) == []

    incomplete = sqlglot.parse_one(
        "SELECT product_code, SUM(loan_amt) AS total FROM loan GROUP BY product_code",
        read="mysql",
    )
    errors = _plan_shape_errors(incomplete, state)
    assert any("ORDER BY" in error for error in errors)
    assert any("LIMIT" in error for error in errors)


def test_minimal_table_cover_prefers_one_detail_table_for_all_slots():
    intent = QueryIntent(
        query_type="fact_filter",
        measures=[IntentSlot(text="loan_amount", role="measure")],
        filters=[IntentSlot(text="product_code", role="attribute", operator="=", value="P01")],
        attributes=[
            IntentSlot(text="loan_no", role="attribute"),
            IntentSlot(text="customer_name", role="attribute"),
        ],
    )
    candidates = []
    for slot in ("loan_amount", "product_code", "loan_no", "customer_name"):
        candidates.append(FieldCandidate(
            table_name="loan_detail", column_name=slot, query_slot=slot,
            semantic_role="measure" if slot == "loan_amount" else "dimension",
            final_score=0.90, phrase_coverage=1.0,
        ))
    candidates.extend([
        FieldCandidate(
            table_name="loan_summary", column_name="loan_amount",
            query_slot="loan_amount", semantic_role="measure",
            final_score=0.92, phrase_coverage=1.0,
        ),
        FieldCandidate(
            table_name="application", column_name="product_code",
            query_slot="product_code", semantic_role="dimension",
            final_score=0.92, phrase_coverage=1.0,
        ),
        FieldCandidate(
            table_name="application", column_name="customer_name",
            query_slot="customer_name", semantic_role="dimension",
            final_score=0.92, phrase_coverage=1.0,
        ),
    ])

    reranked = prefer_minimal_table_cover(candidates, intent)
    first_by_slot = {}
    for item in reranked:
        first_by_slot.setdefault(item.query_slot, item.table_name)

    assert set(first_by_slot.values()) == {"loan_detail"}


def test_value_grounding_cannot_escape_planned_table_subgraph():
    state = NL2SQLState(
        user_query="product P01",
        user_id="u1",
        data_scope=["risk_mart"],
        semantic_graph=SemanticGraph(predicate=SemanticPredicate(
            atom_id="atom_1", predicate_type="comparison",
            concept="product_code", grounding_concept="product_code",
            operator="=", value="P01", confidence=0.95,
        )),
    )
    candidates = [
        FieldCandidate(
            table_name="loan_detail", column_name="product_code",
            query_slot="product_code", final_score=0.90, phrase_coverage=1.0,
        ),
        FieldCandidate(
            table_name="product", column_name="product_code",
            query_slot="product_code", final_score=0.95, phrase_coverage=1.0,
        ),
    ]
    tables = [
        TableDef(
            name="loan_detail", comment="", business_line="risk_mart",
            columns=[{"name": "product_code", "type": "varchar", "examples": []}],
        ),
        TableDef(
            name="product", comment="", business_line="risk_mart",
            columns=[{"name": "product_code", "type": "varchar", "examples": ["P01"]}],
        ),
    ]

    bindings = _ground_semantic_atoms(
        state, candidates, tables, allowed_tables={"loan_detail"}
    )

    assert bindings["atom_1"]["table_name"] == "loan_detail"


def test_primary_fact_context_reranks_repeated_dimension_fields():
    candidates = [
        FieldCandidate(
            table_name="application", column_name="product_code",
            query_slot="产品编码", semantic_role="dimension", final_score=0.91,
            phrase_coverage=1.0,
        ),
        FieldCandidate(
            table_name="loan", column_name="product_code",
            query_slot="产品编码", semantic_role="dimension", final_score=0.72,
            phrase_coverage=1.0,
        ),
    ]
    plan = SchemaPlan(anchor_tables=[PlannedTable(
        table_name="loan", role="primary_fact", score=0.95,
    )])

    reranked = prefer_primary_fact_fields(candidates, plan)

    assert reranked[0].table_name == "loan"
    assert "字段位于已确定的主事实表" in reranked[0].evidence
