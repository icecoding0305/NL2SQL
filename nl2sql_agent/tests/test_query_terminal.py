from nl2sql_agent.services.query_terminal import finalize_query_state


def test_no_sql_cannot_finish_as_done():
    state, status = finalize_query_state({"final_answer": "字段证据不足"})
    assert status == "blocked"
    assert state["blocked_reason"] == "未生成可执行 SQL"
    assert state["final_answer"] == "字段证据不足"


def test_successful_sql_query_remains_done():
    state, status = finalize_query_state({"generated_sql": "SELECT 1"})
    assert status == "done"
    assert "blocked_reason" not in state


def test_explicit_terminal_status_is_preserved():
    state, status = finalize_query_state({"terminal_status": "cancelled"})
    assert status == "cancelled"
    assert "blocked_reason" not in state
