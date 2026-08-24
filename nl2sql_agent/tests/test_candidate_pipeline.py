from nl2sql_agent.nodes.m7_sql_generation import make_sql_generation_node
from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node
from nl2sql_agent.services.llm import SQLResult
from nl2sql_agent.services.sql_candidate_selector import rank_sql_candidates
from nl2sql_agent.services.sql_compiler import UnsupportedPlanError
from nl2sql_agent.state import NL2SQLState, QueryMSchema, QueryPlan, SchemaHit
from nl2sql_agent.testing import FakeLLM

from .conftest import make_input


def test_deterministic_sql_is_recorded_and_validated_as_candidate(deps):
    plan = QueryPlan(
        target_tables=["customer"],
        output_fields=[{
            "concept": "客户编号", "table": "customer", "column": "CUST_ID",
            "source_output_ids": ["output_1"],
        }],
        output_grain={
            "level": "entity", "entity": "客户", "keys": ["customer.CUST_ID"],
        },
        covered_output_ids=["output_1"],
        confidence=0.91,
    )
    state = NL2SQLState(
        **make_input("查询客户编号"),
        query_plan=plan,
        query_mschema=QueryMSchema(profile="precision"),
        retrieved_schema=[SchemaHit(
            table_name="customer",
            columns=[{"name": "CUST_ID", "type": "varchar", "primary_key": True}],
        )],
    )

    generated = make_sql_generation_node(deps)(state)
    assert generated["query_candidates"][-1].source == "deterministic_compiler"
    assert generated["query_candidates"][-1].status == "compiled"
    assert generated["query_candidates"][-1].selected is True

    validated_state = state.model_copy(update=generated)
    validated = make_static_validation_node(deps)(validated_state)
    assert validated["query_candidates"][-1].status == "validated"


def _filtered_plan():
    return QueryPlan(
        target_tables=["dwd_ar_loan_info"],
        filters=[{
            "table": "dwd_ar_loan_info", "column": "LOAN_AMT",
            "operator": ">", "value": 1000, "source_atom_ids": ["atom_1"],
        }],
        output_fields=[{
            "concept": "贷款金额", "table": "dwd_ar_loan_info",
            "column": "LOAN_AMT", "source_output_ids": ["output_1"],
        }],
        covered_atom_ids=["atom_1"],
        covered_output_ids=["output_1"],
        confidence=0.72,
    )


def test_local_selector_prefers_candidate_preserving_plan_shape():
    state = NL2SQLState(
        **make_input("查询贷款金额超过1000的记录"),
        query_plan=_filtered_plan(),
        retrieved_schema=[SchemaHit(
            table_name="dwd_ar_loan_info",
            columns=[{"name": "LOAN_AMT", "type": "decimal"}],
        )],
    )
    ranked = rank_sql_candidates([
        SQLResult("SELECT LOAN_AMT FROM dwd_ar_loan_info", ["dwd_ar_loan_info"]),
        SQLResult(
            "SELECT LOAN_AMT FROM dwd_ar_loan_info WHERE LOAN_AMT > 1000",
            ["dwd_ar_loan_info"],
        ),
    ], state, "mysql")

    assert "WHERE" in ranked[0][0].sql
    assert ranked[0][1] > ranked[1][1]


def test_model_path_generates_candidates_then_uses_alternative_without_new_call(
    deps, monkeypatch,
):
    fake = FakeLLM()

    def respond(llm):
        if llm.sql_calls == 1:
            return SQLResult(
                "SELECT LOAN_AMT FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )
        return SQLResult(
            "SELECT LOAN_AMT FROM dwd_ar_loan_info WHERE LOAN_AMT > 1000",
            ["dwd_ar_loan_info"],
        )

    fake.add_sql(r"候选策略", respond)
    deps.llm = fake
    deps.sql_llm = fake
    monkeypatch.setattr(
        "nl2sql_agent.nodes.m7_sql_generation.compile_query_plan",
        lambda *args, **kwargs: (_ for _ in ()).throw(UnsupportedPlanError("complex")),
    )
    state = NL2SQLState(
        **make_input("查询贷款金额超过1000的记录"),
        query_plan=_filtered_plan(),
        retrieved_schema=[SchemaHit(
            table_name="dwd_ar_loan_info",
            columns=[{"name": "LOAN_AMT", "type": "decimal"}],
        )],
    )

    generated = make_sql_generation_node(deps)(state)
    sql_candidates = [item for item in generated["query_candidates"] if item.stage == "sql"]
    assert fake.sql_calls == 2
    assert len(sql_candidates) == 2
    assert sql_candidates[0].source == "model_sql_candidate"
    assert next(item for item in sql_candidates if item.selected).sql.endswith("LOAN_AMT > 1000")

    failed_candidates = [
        item.model_copy(update={
            "status": "execution_error" if item.selected else item.status,
            "selected": item.selected,
        })
        for item in generated["query_candidates"]
    ]
    retry_state = state.model_copy(update={
        **generated,
        "query_candidates": failed_candidates,
        "retry_count": 1,
        "execution_error": "执行报错",
    })
    retry = make_sql_generation_node(deps)(retry_state)

    assert fake.sql_calls == 2
    assert retry["generated_sql"] == "SELECT LOAN_AMT FROM dwd_ar_loan_info"

    exhausted = [
        item.model_copy(update={"status": "rejected", "selected": False})
        for item in retry["query_candidates"]
    ]
    refine_state = state.model_copy(update={
        **retry,
        "query_candidates": exhausted,
        "retry_count": 2,
        "validation_errors": ["WHERE 条件缺失"],
    })
    refined = make_sql_generation_node(deps)(refine_state)

    assert fake.sql_calls == 3
    assert refined["query_candidates"][-1].source == "model_sql_refiner"
    assert refined["query_candidates"][-1].selected is True
