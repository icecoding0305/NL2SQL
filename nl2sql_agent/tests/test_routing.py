"""端到端路由测试:覆盖验收标准 + 两条重试回路的边界不变式。

基于真实借据表 dwd_ar_loan_info;data_scope 为平台代码(PLATFORM_CODE)。
注意:金额字段聚合(如逾期率的 SUM)按敏感规则会触发人工确认,相关用例在
human_review 暂停后 resume 继续。
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from nl2sql_agent.graph import build_graph
from nl2sql_agent.services.executor import InMemoryExecutor
from nl2sql_agent.services.llm import SQLResult
from nl2sql_agent.testing import FAKE_TABLES, FakeLLM, build_test_deps

from .conftest import make_input


def _invoke(graph, input_, thread="t1"):
    return graph.invoke(input_, {"configurable": {"thread_id": thread}})


# ---------------- 验收 1:原简单查询也统一走计划 ----------------

def test_acceptance_1_simple_path_executes(graph):
    result = _invoke(graph, make_input("查询新信贷的借据笔数"))
    assert result.get("query_plan") is not None
    assert result.get("execution_result") is not None
    assert result.get("execution_error") is None
    assert result.get("final_answer")
    # 全链路节点顺序(模块2统一完成问题理解与改写)
    assert result["trace_steps"] == [
        "entry", "query_resolution", "schema_retrieval", "plan_generation",
        "plan_validation", "sql_generation", "static_validation", "sensitive_check",
        "sandbox_execution", "result_interpretation",
    ]


# ---------------- 验收 2:复合口径走计划路径 ----------------

def test_acceptance_2_composite_metric_plan_path(deps):
    llm = FakeLLM(
        sql_rules=[
            (r"逾期率", SQLResult(
                "SELECT SUM(CASE WHEN OVD_BAL > 0 THEN 1 ELSE 0 END)"
                " / COUNT(*) AS overdue_rate FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )),
        ],
        plan_rules=[
            (r"逾期率", {
                "target_tables": ["dwd_ar_loan_info"],
                "join_logic": [],
                "filters": [],
                "metric_logic": {
                    "metric_name": "逾期率",
                    "definition": "逾期借据数 / 总借据数(OVD_BAL>0 视为逾期)",
                    "columns": ["OVD_BAL", "LOAN_STATUS"],
                },
                "group_by": [],
                "confidence": 0.9,
            }),
        ],
    )
    graph = build_graph(build_test_deps(llm=llm), checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "a2"}}
    graph.invoke(make_input("查询新信贷的逾期率"), config)
    # 金额聚合触发敏感 → 停在 human_review
    assert graph.get_state(config).next == ("human_review",)
    result = graph.invoke(Command(resume={"approved": True, "comment": "ok"}), config)
    assert result["query_plan"] is not None
    assert result["plan_validation_errors"] == []          # 通过模块 6
    # metric_logic 的 definition 与配置术语定义一致
    assert result["query_plan"].metric_logic["metric_name"] == "逾期率"
    assert result["query_plan"].metric_logic["definition"] == "逾期借据数 / 总借据数(OVD_BAL>0 视为逾期)"
    assert result.get("execution_result") is not None


# ---------------- 验收 3:字段幻觉 → 退回模块 7 ----------------

def test_acceptance_3_hallucinated_field_retries_sql(deps):
    def bad_then_good():
        def responder(llm):
            if llm.sql_calls == 1:
                return SQLResult("SELECT foo_bar FROM dwd_ar_loan_info", ["dwd_ar_loan_info"])
            return SQLResult("SELECT COUNT(*) AS cnt FROM dwd_ar_loan_info", ["dwd_ar_loan_info"])

        return responder

    llm = FakeLLM(sql_rules=[(r"借据笔数", bad_then_good())])
    graph = build_graph(build_test_deps(llm=llm), checkpointer=InMemorySaver())
    result = _invoke(graph, make_input("查询新信贷的借据笔数"))
    # 第一次生成的 SQL 引用不存在字段 → 模块 8 拦截 → 退回模块 7 → 最终成功
    assert result["retry_count"] == 1
    assert result.get("execution_result") is not None
    trace = result["trace_steps"]
    assert trace.count("sql_generation") == 2
    assert trace.count("static_validation") == 2


# ---------------- 验收 4:系统隔离(业务线按系统维度) ----------------

def test_acceptance_4_system_isolation(graph):
    # 表归属 risk_mart 系统:可访问该系统的用户检索得到
    result = _invoke(graph, make_input("查询新信贷的借据笔数", data_scope=["risk_mart"]), thread="t4")
    assert [h.table_name for h in result["retrieved_schema"]] == ["dwd_ar_loan_info"]
    # 其他系统(dw/core)用户检索不到——系统级隔离
    result2 = _invoke(graph, make_input("查询新信贷的借据笔数", data_scope=["dw"]), thread="t4b")
    assert [h.table_name for h in result2["retrieved_schema"]] == []


# ---------------- 验收 5:敏感 → human_review 暂停 ----------------

def test_acceptance_5_sensitive_pauses_for_human(graph):
    config = {"configurable": {"thread_id": "t5"}}
    graph.invoke(make_input("查询新信贷的逾期本金"), config)
    snap = graph.get_state(config)
    # 停在 human_review,未自动执行
    assert snap.next == ("human_review",)
    assert snap.values.get("execution_result") is None
    assert snap.values.get("is_sensitive") is True

    # 人工同意后才继续执行
    result = graph.invoke(Command(resume={"approved": True, "comment": "ok"}), config)
    assert result.get("execution_result") is not None


def test_acceptance_5_rejected_stops_before_execution(graph):
    config = {"configurable": {"thread_id": "t5r"}}
    graph.invoke(make_input("查询新信贷的逾期本金"), config)
    assert graph.get_state(config).next == ("human_review",)
    result = graph.invoke(Command(resume={"approved": False}), config)
    assert result.get("execution_result") is None
    assert "sandbox_execution" not in result["trace_steps"]


# ---------------- 验收 6:执行报错 → 退回模块 7 ----------------

def test_acceptance_6_execution_error_retries_sql_generation():
    executor = InMemoryExecutor(
        tables=FAKE_TABLES,
        fail_execute_times=1,  # 第一次执行报错
    )
    llm = FakeLLM(sql_rules=[(r"借据笔数", SQLResult(
        "SELECT LOAN_NO FROM dwd_ar_loan_info", ["dwd_ar_loan_info"],
    ))])
    graph = build_graph(build_test_deps(llm=llm, executor=executor), checkpointer=InMemorySaver())
    result = _invoke(graph, make_input("查询新信贷的借据笔数"))
    assert result.get("execution_result") is not None  # 第二次执行成功
    assert result["retry_count"] == 1
    # 关键不变式:退回的是模块 7,而不是模块 5b
    assert result.get("plan_retry_count", 0) == 0
    assert result.get("query_plan") is not None
    trace = result["trace_steps"]
    assert trace.count("plan_generation") == 1
    first_exec = trace.index("sandbox_execution")
    assert trace[first_exec + 1] == "sql_generation"   # 报错后直接回 SQL 生成


# ---------------- 两条回路的边界不变式 ----------------

def test_plan_retry_loop_stays_inside_plan_path():
    # 计划始终引用不存在的表 → 模块 6 校验失败,只在 5b↔6 内打转,不退回模块 3
    llm = FakeLLM(
        plan_rules=[
            (r"逾期率", {
                "target_tables": ["ghost_table"],       # 不在检索结果内
                "join_logic": [],
                "filters": [],
                "metric_logic": None,
                "group_by": [],
                "confidence": 0.5,
            }),
        ],
        sql_rules=[(r"逾期率", SQLResult("SELECT 1 FROM dwd_ar_loan_info", ["dwd_ar_loan_info"]))],
    )
    graph = build_graph(build_test_deps(llm=llm), checkpointer=InMemorySaver())
    result = _invoke(graph, make_input("查询新信贷的逾期率"), thread="tplan")
    assert result["plan_retry_count"] == 2             # 达到 max_plan_retries
    assert result["final_answer"] and "人工介入" in result["final_answer"]
    trace = result["trace_steps"]
    assert trace.count("schema_retrieval") == 1        # 未退回模块 3
    assert trace.count("plan_generation") == 2
    assert "sql_generation" not in trace               # 计划未通过,不进入 SQL 生成


def test_execution_give_up_after_max_retries():
    # 执行始终报错 → 重试打满后降级,避免死循环
    executor = InMemoryExecutor(
        tables={"dwd_ar_loan_info": [{"LOAN_NO": "LN1", "PLATFORM_CODE": "XXD"}]},
        fail_execute_times=100,  # 一直失败
    )
    llm = FakeLLM(sql_rules=[(r"借据笔数", SQLResult(
        "SELECT LOAN_NO FROM dwd_ar_loan_info", ["dwd_ar_loan_info"],
    ))])
    graph = build_graph(build_test_deps(llm=llm, executor=executor), checkpointer=InMemorySaver())
    result = _invoke(graph, make_input("查询新信贷的借据笔数"), thread="tgiveup")
    assert result["retry_count"] == 3                  # max_retries
    assert result["final_answer"] and "人工介入" in result["final_answer"]
    assert result.get("execution_result") is None


def test_dangerous_sql_is_blocked_without_retry(deps):
    llm = FakeLLM(sql_rules=[(r".*", SQLResult("DROP TABLE dwd_ar_loan_info", ["dwd_ar_loan_info"]))])
    graph = build_graph(build_test_deps(llm=llm), checkpointer=InMemorySaver())
    result = _invoke(graph, make_input("查询新信贷的借据笔数"), thread="tdrop")
    assert result["blocked_reason"] == "Drop"
    assert "SQL 被拦截" in result["final_answer"]
    assert result["trace_steps"].count("sql_generation") == 1  # 不进入重试
