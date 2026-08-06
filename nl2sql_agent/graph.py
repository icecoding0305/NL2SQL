"""LangGraph 查询编排与可恢复重试路由。

路由与两条重试回路的边界(必须严格遵守):
  模块1 → 模块2(问题理解/改写) → 模块3 → 模块5b
                  └─[关键业务信息缺失]→ 结束并提示补充
  模块3 ─[字段口径歧义]→ 业务语义确认 → 回模块3
        └─[物理表近分]→ 系统自动规划，不要求用户选表
  模块5b → 模块6 ─(不过)→ 回模块5b(上限 max_plan_retries)
                    └─(通过)→ 模块7
  模块7 → 模块8 ─(不过,非危险)→ 回模块7(上限 max_retries)
                └─(通过)→ 模块9 ─(approval_required)→ 人工确认→ 模块10
                                ├─(hard_block)→ 结束
                                └─(pass)→ 模块10
  模块10 ─(报错/结果为空)→ 回模块7(上限 max_retries)
          └─(成功)→ 模块11

- 计划校验失败(模块6)只在模块5b内部打转,不退回模块3
- 执行报错(模块10)只退回模块7,不退回模块5b
- 每种错误在离它最近的节点被消化,不允许一次报错把整条链路从头推倒
"""

from __future__ import annotations

import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from nl2sql_agent.services.checkpoint import checkpoint_serializer
from nl2sql_agent.nodes import (
    human_review,
    m1_entry,
    m10_sandbox_execution,
    m11_result_interpretation,
    m2_query_resolution,
    m3_5_retrieval_confidence_router,
    m3_schema_retrieval,
    m5b_plan_generation,
    m6_plan_validation,
    m7_sql_generation,
    m8_static_validation,
    m9_sensitive_check,
)
from nl2sql_agent.services.deps import Deps
from nl2sql_agent.state import NL2SQLState


def _to_serializable(x):
    """把 pydantic 对象/嵌套结构转成可 JSON 序列化的 dict。"""
    from pydantic import BaseModel

    if isinstance(x, BaseModel):
        return x.model_dump()
    if isinstance(x, dict):
        return {k: _to_serializable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_to_serializable(v) for v in x]
    return x


def _event_data(data) -> dict:
    """节点返回数据精简并序列化后推给前端(execution_result 截断避免刷屏)。"""
    if not isinstance(data, dict):
        return {"value": _to_serializable(data)}
    out = _to_serializable(data)
    if isinstance(out.get("execution_result"), list):
        rows = out["execution_result"]
        out["execution_result"] = rows[:20]
        out["result_total"] = len(rows)
    return out


def _emit(sink, event: str, node: str, trace_id: str, data=None) -> None:
    if sink is None:
        return
    try:
        sink(
            {
                "event": event,
                "node": node,
                "trace_id": trace_id,
                **({"data": _event_data(data)} if data is not None else {}),
            }
        )
    except Exception:  # noqa: BLE001 - 事件推送失败不影响流程执行
        pass


def _traced(name: str, fn, sink=None):
    """记录节点延迟/顺序到 state,并通过 event_sink 推送 node_start / node_complete。"""

    def wrapped(state: NL2SQLState):
        _emit(sink, "node_start", name, state.trace_id)
        t0 = time.perf_counter()
        out = fn(state)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if isinstance(out, dict):
            out = dict(out)
            out["node_latencies"] = {**(state.node_latencies or {}), name: latency_ms}
            out["trace_steps"] = [*(state.trace_steps or []), name]
        _emit(sink, "node_complete", name, state.trace_id, out)
        return out

    return wrapped


def _retry_route(route_fn, sink, retry_node: str, reason_getter):
    """包装 retry 路由:决定重试时推送 retry 事件(attempt + reason)。"""

    def route(state: NL2SQLState) -> str:
        out = route_fn(state)
        if out == "retry" and sink is not None:
            try:
                sink(
                    {
                        "event": "retry",
                        "node": retry_node,
                        "trace_id": state.trace_id,
                        "data": {"attempt": state.retry_count, "reason": reason_getter(state)},
                    }
                )
            except Exception:  # noqa: BLE001
                pass
        return out

    return route


# ---------------- 路由函数 ----------------

def route_clarify(state: NL2SQLState) -> str:
    return "proceed" if not state.need_clarification else "need_info"


def route_plan_validation(state: NL2SQLState) -> str:
    if not state.plan_validation_errors:
        return "pass"
    if state.plan_retry_count < state.max_plan_retries:
        return "retry"
    return "give_up"


def route_static_validation(state: NL2SQLState) -> str:
    if state.blocked_reason:
        return "blocked"  # 危险操作,直接结束,不进入重试
    if not state.validation_errors:
        return "pass"
    if state.retry_count < state.max_retries:
        return "retry"
    return "give_up"


def route_sensitive(state: NL2SQLState) -> str:
    return state.risk_decision


def route_human_review(state: NL2SQLState) -> str:
    return "approved" if state.human_approved else "rejected"


def route_sandbox(state: NL2SQLState) -> str:
    if not state.execution_error:
        return "success"
    if state.retry_count < state.max_retries:
        return "retry"
    return "give_up"


# ---------------- 图构建 ----------------

