"""人工确认节点。

通过 interrupt 机制暂停流程,把敏感查询推给人审;恢复时把
Command(resume=...) 的值(如 {"approved": bool, "comment": str})写回 human_approved。
graph 以 compile(..., interrupt_before=["human_review"]) 方式挂载该节点。
"""

from __future__ import annotations

from langgraph.types import interrupt

from nl2sql_agent.state import NL2SQLState


def human_review_node(state: NL2SQLState) -> NL2SQLState | dict:
    payload = {
        "type": "human_review",
        "user_query": state.user_query,
        "sql": state.generated_sql,
        "sensitive_reasons": state.sensitive_reasons,
        "data_scope": state.data_scope,
    }
    decision = interrupt(payload)
    if isinstance(decision, dict):
        approved = bool(decision.get("approved", False))
    else:
        approved = bool(decision)
    out = {"human_approved": approved}
    if not approved:
        out["final_answer"] = "查询未获人工审批，已停止执行。"
    return out
