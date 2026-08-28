"""模块 3.5(检索后置信度路由)验收测试。

覆盖改动规格的 6 条验收标准:
1. 术语库没收录但字段能匹配的新指标 → 不在模块2被拦截,走到检索/3.5(放行或提示,不拒答)
2. 多个相近物理表 → 系统自动进入规划，不要求业务用户选表
3. 字段口径歧义 → 转换成不暴露物理 Schema 的业务选项
4. 低置信 → 用户选继续 → low_confidence_flag=True,模块4强制计划路径、模块9强制人工确认
5. 高置信单一候选(已收录术语如"逾期率")→ 直接放行到模块4
6. 缺时间范围 → 仍在模块2被拦截(行为不受影响)
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver

from nl2sql_agent.graph import build_graph
from nl2sql_agent.services.executor import InMemoryExecutor
from nl2sql_agent.services.llm import SQLResult
from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.testing import FAKE_TABLES, FakeLLM, build_test_deps

from .conftest import make_input


def _add_loan_copy_table(deps) -> None:
    """注入一张与借据表结构相近的共享表,模拟"多相近候选"。"""
    deps.catalog._shared_tables.append(  # noqa: SLF001
        TableDef(
            name="dwd_ar_loan_copy",
            comment="贷款借据信息表(副本)",
            business_line="dwd",
            shared=True,
            columns=[
                {"name": "LOAN_NO", "type": "varchar", "comment": "借据编码"},
                {"name": "OVD_BAL", "type": "decimal", "comment": "逾期本金余额"},
                {"name": "LOAN_AMT", "type": "decimal", "comment": "贷款金额"},
                {"name": "PLATFORM_CODE", "type": "varchar", "comment": "平台代码"},
            ],
        )
    )


# ---------------- 验收 1:新指标不被模块2拦截 ----------------

def test_acceptance_1_new_metric_not_blocked_at_module2(deps):
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "rc-a1"}}
    graph.invoke(make_input("查询逾期的总金额"), cfg)
    snap = graph.get_state(cfg)
    # 模块2(时间范围)没有拦截(术语库没有"逾期总金额"也不能在模块2被拒答)
    assert snap.values.get("need_clarification") is False
    # 低置信由系统自动改写并重新召回，不再暂停让用户选择物理 Schema。
    assert snap.next != ("clarify_low_confidence",)
    assert "schema_retrieval" in snap.values.get("trace_steps", [])
    assert "rewrite_retrieval" in snap.values.get("trace_steps", [])
    assert snap.values.get("retrieval_rewrite_count") == 1


# ---------------- 验收 2:多相近物理表 → 系统自动规划 ----------------

def test_acceptance_2_multi_table_candidates_do_not_ask_user(deps):
    _add_loan_copy_table(deps)
    # This case verifies the non-governed ambiguity path. Production config
    # deliberately makes “逾期本金” a strict single-source term, so disable
    # that independent governance rule for this scenario only.
    entry = deps.term_mapping._global["逾期本金"]  # noqa: SLF001
    entry.strict_preferred_tables = False
    entry.preferred_tables = []
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "rc-a2"}}
    graph.invoke(make_input("查询新信贷的逾期本金"), cfg)
    snap = graph.get_state(cfg)
    assert snap.next != ("clarify_business",)
    cands = snap.values.get("retrieval_candidates") or []
    assert len(cands) > 1  # 候选供内部规划使用，不成为用户选择题
    assert snap.values.get("business_clarification") is None
    assert "plan_generation" in snap.values.get("trace_steps", [])
    assert "complexity_check" not in snap.values.get("trace_steps", [])


# ---------------- 验收 3:物理字段歧义转换为业务选项 ----------------

def test_acceptance_3_field_candidates_become_business_options(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import _business_clarification
    from nl2sql_agent.state import FieldCandidate

    ambiguities = {"逾期本金": [
        FieldCandidate(
            table_name="dwd_ar_loan_info", column_name="OVD_BAL",
            query_slot="逾期本金", final_score=0.9,
        ),
        FieldCandidate(
            table_name="dwd_ar_loan_copy", column_name="OVD_BAL",
            query_slot="逾期本金", final_score=0.88,
        ),
    ]}
    clarification, bindings = _business_clarification(
        deps, ["risk_mart"], ambiguities
    )
    assert clarification is not None
    public_text = str(clarification.model_dump())
    assert "dwd_ar_loan" not in public_text
    assert "OVD_BAL" not in public_text
    assert set(bindings) == {option.id for option in clarification.options}
    assert any("dwd_ar_loan_info.OVD_BAL" == value for value in bindings.values())


def test_identical_business_labels_do_not_become_user_choices(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import _retain_true_business_ambiguities
    from nl2sql_agent.state import FieldCandidate

    ambiguities = {"产品编码": [
        FieldCandidate(
            table_name="dwd_ar_loan_info", column_name="PRD_CODE",
            query_slot="产品编码", final_score=0.91,
        ),
        FieldCandidate(
            table_name="dwd_ev_repay_detail", column_name="PRD_CODE",
            query_slot="产品编码", final_score=0.90,
        ),
    ]}

    assert _retain_true_business_ambiguities(
        deps, ["risk_mart"], ambiguities
    ) == {}


# ---------------- 验收 4:低置信 → 自动改写重检索 → 统一计划 ----------------

def test_acceptance_4_low_confidence_rewrites_without_user_interrupt(deps):
    llm = FakeLLM(
        sql_rules=[(r".*", SQLResult(
            "SELECT LOAN_NO FROM dwd_ar_loan_info", ["dwd_ar_loan_info"],
        ))],
        plan_rules=[(r".*", {
            "target_tables": ["dwd_ar_loan_info"],
            "join_logic": [],
            "filters": [],
            "metric_logic": None,
            "group_by": [],
            "confidence": 0.6,
        })],
    )
    deps = build_test_deps(llm=llm, executor=InMemoryExecutor(tables=FAKE_TABLES))
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "rc-a4"}}
    result = graph.invoke(make_input("查询逾期的总金额"), cfg)
    assert result.get("low_confidence_flag") is True
    assert result.get("retrieval_rewrite_count") == 1
    assert result.get("trace_steps", []).count("schema_retrieval") == 2
    assert "rewrite_retrieval" in result.get("trace_steps", [])
    assert "clarify_low_confidence" not in result.get("trace_steps", [])
    assert "plan_generation" in result.get("trace_steps", [])


# ---------------- 验收 5:高置信单一候选直接放行 ----------------

def test_acceptance_5_high_confidence_pass_through(deps):
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "rc-a5"}}
    graph.invoke(make_input("查询新信贷的逾期率"), cfg)
    snap = graph.get_state(cfg)
    # 术语精确命中 → 高置信 → 不触发任何澄清,直接进入计划
    assert snap.values.get("retrieval_confidence") == 1.0
    trace = snap.values.get("trace_steps", [])
    assert "clarify_low_confidence" not in trace
    assert "clarify_business" not in trace
    assert "plan_generation" in trace
    assert "complexity_check" not in trace


# ---------------- 验收 6:缺时间范围仍在模块2拦截 ----------------

def test_acceptance_6_missing_time_range_still_blocked(deps):
    graph = build_graph(deps, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "rc-a6"}}
    result = graph.invoke(make_input("查询新信贷贷款余额的时间段分布"), cfg)
    assert result.get("need_clarification") is True
    assert result.get("clarification_reason") == "missing_time_range"
    assert "schema_retrieval" not in result.get("trace_steps", [])  # 未进模块3
