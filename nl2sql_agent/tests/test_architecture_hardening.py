"""重试反馈、结果语义、风险三态、强类型计划与持久化 checkpoint。"""

from __future__ import annotations

import pytest
from langgraph.types import Command
from pydantic import ValidationError

from nl2sql_agent.graph import build_graph
from nl2sql_agent.nodes.m10_sandbox_execution import make_sandbox_execution_node
from nl2sql_agent.nodes.m5b_plan_generation import build_plan_prompt
from nl2sql_agent.nodes.m6_plan_validation import validate_plan
from nl2sql_agent.nodes.m7_sql_generation import build_prompt_from_query
from nl2sql_agent.nodes.m11_result_interpretation import sanitize_rows_for_llm
from nl2sql_agent.nodes.m9_sensitive_check import make_sensitive_check_node
from nl2sql_agent.services.checkpoint import create_sqlite_checkpointer
from nl2sql_agent.services.executor import InMemoryExecutor
from nl2sql_agent.state import NL2SQLState, QueryPlan, SchemaHit, SchemaPlan
from nl2sql_agent.testing import FAKE_TABLES, build_test_deps

from .conftest import make_input


def _schema(table: str = "dwd_ar_loan_info", columns: tuple[str, ...] = ("LOAN_NO",)):
    return SchemaHit(
        table_name=table,
        columns=[{"name": c, "type": "varchar", "comment": c} for c in columns],
    )


def test_retry_prompts_include_previous_failure(deps):
    sql_state = NL2SQLState(
        **make_input("查询借据"),
        retrieved_schema=[_schema()],
        generated_sql="SELECT BAD_COL FROM dwd_ar_loan_info",
        validation_errors=["字段 BAD_COL 不存在"],
    )
    sql_prompt = build_prompt_from_query(sql_state, deps)
    assert "SELECT BAD_COL" in sql_prompt
    assert "字段 BAD_COL 不存在" in sql_prompt

    plan_state = NL2SQLState(
        **make_input("查询逾期率"),
        retrieved_schema=[_schema(columns=("LOAN_NO", "OVD_BAL"))],
        plan_validation_errors=["引用了不存在的字段 BAD_COL"],
    )
    plan_prompt = build_plan_prompt(plan_state, deps)
    assert "引用了不存在的字段 BAD_COL" in plan_prompt


def test_fact_filter_plan_prompt_forbids_invented_metric(deps):
    from nl2sql_agent.services.schema_planner import parse_query_intent

    state = NL2SQLState(
        **make_input("统计代偿金额超过10000的客户的基本信息"),
        query_intent=parse_query_intent("统计代偿金额超过10000的客户的基本信息"),
        retrieved_schema=[_schema(columns=("DC_ALL_BAL", "LOAN_NO"))],
    )
    prompt = build_plan_prompt(state, deps)
    assert "query_intent.query_type=fact_filter" in prompt
    assert "metric_logic 必须为 null" in prompt
    assert '"metric_name"' in prompt
    assert "禁止使用 name" in prompt


def test_prompts_do_not_expose_data_scope_as_row_filter(deps):
    state = NL2SQLState(
        **make_input("查询借据", data_scope=["risk_mart"]),
        retrieved_schema=[_schema(columns=("LOAN_NO", "PLATFORM_CODE"))],
    )
    sql_prompt = build_prompt_from_query(state, deps)
    plan_prompt = build_plan_prompt(state, deps)
    assert "['risk_mart']" not in sql_prompt
    assert "['risk_mart']" not in plan_prompt
    assert "系统命名空间" in sql_prompt


def test_plan_and_sql_use_clarified_query_and_term_facts(deps):
    state = NL2SQLState(
        **make_input("原始模糊问题"),
        clarified_query="查询贷款金额",
        retrieved_schema=[_schema(columns=("LOAN_AMT",))],
    )
    plan_prompt = build_plan_prompt(state, deps)
    sql_prompt = build_prompt_from_query(state, deps)
    for prompt in (plan_prompt, sql_prompt):
        assert "查询贷款金额" in prompt
        assert "原始模糊问题" not in prompt
        assert "贷款金额(LOAN_AMT)" in prompt
        assert "不可信数据" in prompt


def test_prompt_schema_facts_only_include_query_mschema_relations(deps):
    from nl2sql_agent.services import prompt_context
    state = NL2SQLState(
        **make_input("查询客户借据"),
        retrieved_schema=[
            _schema("loan", ("CUST_ID",)),
            SchemaHit(table_name="customer", columns=[{
                "name": "CUST_ID", "type": "varchar", "comment": "客户编号",
                "primary_key": True,
            }]),
        ],
        schema_plan=SchemaPlan(
            anchor_tables=[{"table_name": "loan", "role": "primary_fact", "selected_columns": ["CUST_ID"]}],
            dimension_tables=[{"table_name": "customer", "role": "entity", "selected_columns": ["CUST_ID"]}],
            relations=[{
                "source_table": "loan", "source_columns": ["CUST_ID"],
                "target_table": "customer", "target_columns": ["CUST_ID"],
                "status": "verified",
            }],
        ),
    )
    facts = prompt_context.schema_facts(state, deps)
    query_schema = facts["query_mschema"]
    assert query_schema["relations"][0]["source_table"] == "loan"
    assert query_schema["tables"][1]["columns"][0]["primary_key"] is True


