"""Consistent terminal state classification for query executions."""

from __future__ import annotations

from typing import Any


def finalize_query_state(raw_state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    state = dict(raw_state)
    explicit = state.get("terminal_status")
    if explicit:
        return state, explicit
    if state.get("blocked_reason"):
        return state, "blocked"
    if state.get("human_approved") is False:
        return state, "rejected"
    if state.get("plan_validation_errors"):
        state["final_answer"] = state.get("final_answer") or "查询计划未通过完整性校验。"
        return state, "error"
    if state.get("validation_errors"):
        state["final_answer"] = state.get("final_answer") or "SQL 未通过静态校验。"
        return state, "error"
    if state.get("execution_error"):
        state["final_answer"] = state.get("final_answer") or str(state["execution_error"])
        return state, "error"
    if not state.get("generated_sql"):
        state["blocked_reason"] = "未生成可执行 SQL"
        state["final_answer"] = state.get("final_answer") or (
            "未能将问题完整绑定到当前数据库 Schema，因此没有生成或执行 SQL。"
        )
        return state, "blocked"
    if state.get("execution_result") is None:
        state["final_answer"] = state.get("final_answer") or "SQL 尚未成功执行。"
        return state, "error"
    status = "done"
    return state, status
