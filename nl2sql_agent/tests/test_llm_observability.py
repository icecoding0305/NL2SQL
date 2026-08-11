from __future__ import annotations

from nl2sql_agent.graph import _traced
from nl2sql_agent.services.deps import Deps
from nl2sql_agent.services.llm import DeepSeekLLMClient
from nl2sql_agent.services.llm_telemetry import begin_capture, end_capture
from nl2sql_agent.services.query_store import QueryStore
from nl2sql_agent.state import NL2SQLState
from nl2sql_agent.testing import FakeLLM


class _Message:
    content = '{"answer": 1}'


class _Completions:
    def create(self, **kwargs):
        usage = type("Usage", (), {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
        })()
        choice = type("Choice", (), {"message": _Message()})()
        return type("Response", (), {"id": "req-test", "choices": [choice], "usage": usage})()


class _Client:
    chat = type("Chat", (), {"completions": _Completions()})()


def test_deepseek_call_metadata_is_captured_without_content():
    client = DeepSeekLLMClient(_Client(), model="deepseek-v4-flash")
    tokens = begin_capture("query_resolution", "trace-1")
    try:
        assert client.complete("secret prompt") == '{"answer": 1}'
    finally:
        calls = end_capture(tokens)

    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == "deepseek-v4-flash"
    assert call["node"] == "query_resolution"
    assert call["request_id"] == "req-test"
    assert call["prompt_tokens"] == 12
    assert call["completion_tokens"] == 7
    assert call["total_tokens"] == 19
    assert "secret prompt" not in str(call)


def test_repeated_node_latencies_are_accumulated_and_preserved():
    state = NL2SQLState(user_query="q", user_id="u", trace_id="trace-2")
    node = _traced("plan_generation", lambda _state: {})
    first = node(state)
    state = state.model_copy(update=first)
    second = node(state)

    assert len(second["node_latency_history"]["plan_generation"]) == 2
    assert second["node_latencies"]["plan_generation"] == round(
        sum(second["node_latency_history"]["plan_generation"]), 2
    )


def test_query_store_persists_llm_observability_fields(tmp_path):
    store = QueryStore(tmp_path / "observability.db")
    store.save_query("trace-3", user_id="u", user_query="q")
    store.update_query(
        "trace-3",
        node_latency_history={"plan_generation": [10.0, 20.0]},
        llm_calls=[{"model": "deepseek-v4-flash", "duration_ms": 10.0}],
    )
    row = store.get_query("trace-3")
    assert row["node_latency_history"] == {"plan_generation": [10.0, 20.0]}
    assert row["llm_calls"][0]["model"] == "deepseek-v4-flash"


def test_model_routes_distinguish_configured_sql_model_from_compiler():
    flash = FakeLLM()
    flash.model = "deepseek-v4-flash"
    sql_model = FakeLLM()
    sql_model.model = "qwen-sql"
    deps = Deps(
        config=None,
        loader=None,
        llm=flash,
        sql_llm=sql_model,
        node_llms={"query_resolution": flash, "plan_generation": flash},
        term_mapping=None,
        catalog=None,
        vector_store=None,
        executor=None,
        few_shot=None,
        sql=None,
        prompts=None,
    )
    routes = deps.model_routes()
    assert routes["query_resolution"]["model"] == "deepseek-v4-flash"
    assert routes["plan_generation"]["model"] == "deepseek-v4-flash"
    assert routes["sql_model"]["model"] == "qwen-sql"
    assert routes["sql_generation"]["provider"] == "deterministic"
