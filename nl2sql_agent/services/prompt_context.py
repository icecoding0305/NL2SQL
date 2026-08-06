"""为 Plan/SQL 提示词构造一致、最小且可审计的事实上下文。"""

from __future__ import annotations

import json
from nl2sql_agent.state import NL2SQLState


def effective_query(state: NL2SQLState) -> str:
    return state.clarified_query or state.user_query


def conversation_facts(state: NL2SQLState, max_turns: int = 5) -> list[dict]:
    """把同一会话内既往的<用户问题 + 最终答案>整理成上文,供多轮追问理解指代。

    只取有意义的轮次(问题非空、有答案),最近的靠后。
    """
    turns: list[dict] = []
    history = state.conversation_history or []
    for msg in history:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "assistant" and turns and "answer" not in turns[-1]:
            turns[-1]["answer"] = content
        elif role == "user" and content:
            turns.append({"question": content})
    return turns[-max_turns:]


def term_facts(state: NL2SQLState, deps) -> list[dict]:
    query = effective_query(state)
    facts: list[dict] = []
    for term in deps.term_mapping.extract_terms(query, state.data_scope):
        resolution = deps.term_mapping.resolve(term, state.data_scope)
        if resolution.status.value != "found":
            continue
        entry = resolution.entries[0]
        facts.append({
            "term": entry.term,
            "resolved_fields": entry.resolved_fields,
            "definition": entry.definition,
            "composite_metric": entry.composite_metric,
        })
    return facts


def schema_facts(state: NL2SQLState, deps) -> dict:
    """Compatibility alias that can only return query-scoped Schema facts."""
    return compact_schema_facts(state)


def compact_schema_facts(
    state: NL2SQLState, query_mschema=None, *, include_semantics: bool = True
) -> dict:
    """Return only the query-scoped facts needed by planning and SQL translation.

    This avoids sending the full retrieved tables and the same fields again inside
    Query M-Schema, which materially increases model latency for wide schemas.
    """
    if query_mschema is None:
        from nl2sql_agent.services.logical_planner import build_query_mschema

        # Rebuild from retrieval/SchemaPlan facts instead of trusting a persisted or
        # caller-supplied state projection that may accidentally contain full Schema.
        query_mschema = build_query_mschema(state)
    model = query_mschema.model_dump()
    facts = {"query_mschema": model}
    if include_semantics:
        facts.update({
            "query_intent": state.query_intent.model_dump() if state.query_intent else None,
            "semantic_graph": state.semantic_graph.model_dump() if state.semantic_graph else None,
        })
    return facts


def prompt_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
