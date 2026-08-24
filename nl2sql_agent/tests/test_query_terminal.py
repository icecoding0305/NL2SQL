from nl2sql_agent.services.query_terminal import finalize_query_state


def test_no_sql_cannot_finish_as_done():
    state, status = finalize_query_state({"final_answer": "字段证据不足"})
    assert status == "blocked"
    assert state["blocked_reason"] == "未生成可执行 SQL"
    assert state["final_answer"] == "字段证据不足"


def test_successful_executed_query_remains_done():
    state, status = finalize_query_state({
        "generated_sql": "SELECT 1", "execution_result": [],
    })
    assert status == "done"
    assert "blocked_reason" not in state


def test_generated_but_unvalidated_or_unexecuted_sql_is_not_done():
    _, validation_status = finalize_query_state({
        "generated_sql": "SELECT bad", "validation_errors": ["语法错误"],
    })
    _, execution_status = finalize_query_state({"generated_sql": "SELECT 1"})
    assert validation_status == "error"
    assert execution_status == "error"


def test_explicit_terminal_status_is_preserved():
    state, status = finalize_query_state({"terminal_status": "cancelled"})
    assert status == "cancelled"
    assert "blocked_reason" not in state
