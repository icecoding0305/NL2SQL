import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from nl2sql_agent import api
from nl2sql_agent.graph import _traced
from nl2sql_agent.services.query_cancellation import QueryExecutionCancelled
from nl2sql_agent.services.query_store import QueryStore


def test_cancel_api_marks_running_query_and_sets_worker_signal(tmp_path, monkeypatch):
    store = QueryStore(tmp_path / "cancel.db")
    store.save_query("t-running", user_query="slow query", status="running")
    signal = threading.Event()
    monkeypatch.setattr(api, "_store", store)
    monkeypatch.setattr(api, "_active_query_cancellations", {"t-running": signal})

    result = asyncio.run(api.api_cancel_query("t-running"))

    assert result == {"trace_id": "t-running", "status": "cancelled"}
    assert signal.is_set()
    row = store.get_query("t-running")
    assert row["status"] == "cancelled"
    assert row["next_node"] is None
    assert row["final_answer"] == "查询已由用户停止。"


def test_cancel_api_rejects_completed_query(tmp_path, monkeypatch):
    store = QueryStore(tmp_path / "cancel-complete.db")
    store.save_query("t-done", user_query="done", status="done")
    monkeypatch.setattr(api, "_store", store)

    with pytest.raises(HTTPException) as error:
        asyncio.run(api.api_cancel_query("t-done"))

    assert error.value.status_code == 409


def test_graph_stops_before_next_node_when_cancelled():
    called = False

    def node(_state):
        nonlocal called
        called = True
        return {}

    wrapped = _traced("test", node, cancellation_check=lambda: True)

    with pytest.raises(QueryExecutionCancelled):
        wrapped(SimpleNamespace(trace_id="t-cancelled"))
    assert called is False
