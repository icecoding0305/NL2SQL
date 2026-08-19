"""Consistent terminal state classification for query executions."""

from __future__ import annotations

from typing import Any


def finalize_query_state(raw_state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    state = dict(raw_state)
    status = state.get("terminal_status") or (
        "blocked" if state.get("blocked_reason") else "done"
    )
    if status == "done" and not state.get("generated_sql"):
        status = "blocked"
        state["blocked_reason"] = state.get("blocked_reason") or "未生成可执行 SQL"
        state["final_answer"] = state.get("final_answer") or (
            "未能将问题完整绑定到当前数据库 Schema，因此没有生成或执行 SQL。"
        )
    return state, status