def build_graph(deps: Deps, checkpointer=None, event_sink=None):
    """编译 LangGraph。

    event_sink:可选同步回调,接收节点事件(dict),用于 WebSocket 流式推送。
    每次查询调用一次 build_graph 并绑定专属 sink(避免跨查询串流)。
    """
    g = StateGraph(NL2SQLState)

    def t(name, fn):
        return _traced(name, fn, event_sink)

    g.add_node("entry", t("entry", m1_entry.make_entry_node(deps)))
    g.add_node("query_resolution", t("query_resolution", m2_query_resolution.make_query_resolution_node(deps)))
    g.add_node("schema_retrieval", t("schema_retrieval", m3_schema_retrieval.make_schema_retrieval_node(deps)))
    g.add_node("clarify_business", t("clarify_business", m3_5_retrieval_confidence_router.make_clarify_business_node(deps)))
    g.add_node("clarify_low_confidence", t("clarify_low_confidence", m3_5_retrieval_confidence_router.make_clarify_low_confidence_node(deps)))
    g.add_node("plan_generation", t("plan_generation", m5b_plan_generation.make_plan_generation_node(deps)))
    g.add_node("plan_validation", t("plan_validation", m6_plan_validation.make_plan_validation_node(deps)))
    g.add_node("sql_generation", t("sql_generation", m7_sql_generation.make_sql_generation_node(deps)))
    g.add_node("static_validation", t("static_validation", m8_static_validation.make_static_validation_node(deps)))
    g.add_node("sensitive_check", t("sensitive_check", m9_sensitive_check.make_sensitive_check_node(deps)))
    g.add_node("human_review", t("human_review", human_review.human_review_node))
    g.add_node("sandbox_execution", t("sandbox_execution", m10_sandbox_execution.make_sandbox_execution_node(deps)))
    g.add_node("result_interpretation", t("result_interpretation", m11_result_interpretation.make_result_interpretation_node(deps)))

    # 模块1 → 模块2(时间范围检查)
    g.add_edge(START, "entry")
    g.add_edge("entry", "query_resolution")

    # 模块2 →(缺时间范围)→ END;否则 → 模块3
    g.add_conditional_edges(
        "query_resolution",
        route_clarify,
        {"need_info": END, "proceed": "schema_retrieval"},
    )

    # 模块3 → 模块3.5(检索后置信度路由)
    g.add_conditional_edges(
        "schema_retrieval",
        m3_5_retrieval_confidence_router.make_route_after_retrieval(deps),
        {
            "clarify_business": "clarify_business",
            "clarify_low_confidence": "clarify_low_confidence",
            "plan_generation": "plan_generation",
        },
    )
    # 只澄清业务口径；用户不会接触物理表/字段绑定。
    g.add_edge("clarify_business", "schema_retrieval")
    # 低置信澄清:继续 → 统一计划路径(带 low_confidence_flag);不继续 → 结束
    g.add_conditional_edges(
        "clarify_low_confidence",
        m3_5_retrieval_confidence_router.route_after_low_confidence,
        {"need_info": END, "proceed": "plan_generation"},
    )

    # 所有查询统一经过模块5b/6，不再用脆弱的复杂度分类绕过计划。
    g.add_edge("plan_generation", "plan_validation")
    g.add_conditional_edges(
        "plan_validation",
        _retry_route(
            route_plan_validation,
            event_sink,
            "plan_generation",
            lambda s: (s.plan_validation_errors or ["计划校验失败"])[-1],
        ),
        {"pass": "sql_generation", "retry": "plan_generation", "give_up": END},
    )

    # 模块7 → 模块8 → 通过→ 模块9;不过(非危险)→ 回7(上限 max_retries);危险→ END
    g.add_edge("sql_generation", "static_validation")
    g.add_conditional_edges(
        "static_validation",
        _retry_route(
            route_static_validation,
            event_sink,
            "sql_generation",
            lambda s: (s.validation_errors or ["静态校验失败"])[-1],
        ),
        {"pass": "sensitive_check", "retry": "sql_generation", "blocked": END, "give_up": END},
    )

    # 模块9 → 可审批风险→ human_review;硬风险→ END;无风险→ 模块10
    g.add_conditional_edges(
        "sensitive_check",
        route_sensitive,
        {
            "approval_required": "human_review",
            "hard_block": END,
            "pass": "sandbox_execution",
        },
    )

    # 人工确认 → 通过→ 模块10;拒绝→ END
    g.add_conditional_edges(
        "human_review",
        route_human_review,
        {"approved": "sandbox_execution", "rejected": END},
    )

    # 模块10 → 成功→ 模块11;报错/空→ 回7(上限 max_retries);打满→ END
    g.add_conditional_edges(
        "sandbox_execution",
        _retry_route(
            route_sandbox,
            event_sink,
            "sql_generation",
            lambda s: s.execution_error or "执行失败",
        ),
        {"success": "result_interpretation", "retry": "sql_generation", "give_up": END},
    )

    # 模块11 → END
    g.add_edge("result_interpretation", END)

    return g.compile(
        # 复用 checkpoint 序列化器(注册全部 state 模型,避免 msgpack 反序列化告警)
        checkpointer=checkpointer
        or InMemorySaver(serde=checkpoint_serializer()),
        interrupt_before=["human_review", "clarify_low_confidence"],
    )
