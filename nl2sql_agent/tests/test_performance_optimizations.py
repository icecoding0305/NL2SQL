import pytest
import sqlglot
from sqlglot import exp

from nl2sql_agent.nodes.m2_query_resolution import _should_use_llm_resolution
from nl2sql_agent.nodes.m5b_plan_generation import build_plan_prompt
from nl2sql_agent.nodes.m7_sql_generation import build_prompt_from_plan, build_prompt_from_query
from nl2sql_agent.services.deps import build_deps
from nl2sql_agent.services.executor import InMemoryExecutor
from nl2sql_agent.services.sql_compiler import UnsupportedPlanError, compile_query_plan
from nl2sql_agent.testing import FakeLLM
from nl2sql_agent.state import NL2SQLState, QueryPlan, ResolvedQuery, SchemaHit, SchemaPlan


def _state(query: str = "统计贷款金额超过1000的客户信息"):
    return NL2SQLState(user_query=query, user_id="u1", data_scope=["risk_mart"])


def test_high_confidence_structured_resolution_skips_llm_but_complex_marker_uses_it():
    resolved = ResolvedQuery(
        original_query="统计贷款金额超过1000的客户信息",
        rewritten_query="统计贷款金额超过1000的客户信息",
        query_type="fact_filter",
        confidence=0.8,
    )
    config = {"use_llm": "auto", "auto_min_confidence": 0.7, "llm_required_markers": ["最新"]}
    assert not _should_use_llm_resolution(_state(), resolved, config)

    latest = resolved.model_copy(update={"original_query": "每个客户最新一笔贷款"})
    assert _should_use_llm_resolution(_state(latest.original_query), latest, config)


def test_explicit_detail_plan_compiles_without_sql_llm():
    plan = QueryPlan(
        target_tables=["loan", "customer"],
        join_logic=[{
            "left_table": "loan", "left_column": "CUST_ID",
            "right_table": "customer", "right_column": "CUST_ID",
        }],
        filters=[
            {"table": "loan", "column": "LOAN_AMT", "operator": ">", "value": 1000},
            {"table": "loan", "column": "OVD_BAL", "operator": ">", "value": 0},
        ],
        output_fields=[
            {"concept": "客户编号", "table": "customer", "column": "CUST_ID"},
            {"concept": "客户名称", "table": "customer", "column": "NAME"},
        ],
        output_grain={"level": "entity", "entity": "客户", "keys": ["customer.CUST_ID"]},
    )

    sql, used_tables = compile_query_plan(plan, "mysql")
    tree = sqlglot.parse_one(sql, read="mysql")
    assert isinstance(tree, exp.Select)
    assert tree.args.get("distinct") is not None
    assert {table.name for table in tree.find_all(exp.Table)} == {"loan", "customer"}
    assert used_tables == ["loan", "customer"]
    assert "OVD_BAL" in sql and "LOAN_AMT" in sql


def test_plan_without_explicit_outputs_uses_compatibility_fallback():
    with pytest.raises(UnsupportedPlanError):
        compile_query_plan(QueryPlan(target_tables=["loan"]), "mysql")


def test_main_model_is_independent_from_configured_sql_model(monkeypatch):
    main_model = FakeLLM()
    sql_model = FakeLLM()
    monkeypatch.setattr("nl2sql_agent.services.deps.build_llm", lambda config=None: main_model)
    deps = build_deps(
        sql_llm=sql_model,
        executor=InMemoryExecutor(tables={}),
        vector_store=object(),
    )
    assert deps.llm is main_model
    assert deps.sql_llm is sql_model
    assert deps.config.approval_enabled is False


def test_unified_runtime_reuses_main_model_for_sql_fallback(monkeypatch):
    main_model = FakeLLM()
    main_model.model = "configured-model"
    monkeypatch.setattr(
        "nl2sql_agent.services.deps.build_llm", lambda config=None: main_model
    )
    deps = build_deps(
        executor=InMemoryExecutor(tables={}),
        vector_store=object(),
    )
    assert deps.llm is main_model
    assert deps.sql_llm is main_model
    assert deps.node_llms == {}
    assert deps.model_routes()["sql_model"]["source"] == "runtime.unified"


def test_runtime_prompts_never_include_complete_retrieved_schema(deps):
    state = _state()
    state.retrieved_schema = [
        SchemaHit(table_name="loan", columns=[
            {"name": "CUST_ID", "type": "varchar", "comment": "客户编号"},
            {"name": "LOAN_AMT", "type": "decimal", "comment": "贷款金额"},
            {"name": "FULL_SCHEMA_SECRET", "type": "varchar", "comment": "不得进入提示词"},
        ]),
        SchemaHit(table_name="unplanned_table", columns=[
            {"name": "UNPLANNED_SECRET", "type": "varchar", "comment": "不得进入提示词"},
        ]),
    ]
    state.schema_plan = SchemaPlan(
        anchor_tables=[{
            "table_name": "loan", "role": "primary_fact",
            "selected_columns": ["CUST_ID", "LOAN_AMT"],
        }],
    )

    for prompt in (build_plan_prompt(state, deps), build_prompt_from_query(state, deps)):
        assert "CUST_ID" in prompt and "LOAN_AMT" in prompt
        assert "FULL_SCHEMA_SECRET" not in prompt
        assert "UNPLANNED_SECRET" not in prompt
        assert '"query_mschema"' in prompt


def test_validated_plan_sql_fallback_does_not_reinterpret_conversation(deps):
    state = _state("当前问题不应再次解释")
    state.conversation_history = [
        {"role": "user", "content": "previous-conversation-secret"},
    ]
    state.retrieved_schema = [SchemaHit(table_name="loan", columns=[
        {"name": "CUST_ID", "type": "varchar", "comment": "客户编号"},
        {"name": "UNUSED_SECRET", "type": "varchar", "comment": "无关字段"},
    ])]
    state.schema_plan = SchemaPlan(anchor_tables=[{
        "table_name": "loan", "role": "primary_fact",
        "selected_columns": ["CUST_ID"],
    }])
    state.query_plan = QueryPlan(
        target_tables=["loan"],
        output_fields=[{"concept": "客户", "table": "loan", "column": "CUST_ID"}],
        output_grain={"level": "record", "keys": ["loan.CUST_ID"]},
    )

    prompt = build_prompt_from_plan(state, deps)

    assert "previous-conversation-secret" not in prompt
    assert "当前问题不应再次解释" not in prompt
    assert "CUST_ID" in prompt
    assert "UNUSED_SECRET" not in prompt
    assert '"profile": "execution"' in prompt
