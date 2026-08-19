from nl2sql_agent.services.query_store import QueryStore


def test_delete_query_removes_record_and_feedback(tmp_path):
    store = QueryStore(tmp_path / "history.db")
    store.save_query("t1", user_id="u1", user_query="测试问题", status="done")
    store.add_feedback("t1", "sql_generation", "sql_wrong", "测试反馈")

    assert store.delete_query("t1") is True
    assert store.get_query("t1") is None
    assert store.list_feedbacks("t1") == []
    assert store.delete_query("t1") is False


def test_conversation_groups_turns_and_keeps_first_question_as_title(tmp_path):
    store = QueryStore(tmp_path / "conversations.db")
    store.save_query(
        "t1", conversation_id="c1", user_id="u1",
        user_query="查询逾期客户", status="done", created_at="2026-08-11T10:00:00",
    )
    store.save_query(
        "t2", conversation_id="c1", user_id="u1",
        user_query="再返回地址", status="running", created_at="2026-08-11T10:01:00",
    )
    store.save_query(
        "t3", conversation_id="c2", user_id="u1",
        user_query="新会话", status="done", created_at="2026-08-11T09:00:00",
    )

    assert [turn["trace_id"] for turn in store.list_conversation("c1")] == ["t1", "t2"]
    summaries = store.list_conversations(user_id="u1")
    assert [item["conversation_id"] for item in summaries] == ["c1", "c2"]
    assert summaries[0]["title"] == "查询逾期客户"
    assert summaries[0]["trace_id"] == "t2"
    assert summaries[0]["turn_count"] == 2
    assert summaries[0]["status"] == "running"
    assert "retrieved_schema" not in summaries[0]


def test_delete_conversation_removes_all_turns_and_feedback(tmp_path):
    store = QueryStore(tmp_path / "delete-conversation.db")
    store.save_query("t1", conversation_id="c1", user_query="第一问", status="done")
    store.save_query("t2", conversation_id="c1", user_query="追加问题", status="done")
    store.add_feedback("t2", "sql_generation", "wrong", "反馈")

    assert store.delete_conversation("c1") == ["t1", "t2"]
    assert store.list_conversation("c1") == []
    assert store.list_feedbacks("t2") == []


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


def test_delete_conversation_api_cleans_every_checkpoint(tmp_path, monkeypatch):
    import asyncio

    from nl2sql_agent import api

    store = QueryStore(tmp_path / "api-conversation.db")
    store.save_query("t1", conversation_id="c1", user_query="第一问", status="done")
    store.save_query("t2", conversation_id="c1", user_query="追问", status="done")

    class Checkpointer:
        deleted: list[str] = []

        def delete_thread(self, trace_id: str):
            self.deleted.append(trace_id)

    checkpointer = Checkpointer()
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "_checkpointer", checkpointer)

    result = asyncio.run(api.api_delete_conversation("c1"))

    assert result["trace_ids"] == ["t1", "t2"]
    assert checkpointer.deleted == ["t1", "t2"]


def test_query_store_persists_selected_database(tmp_path):
    store = QueryStore(tmp_path / "database-history.db")

    store.save_query(
        "t-db",
        conversation_id="c-db",
        user_id="u1",
        user_query="查询逾期客户",
        data_scope=["risk_mart"],
        database_id="database-a",
        status="done",
    )

    record = store.get_query("t-db")
    assert record is not None
    assert record["database_id"] == "database-a"
