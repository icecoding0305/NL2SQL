from nl2sql_agent.services.query_store import QueryStore


def test_delete_query_removes_record_and_feedback(tmp_path):
    store = QueryStore(tmp_path / "history.db")
    store.save_query("t1", user_id="u1", user_query="测试问题", status="done")
    store.add_feedback("t1", "sql_generation", "sql_wrong", "测试反馈")

    assert store.delete_query("t1") is True
    assert store.get_query("t1") is None
    assert store.list_feedbacks("t1") == []
    assert store.delete_query("t1") is False


def test_delete_query_api_blocks_active_and_cleans_checkpoint(tmp_path, monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from nl2sql_agent import api

    store = QueryStore(tmp_path / "api-history.db")
    store.save_query("done", user_query="完成", status="done")
    store.save_query("running", user_query="处理中", status="running")

    class Checkpointer:
        deleted: list[str] = []

        def delete_thread(self, trace_id: str):
            self.deleted.append(trace_id)

    checkpointer = Checkpointer()
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "_checkpointer", checkpointer)

    result = asyncio.run(api.api_delete_query("done"))
    assert result["status"] == "deleted"
    assert checkpointer.deleted == ["done"]

    try:
        asyncio.run(api.api_delete_query("running"))
    except HTTPException as error:
        assert error.status_code == 409
    else:
        raise AssertionError("运行中的会话不应允许删除")