def test_result_rows_are_masked_before_llm(deps):
    state = NL2SQLState(
        **make_input("查询客户信息"),
        retrieved_schema=[SchemaHit(
            table_name="customer",
            columns=[
                {"name": "IDNUM", "type": "varchar", "comment": "证件号码", "sensitive": True},
                {"name": "AMT", "type": "decimal", "comment": "金额"},
            ],
        )],
        generated_sql="SELECT IDNUM AS cert, AMT FROM customer",
    )
    rows = [{"IDNUM": "110101199001011234", "cert": "110101199001011234", "AMT": 100, "NOTE": "x" * 250}]
    safe = sanitize_rows_for_llm(rows, state, deps)
    assert safe[0]["IDNUM"] == "[已脱敏]"
    assert safe[0]["cert"] == "[已脱敏]"
    assert safe[0]["AMT"] == 100
    assert safe[0]["NOTE"].endswith("…[已截断]")
    assert "110101199001011234" not in str(safe)


def test_enabled_row_filter_without_trusted_values_is_blocked(deps):
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    deps.config.row_level_filter = {"enabled": True, "column": "PLATFORM_CODE"}
    state = NL2SQLState(
        **make_input("查询借据", data_scope=["risk_mart"]),
        generated_sql="SELECT LOAN_NO FROM dwd_ar_loan_info",
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[_schema(columns=("LOAN_NO", "PLATFORM_CODE"))],
    )
    out = make_static_validation_node(deps)(state)
    assert out["blocked_reason"]
    assert "未提供可信权限值" in out["blocked_reason"]


def test_static_validation_rejects_data_scope_used_as_field_value(deps):
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    state = NL2SQLState(
        **make_input("查询借据", data_scope=["risk_mart"]),
        generated_sql=(
            "SELECT LOAN_NO FROM dwd_ar_loan_info "
            "WHERE PLATFORM_CODE = 'risk_mart'"
        ),
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[_schema(columns=("LOAN_NO", "PLATFORM_CODE"))],
    )
    out = make_static_validation_node(deps)(state)
    assert out["retry_count"] == 1
    assert "系统命名空间误作表字段值" in out["validation_errors"][-1]


def test_empty_execution_result_is_success(deps):
    deps.executor = InMemoryExecutor(tables=FAKE_TABLES, empty_result=True)
    state = NL2SQLState(
        **make_input("查询不存在的借据"),
        generated_sql="SELECT LOAN_NO FROM dwd_ar_loan_info",
    )
    out = make_sandbox_execution_node(deps)(state)
    assert out == {"execution_result": [], "execution_error": None}


def test_scan_limit_is_hard_block_not_approval(deps):
    deps.executor = InMemoryExecutor(tables=FAKE_TABLES, explain_rows=1_000_001)
    state = NL2SQLState(
        **make_input("查询借据"),
        generated_sql="SELECT LOAN_NO FROM dwd_ar_loan_info",
        retrieved_schema=[_schema()],
    )
    out = make_sensitive_check_node(deps)(state)
    assert out["risk_decision"] == "hard_block"
    assert out["blocked_reason"]


def test_query_plan_rejects_unknown_operator_and_checks_join_ownership(deps):
    with pytest.raises(ValidationError):
        QueryPlan(
            target_tables=["dwd_ar_loan_info"],
            filters=[{"column": "LOAN_NO", "operator": "execute", "value": "x"}],
        )

    schema = [
        _schema("left_table", ("LEFT_ID",)),
        _schema("right_table", ("RIGHT_ID",)),
    ]
    plan = QueryPlan(
        target_tables=["left_table", "right_table"],
        join_logic=[{
            "left_table": "left_table",
            "right_table": "right_table",
            "left_column": "RIGHT_ID",  # 字段存在，但不属于声明的左表
            "right_column": "RIGHT_ID",
            "join_type": "inner",
        }],
    )
    errors = validate_plan(plan, schema, deps.term_mapping, ["risk_mart"])
    assert any("join 左表 left_table 不存在字段 RIGHT_ID" in e for e in errors)


def test_sqlite_checkpoint_survives_reopen(tmp_path, deps):
    path = tmp_path / "checkpoints.db"
    config = {"configurable": {"thread_id": "persistent-review"}}

    saver1 = create_sqlite_checkpointer(path)
    graph1 = build_graph(deps, checkpointer=saver1)
    graph1.invoke(make_input("查询新信贷的逾期本金"), config)
    assert graph1.get_state(config).next == ("human_review",)
    saver1.conn.close()

    saver2 = create_sqlite_checkpointer(path)
    graph2 = build_graph(deps, checkpointer=saver2)
    result = graph2.invoke(Command(resume={"approved": True}), config)
    assert result["execution_result"] is not None
    saver2.conn.close()
