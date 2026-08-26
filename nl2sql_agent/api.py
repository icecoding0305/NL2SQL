"""前端 API 层。

- WebSocket /ws/query:流式推送 pipeline 节点事件(node_start/complete/retry/interrupt/final/error)
- REST /api/*:非流式提交、状态查询、审批、历史、审计、反馈、配置
- 断线重连:带 trace_id 重连可恢复到当前阶段(已完成/待审批/执行中)

执行模型:每次查询在独立线程跑 LangGraph,事件通过 EventStream 桥接到
WebSocket 连接的 asyncio 队列(loop.call_soon_threadsafe)。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from langgraph.types import Command
from pydantic import BaseModel, Field

from nl2sql_agent.graph import build_graph
from nl2sql_agent.services.checkpoint import create_sqlite_checkpointer
from nl2sql_agent.services.database_store import DatabaseConfigStore
from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.deps import CONFIG_DIR, build_deps, load_env
from nl2sql_agent.services.knowledge_management import (
    seed_from_legacy_config,
    validate_knowledge,
)
from nl2sql_agent.services.knowledge_store import KnowledgeStore
from nl2sql_agent.services.query_store import QueryStore
from nl2sql_agent.services.query_cancellation import QueryExecutionCancelled
from nl2sql_agent.services.relation_store import DatabaseRelationStore
from nl2sql_agent.services.query_terminal import finalize_query_state
from nl2sql_agent.services.text_encoding import normalize_query_payload, repair_mojibake
from nl2sql_agent.security import verify_platform_token

router = APIRouter(prefix="/api")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_env()
_deps: dict[str, tuple[str, Any]] = {}
_store = QueryStore(DATA_DIR / "nl2sql.db")
_database_store = DatabaseConfigStore(DATA_DIR / "nl2sql.db", DATA_DIR.parent)
_relation_store = DatabaseRelationStore(DATA_DIR / "nl2sql.db")
_knowledge_store = KnowledgeStore(DATA_DIR / "nl2sql.db")
_knowledge_seeded = False
_knowledge_seed_lock = threading.Lock()
_deps_lock = threading.Lock()
_active_query_cancellations: dict[str, threading.Event] = {}
_active_query_cancellations_lock = threading.Lock()


# 全局共享 checkpointer:查询与审批(resume)必须用同一实例,否则无法恢复线程状态
_checkpointer = create_sqlite_checkpointer(DATA_DIR / "langgraph_checkpoints.db")


def get_deps(database_id: str | None = None):
    """Return dependencies bound to one configured physical database."""
    load_env()
    record = _database_store.get(database_id, include_secret=True)
    if not record:
        raise KeyError(f"数据库配置 {database_id or 'default'} 不存在")
    cache_key = str(record["id"])
    version = str(record.get("updated_at") or "")
    with _deps_lock:
        cached = _deps.get(cache_key)
        if cached and cached[0] == version:
            return cached[1]
        schema_path = _database_store.schema_path(cache_key)
        _ensure_knowledge_seeded()
        knowledge_bundle = _knowledge_store.runtime_bundle(
            cache_key, str(record.get("namespace") or "risk_mart")
        )
        deps = build_deps(
            database_url=_database_store.connection_url(cache_key),
            m_schema_path=schema_path,
            relation_overrides=_relation_store.runtime_relations(cache_key),
            knowledge_bundle=knowledge_bundle,
        )
        _deps[cache_key] = (version, deps)
        return deps


def _invalidate_deps(database_id: str | None = None) -> None:
    with _deps_lock:
        if database_id is None:
            _deps.clear()
        else:
            _deps.pop(database_id, None)


def _query_cancel_event(trace_id: str) -> threading.Event:
    """Return the process-local cancellation signal for one running trace."""
    with _active_query_cancellations_lock:
        return _active_query_cancellations.setdefault(trace_id, threading.Event())


def _release_query_cancel_event(trace_id: str, event: threading.Event) -> None:
    with _active_query_cancellations_lock:
        if _active_query_cancellations.get(trace_id) is event:
            _active_query_cancellations.pop(trace_id, None)


def _query_is_cancelled(trace_id: str, event: threading.Event) -> bool:
    if event.is_set():
        return True
    row = _store.get_query(trace_id)
    return bool(row and row.get("status") == "cancelled")


# ---------------- 事件桥接 ----------------

class EventStream:
    """同步线程 → asyncio 事件循环的桥:图执行线程调 emit,WS 协程 await get。"""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()

    def emit(self, event: dict) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    async def get(self):
        return await self._queue.get()


def _state_to_dict(state: dict) -> dict:
    """把 state(values)转成可 JSON 序列化并落库的字典。"""
    hits = state.get("retrieved_schema") or []
    cands = state.get("retrieval_candidates") or []
    return {
        "user_query": state.get("user_query"),
        "generated_sql": state.get("generated_sql"),
        "plan": (
            state["query_plan"].model_dump()
            if state.get("query_plan") is not None
            else None
        ),
        "logical_plan": (
            state["logical_plan"].model_dump() if state.get("logical_plan") is not None else None
        ),
        "query_mschema": (
            state["query_mschema"].model_dump() if state.get("query_mschema") is not None else None
        ),
        "query_mschema_precision": (
            state["query_mschema_precision"].model_dump()
            if state.get("query_mschema_precision") is not None else None
        ),
        "query_mschema_recall": (
            state["query_mschema_recall"].model_dump()
            if state.get("query_mschema_recall") is not None else None
        ),
        "query_mschema_execution": (
            state["query_mschema_execution"].model_dump()
            if state.get("query_mschema_execution") is not None else None
        ),
        "query_candidates": [
            item.model_dump() for item in (state.get("query_candidates") or [])
        ],
        "retrieved_schema": [h.model_dump() for h in hits],
        "sensitive_reasons": state.get("sensitive_reasons") or [],
        "execution_result": state.get("execution_result"),
        "execution_error": state.get("execution_error"),
        "result_summary": (
            state["result_summary"].model_dump() if state.get("result_summary") else None
        ),
        "final_answer": state.get("final_answer"),
        "trace_steps": state.get("trace_steps") or [],
        "node_latencies": state.get("node_latencies") or {},
        "node_latency_history": state.get("node_latency_history") or {},
        "llm_calls": state.get("llm_calls") or [],
        "retry_count": state.get("retry_count", 0),
        "plan_retry_count": state.get("plan_retry_count", 0),
        "blocked_reason": state.get("blocked_reason"),
        "is_sensitive": state.get("is_sensitive", False),
        "risk_decision": state.get("risk_decision", "pass"),
        "human_approved": state.get("human_approved"),
        # 检索置信度相关(模块 3.5 澄清展示用)
        "retrieval_confidence": state.get("retrieval_confidence"),
        "retrieval_candidates": [h.model_dump() for h in cands],
        "query_intent": state["query_intent"].model_dump() if state.get("query_intent") else None,
        "resolved_query": state["resolved_query"].model_dump() if state.get("resolved_query") else None,
        "semantic_graph": state["semantic_graph"].model_dump() if state.get("semantic_graph") else None,
        "semantic_coverage": state.get("semantic_coverage") or {},
        "business_clarification": (
            state["business_clarification"].model_dump()
            if state.get("business_clarification") else None
        ),
        "decision_summary": (
            state["decision_summary"].model_dump()
            if state.get("decision_summary") else None
        ),
        "projection_decision": (
            state["projection_decision"].model_dump()
            if state.get("projection_decision") else None
        ),
        "field_candidates": [item.model_dump() for item in (state.get("field_candidates") or [])],
        "field_ambiguities": {
            slot: [item.model_dump() for item in options]
            for slot, options in (state.get("field_ambiguities") or {}).items()
        },
        "schema_plan": state["schema_plan"].model_dump() if state.get("schema_plan") else None,
        "clarification_reason": state.get("clarification_reason"),
        "low_confidence_flag": state.get("low_confidence_flag"),
    }


def _persist(
    trace_id: str,
    state: dict,
    status: str,
    approved: bool | None = None,
    next_node: str | None = None,
) -> None:
    d = _state_to_dict(state)
    _store.update_query(
        trace_id,
        status=status,
        user_query=d["user_query"],
        generated_sql=d["generated_sql"],
        plan_json=d["plan"],
        logical_plan=d["logical_plan"],
        query_mschema=d["query_mschema"],
        query_mschema_precision=d["query_mschema_precision"],
        query_mschema_recall=d["query_mschema_recall"],
        query_mschema_execution=d["query_mschema_execution"],
        query_candidates=d["query_candidates"],
        retrieved_schema=d["retrieved_schema"],
        sensitive_reasons=d["sensitive_reasons"],
        execution_result=d["execution_result"],
        result_summary=d["result_summary"],
        final_answer=d["final_answer"],
        trace_steps=d["trace_steps"],
        node_latencies=d["node_latencies"],
        node_latency_history=d["node_latency_history"],
        llm_calls=d["llm_calls"],
        retry_count=d["retry_count"],
        plan_retry_count=d["plan_retry_count"],
        approved=approved,
        next_node=next_node,
        retrieval_confidence=d["retrieval_confidence"],
        retrieval_candidates=d["retrieval_candidates"],
        query_intent=d["query_intent"],
        resolved_query=d["resolved_query"],
        semantic_graph=d["semantic_graph"],
        semantic_coverage=d["semantic_coverage"],
        business_clarification=d["business_clarification"],
        decision_summary=d["decision_summary"],
        projection_decision=d["projection_decision"],
        field_candidates=d["field_candidates"],
        field_ambiguities=d["field_ambiguities"],
        schema_plan=d["schema_plan"],
        clarification_reason=d["clarification_reason"],
        low_confidence_flag=bool(d["low_confidence_flag"]),
        risk_decision=d["risk_decision"],
        finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def _run_query(input_data: dict, trace_id: str, sink, database_id: str | None = None) -> None:
    """同步执行图(线程内),按结果推送 final/interrupt/error 并落库。"""
    cancel_event = _query_cancel_event(trace_id)
    is_cancelled = lambda: _query_is_cancelled(trace_id, cancel_event)
    try:
        if is_cancelled():
            raise QueryExecutionCancelled("query cancelled by user")
        graph = build_graph(
            get_deps(database_id),
            checkpointer=_checkpointer,
            event_sink=sink,
            cancellation_check=is_cancelled,
        )
        config = {"configurable": {"thread_id": trace_id}}
        graph.invoke(input_data, config)
        snap = graph.get_state(config)
        state = dict(snap.values)
        if is_cancelled():
            raise QueryExecutionCancelled("query cancelled by user")
        if snap.next:
            # 停在人工确认 / 候选澄清 / 低置信澄清(节点名与实际暂停节点一致)
            pause_node = snap.next[0]
            sink({"event": "interrupt", "node": pause_node, "trace_id": trace_id,
                  "data": _state_to_dict(state)})
            _persist(trace_id, state, status="pending_review", next_node=pause_node)
        else:
            state, terminal_status = finalize_query_state(state)
            sink({"event": "final", "data": _state_to_dict(state), "trace_id": trace_id})
            _persist(trace_id, state, status=terminal_status, next_node=None)
    except QueryExecutionCancelled:
        _store.update_query(
            trace_id,
            status="cancelled",
            next_node=None,
            final_answer="查询已由用户停止。",
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        sink({"event": "cancelled", "node": None, "trace_id": trace_id})
    except Exception as e:  # noqa: BLE001
        if is_cancelled():
            _store.update_query(
                trace_id,
                status="cancelled",
                next_node=None,
                final_answer="查询已由用户停止。",
                finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            sink({"event": "cancelled", "node": None, "trace_id": trace_id})
            return
        sink({"event": "error", "node": None, "message": str(e), "trace_id": trace_id})
        _store.update_query(trace_id, status="error", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    finally:
        _release_query_cancel_event(trace_id, cancel_event)
        sink({"event": "done", "trace_id": trace_id})


# ---------------- WebSocket:流式查询 ----------------

async def _ws_query_handler(ws: WebSocket, msg: dict) -> None:
    msg = normalize_query_payload(msg)
    trace_id = msg.get("trace_id") or f"t{int(time.time() * 1000)}{secrets.token_hex(3)}"
    conversation_id = msg.get("conversation_id") or trace_id
    database = _database_store.get(msg.get("database_id"))
    if not database:
        await ws.send_json({"event": "error", "trace_id": trace_id, "node": None,
                            "message": "请选择有效的数据库"})
        await ws.close()
        return
    database_id = database["id"]
    if database.get("schema_status") != "ready":
        await ws.send_json({"event": "error", "trace_id": trace_id, "node": None,
                            "message": "所选数据库尚未完成 Schema 同步"})
        await ws.close()
        return
    await ws.send_json({"event": "trace", "trace_id": trace_id, "node": None})

    existing = _store.get_query(trace_id)
    if existing:
        # 断线重连恢复:按当前状态推送,不重新发起查询
        status = existing.get("status")
        if status == "pending_review":
            await ws.send_json({"event": "interrupt", "node": existing.get("next_node") or "human_review",
                                "data": existing, "trace_id": trace_id})
            await ws.close()
            return
        if status in ("done", "blocked", "rejected", "error", "cancelled"):
            await ws.send_json({"event": "final", "data": existing, "trace_id": trace_id})
            await ws.close()
            return
        await ws.send_json({"event": "restore", "data": existing, "trace_id": trace_id})
        await ws.close()
        return

    # 前端发送 user_query,REST 用 user_query,WS 联调用 query —— 都兼容
    user_query = msg.get("user_query") or msg.get("query") or ""
    input_data = {
        "user_query": user_query,
        "user_id": msg.get("user_id", ""),
        "data_scope": [database.get("namespace") or "risk_mart"],
        "conversation_history": msg.get("conversation_history", []),
        "trace_id": trace_id,
    }
    _store.save_query(
        trace_id,
        user_id=input_data["user_id"],
        user_query=input_data["user_query"],
        data_scope=input_data["data_scope"],
        conversation_id=conversation_id,
        database_id=database_id,
    )
    loop = asyncio.get_running_loop()
    stream = EventStream(loop)
    threading.Thread(
        target=_run_query,
        args=(input_data, trace_id, stream.emit, database_id),
        daemon=True,
    ).start()

    while True:
        try:
            event = await asyncio.wait_for(stream.get(), timeout=60)
        except asyncio.TimeoutError:
            await ws.send_json({"event": "ping", "trace_id": trace_id})
            continue
        # 数据库驱动会把 DECIMAL、日期等值作为 Python 原生对象返回。
        # 节点事件必须先转成 JSON 安全类型，否则查询虽已执行成功，
        # WebSocket 仍会在结果展示前因序列化异常而断开。
        await ws.send_json(jsonable_encoder(event))
        if event.get("event") in ("final", "interrupt", "error", "done"):
            break
    await ws.close()


@router.websocket("/ws/query")
async def ws_query(ws: WebSocket):
    await ws.accept()
    try:
        msg = await ws.receive_json()
    except (WebSocketDisconnect, ValueError):
        await ws.close()
        return
    if not verify_platform_token(msg.pop("platform_token", None)):
        await ws.send_json({"event": "error", "node": None, "message": "访问密码无效或已失效"})
        await ws.close(code=4401)
        return
    await _ws_query_handler(ws, msg)


# ---------------- REST ----------------

class QueryRequest(BaseModel):
    user_query: str
    user_id: str
    data_scope: list[str]
    conversation_history: list[dict] = Field(default_factory=list)
    conversation_id: str | None = None
    database_id: str | None = None
    trace_id: str | None = None


@router.post("/query")
async def api_query(req: QueryRequest):
    """非流式提交:同步执行并返回结果或"待审批"状态(供轮询/页面刷新恢复)。"""
    user_query = repair_mojibake(req.user_query)
    conversation_history = normalize_query_payload(req.conversation_history)
    trace_id = req.trace_id or f"t{int(time.time() * 1000)}{secrets.token_hex(3)}"
    conversation_id = req.conversation_id or trace_id
    database = _database_store.get(req.database_id)
    if not database:
        raise HTTPException(400, "请选择有效的数据库")
    database_id = database["id"]
    _store.save_query(
        trace_id,
        conversation_id=conversation_id,
        database_id=database_id,
        user_id=req.user_id,
        user_query=user_query,
        data_scope=[database.get("namespace") or "risk_mart"],
    )

    result: dict = {}

    def _run():
        nonlocal result
        _run_query(
            {
                "user_query": user_query,
                "user_id": req.user_id,
                "data_scope": [database.get("namespace") or "risk_mart"],
                "conversation_history": conversation_history,
                "trace_id": trace_id,
            },
            trace_id,
            sink=lambda e: None,
            database_id=database_id,
        )
        result = _store.get_query(trace_id) or {}

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=180)
    if t.is_alive():
        _store.update_query(trace_id, status="running")
        return {"trace_id": trace_id, "status": "running"}
    return {"trace_id": trace_id, **result}


@router.get("/query/{trace_id}")
async def api_query_status(trace_id: str):
    row = _store.get_query(trace_id)
    if not row:
        raise HTTPException(404, f"trace {trace_id} 不存在")
    return row


@router.post("/query/{trace_id}/cancel")
async def api_cancel_query(trace_id: str):
    """Stop an active query and prevent late worker results from overwriting it."""
    row = _store.get_query(trace_id)
    if not row:
        raise HTTPException(404, f"trace {trace_id} 不存在")
    status = row.get("status")
    if status == "cancelled":
        return {"trace_id": trace_id, "status": "cancelled"}
    if status not in {"running", "pending_review"}:
        raise HTTPException(409, f"当前状态 {status} 的查询不能停止")
    with _active_query_cancellations_lock:
        event = _active_query_cancellations.get(trace_id)
        if event is not None:
            event.set()
    _store.update_query(
        trace_id,
        status="cancelled",
        next_node=None,
        final_answer="查询已由用户停止。",
        finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )
    return {"trace_id": trace_id, "status": "cancelled"}


class ApproveRequest(BaseModel):
    approved: bool
    reason: str = ""
    approver: str = ""


@router.post("/query/{trace_id}/approve")
async def api_approve(trace_id: str, body: ApproveRequest):
    row = _store.get_query(trace_id)
    if not row:
        raise HTTPException(404, f"trace {trace_id} 不存在")
    if not get_deps(row.get("database_id")).config.approval_enabled:
        raise HTTPException(404, "查询审批功能已临时关闭")
    if row.get("status") != "pending_review":
        raise HTTPException(400, f"当前状态 {row.get('status')} 不可审批(需 pending_review)")
    # 先标记为进行中,避免前端把旧的 pending_review 误判成"又停在澄清"
    _store.update_query(trace_id, status="running")

    def _resume():
        print(f"[approve] start trace={trace_id}", flush=True)
        try:
            graph = build_graph(
                get_deps(row.get("database_id")),
                checkpointer=_checkpointer,
                event_sink=None,
            )
            config = {"configurable": {"thread_id": trace_id}}
            graph.invoke(Command(resume={"approved": body.approved, "comment": body.reason}), config)
            snap = graph.get_state(config)
            state = snap.values
            next_node = snap.next[0] if snap.next else None
            if snap.next:
                _persist(trace_id, state, status="pending_review", next_node=next_node)
            else:
                terminal_status = "done" if body.approved else "rejected"
                _persist(trace_id, state, status=terminal_status, approved=body.approved, next_node=None)
                if body.approver:
                    _store.update_query(trace_id, approver=body.approver)
            print(f"[approve] done trace={trace_id} next={snap.next}", flush=True)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            _store.update_query(trace_id, status="error", execution_error=str(e))

    threading.Thread(target=_resume, daemon=True).start()
    return {"trace_id": trace_id, "status": "resumed"}


class ResumeRequest(BaseModel):
    resume: dict = {}   # 澄清节点的恢复值,如 {"table": "..."} / {"continue": true}
    approver: str = ""


@router.post("/query/{trace_id}/resume")
async def api_resume(trace_id: str, body: ResumeRequest):
    """通用恢复:候选澄清(选定表)、低置信澄清(是否继续)等任意 interrupt 的 resume。"""
    row = _store.get_query(trace_id)
    if not row:
        raise HTTPException(404, f"trace {trace_id} 不存在")
    if row.get("status") != "pending_review":
        raise HTTPException(400, f"当前状态 {row.get('status')} 不可恢复(需 pending_review)")
    # 先标记为进行中,避免前端把旧的 pending_review 误判成"又停在澄清"
    _store.update_query(trace_id, status="running")

    def _resume():
        try:
            graph = build_graph(
                get_deps(row.get("database_id")),
                checkpointer=_checkpointer,
                event_sink=None,
            )
            config = {"configurable": {"thread_id": trace_id}}
            graph.invoke(Command(resume=body.resume), config)
            snap = graph.get_state(config)
            state = snap.values
            next_node = snap.next[0] if snap.next else None
            if snap.next:
                _persist(trace_id, state, status="pending_review", next_node=next_node)
            else:
                terminal_status = (
                    state.get("terminal_status")
                    or ("blocked" if state.get("blocked_reason") else "done")
                )
                _persist(trace_id, state, status=terminal_status, next_node=None)
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            _store.update_query(trace_id, status="error", execution_error=str(e))

    threading.Thread(target=_resume, daemon=True).start()
    return {"trace_id": trace_id, "status": "resumed"}


@router.get("/diagnostics/models")
async def api_model_diagnostics():
    """Expose effective model routing; secrets and endpoint URLs are never returned."""
    return {
        "routes": get_deps().model_routes(),
        "note": "Whether a configured model was actually called is recorded in each query's llm_calls.",
    }


@router.get("/history")
async def api_history(
    user_id: str | None = None,
    business_line: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 200,
):
    return _store.list_queries(user_id, business_line, start_date, end_date, limit)


@router.get("/conversations")
async def api_conversations(user_id: str | None = None, limit: int = 50):
    """Return one sidebar item per multi-turn conversation."""
    return _store.list_conversations(user_id=user_id, limit=limit)


@router.get("/conversation/{conversation_id}")
async def api_conversation(conversation_id: str):
    turns = _store.list_conversation(conversation_id)
    if not turns:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")
    return turns


@router.delete("/conversation/{conversation_id}")
async def api_delete_conversation(conversation_id: str):
    """Delete every completed query turn belonging to one conversation."""
    turns = _store.list_conversation(conversation_id)
    if not turns:
        raise HTTPException(404, f"conversation {conversation_id} 不存在")
    if any(turn.get("status") in {"running", "pending_review"} for turn in turns):
        raise HTTPException(409, "包含运行中或待确认查询的对话不能删除")
    trace_ids = _store.delete_conversation(conversation_id)
    for trace_id in trace_ids:
        try:
            _checkpointer.delete_thread(trace_id)
        except Exception:
            pass
    return {
        "status": "deleted",
        "conversation_id": conversation_id,
        "trace_ids": trace_ids,
    }


@router.delete("/query/{trace_id}")
async def api_delete_query(trace_id: str):
    """删除已结束的会话、反馈和 LangGraph checkpoint。"""
    row = _store.get_query(trace_id)
    if not row:
        raise HTTPException(404, f"trace {trace_id} 不存在")
    if row.get("status") in {"running", "pending_review"}:
        raise HTTPException(409, "运行中或待确认的会话不能删除")
    if not _store.delete_query(trace_id):
        raise HTTPException(404, f"trace {trace_id} 不存在")
    try:
        _checkpointer.delete_thread(trace_id)
    except Exception:  # 查询展示记录已删除；旧版本 checkpointer 不支持时不阻塞
        pass
    return {"status": "deleted", "trace_id": trace_id}


@router.get("/approvals")
async def api_approvals():
    return _store.list_pending_approvals()


@router.get("/audit/{trace_id}")
async def api_audit(trace_id: str):
    row = _store.get_query(trace_id)
    if not row:
        raise HTTPException(404, f"trace {trace_id} 不存在")
    row["feedbacks"] = _store.list_feedbacks(trace_id)
    return row


class FeedbackRequest(BaseModel):
    trace_id: str
    node: str = ""
    feedback_type: str  # plan_wrong / sql_wrong / other
    comment: str = ""


@router.post("/feedback")
async def api_feedback(body: FeedbackRequest):
    _store.add_feedback(body.trace_id, body.node, body.feedback_type, body.comment)
    return {"status": "ok"}


# ---------------- 数据库连接管理 ----------------

class DatabaseConfigInput(BaseModel):
    name: str
    engine: str = "mysql"
    host: str
    port: int = 3306
    database_name: str
    username: str
    password: str = ""
    namespace: str = "risk_mart"
    is_default: bool = False


class DatabaseConfigUpdate(BaseModel):
    name: str | None = None
    engine: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = None
    namespace: str | None = None


class DatabaseRelationInput(BaseModel):
    source_table: str
    source_columns: list[str] = Field(min_length=1)
    target_table: str
    target_columns: list[str] = Field(min_length=1)
    cardinality: str = "many_to_one"
    preferred_join_type: str = "inner"
    description: str = ""
    enabled: bool = True


class DatabaseRelationUpdate(BaseModel):
    source_table: str | None = None
    source_columns: list[str] | None = None
    target_table: str | None = None
    target_columns: list[str] | None = None
    cardinality: str | None = None
    preferred_join_type: str | None = None
    description: str | None = None
    enabled: bool | None = None
    status: str | None = None


class DatabaseRelationBatchDecision(BaseModel):
    relation_ids: list[str] = Field(min_length=1, max_length=200)
    status: Literal["verified", "rejected"]


def _schema_options(database_id: str) -> list[dict]:
    database = _database_store.get(database_id)
    if not database:
        raise HTTPException(404, "数据库配置不存在")
    path = _database_store.schema_path(database_id)
    if not path.exists():
        raise HTTPException(409, "该数据库尚未同步 Schema")
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(500, "M-Schema 文件无法读取") from exc
    return [
        {
            "table_name": table_name,
            "comment": table.get("comment") or table.get("preliminary_description") or "",
            "columns": [
                {
                    "name": column_name,
                    "comment": field.get("comment") or "",
                    "type": field.get("type") or field.get("raw_type") or "",
                }
                for column_name, field in (table.get("fields") or {}).items()
            ],
        }
        for table_name, table in (schema.get("tables") or {}).items()
    ]


def _validate_relation(database_id: str, values: dict, current: dict | None = None) -> dict:
    merged = {**(current or {}), **values}
    source_table = str(merged.get("source_table") or "").strip()
    target_table = str(merged.get("target_table") or "").strip()
    source_columns = list(merged.get("source_columns") or [])
    target_columns = list(merged.get("target_columns") or [])
    if not source_table or not target_table or source_table == target_table:
        raise HTTPException(400, "关系两端必须是不同的表")
    if not source_columns or len(source_columns) != len(target_columns):
        raise HTTPException(400, "关系两端的字段数量必须相同且不能为空")
    tables = {item["table_name"]: item for item in _schema_options(database_id)}
    if source_table not in tables or target_table not in tables:
        raise HTTPException(400, "关系引用了当前数据库中不存在的表")
    source_available = {item["name"] for item in tables[source_table]["columns"]}
    target_available = {item["name"] for item in tables[target_table]["columns"]}
    if not set(source_columns) <= source_available or not set(target_columns) <= target_available:
        raise HTTPException(400, "关系引用了表中不存在的字段")
    if merged.get("cardinality", "many_to_one") not in {
        "one_to_one", "one_to_many", "many_to_one", "many_to_many", "unknown",
    }:
        raise HTTPException(400, "不支持的关系基数")
    if merged.get("preferred_join_type", "inner") not in {"inner", "left"}:
        raise HTTPException(400, "关联方式仅支持 inner 或 left")
    status = str(merged.get("status") or "verified")
    if status not in {"candidate", "inferred", "verified", "confirmed", "rejected"}:
        raise HTTPException(400, "不支持的关系状态")
    validated = {
        key: merged[key]
        for key in (
            "source_table", "source_columns", "target_table", "target_columns",
            "cardinality", "preferred_join_type", "description", "enabled", "status",
        )
        if key in merged
    }
    if status in {"verified", "confirmed"}:
        validated["enabled"] = True
    elif status in {"candidate", "inferred", "rejected"}:
        validated["enabled"] = False
    return validated


@router.get("/databases")
async def api_databases():
    """List selectable databases. Passwords are never returned."""
    return _database_store.list()


@router.post("/databases")
async def api_create_database(body: DatabaseConfigInput):
    if body.engine not in {"mysql", "postgres"}:
        raise HTTPException(400, "仅支持 mysql 或 postgres")
    try:
        record = _database_store.create(body.model_dump())
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "数据库配置已存在") from exc
    return record


@router.put("/databases/{database_id}")
async def api_update_database(database_id: str, body: DatabaseConfigUpdate):
    record = _database_store.update(database_id, body.model_dump(exclude_unset=True))
    if not record:
        raise HTTPException(404, "数据库配置不存在")
    _invalidate_deps(database_id)
    return record


@router.delete("/databases/{database_id}")
async def api_delete_database(database_id: str):
    record = _database_store.get(database_id)
    if not record:
        raise HTTPException(404, "数据库配置不存在")
    if record.get("is_default") and len(_database_store.list()) > 1:
        raise HTTPException(409, "请先将其他数据库设为默认")
    if not _database_store.delete(database_id):
        raise HTTPException(404, "数据库配置不存在")
    _relation_store.delete_for_database(database_id)
    _invalidate_deps(database_id)
    return {"status": "deleted", "id": database_id}


@router.post("/databases/{database_id}/default")
async def api_default_database(database_id: str):
    if not _database_store.set_default(database_id):
        raise HTTPException(404, "数据库配置不存在")
    return {"status": "ok", "id": database_id}


@router.post("/databases/{database_id}/test")
async def api_test_database(database_id: str):
    from nl2sql_agent.services.deps import build_executor_from_url

    try:
        record = _database_store.get(database_id)
        executor = build_executor_from_url(_database_store.connection_url(database_id))
        probe_sql = (
            "SELECT DATABASE() AS database_name"
            if record and record.get("engine") == "mysql"
            else "SELECT current_database() AS database_name"
        )
        rows = executor.execute(probe_sql, timeout_seconds=8)
        return {"status": "ok", "database": rows[0].get("database_name") if rows else None}
    except KeyError as exc:
        raise HTTPException(404, "数据库配置不存在") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"连接失败：{exc}") from exc


@router.post("/databases/{database_id}/sync-schema")
async def api_sync_database_schema(database_id: str):
    record = _database_store.get(database_id)
    if not record:
        raise HTTPException(404, "数据库配置不存在")
    if record.get("schema_status") == "syncing":
        raise HTTPException(409, "Schema 正在同步")
    _database_store.set_schema_status(database_id, "syncing", "正在提取数据库结构")
    _invalidate_deps(database_id)

    def _sync() -> None:
        try:
            from nl2sql_agent.services.config_loader import ConfigLoader
            from nl2sql_agent.services.deps import CONFIG_DIR
            from nl2sql_agent.services.schema_ingest.diff_sync import sync
            from nl2sql_agent.services.schema_ingest.review_queue import ReviewStore

            deps = get_deps(database_id)
            ingest_config = ConfigLoader(CONFIG_DIR).load("schema_ingest.yaml") or {}
            datasource = _database_store.artifact_key(database_id)
            path = _database_store.schema_path(database_id)
            mode = "incremental" if path.exists() else "full"
            report = sync(
                datasource,
                record["database_name"] if record["engine"] == "mysql" else "public",
                deps,
                ingest_config,
                ReviewStore(DATA_DIR / "schema_ingest.db"),
                mode=mode,
                business_line=record["namespace"],
            )
            schema_payload = json.loads(path.read_text(encoding="utf-8"))
            _relation_store.replace_discovered(
                database_id, schema_payload.get("relation_candidates") or []
            )
            message = (
                f"入库 {report.ingested}，待审核 {report.queued}，跳过 {report.skipped}；"
                f"画像 {report.profiled_tables} 表/{report.profiled_columns} 字段，"
                f"跳过 {report.profile_skipped_columns} 字段；"
                f"发现 {report.relation_candidates} 条候选关系"
            )
            _database_store.set_schema_status(database_id, "ready", message)
            _invalidate_deps(database_id)
        except Exception as exc:  # noqa: BLE001
            _database_store.set_schema_status(database_id, "error", str(exc))
            _invalidate_deps(database_id)

    threading.Thread(target=_sync, daemon=True).start()
    return {"status": "syncing", "id": database_id}


@router.get("/databases/{database_id}/schema-options")
async def api_database_schema_options(database_id: str):
    return _schema_options(database_id)


@router.get("/databases/{database_id}/relations")
async def api_database_relations(database_id: str):
    if not _database_store.get(database_id):
        raise HTTPException(404, "数据库配置不存在")
    return _relation_store.list(database_id)


@router.post("/databases/{database_id}/relations/discover")
async def api_discover_database_relations(database_id: str):
    database = _database_store.get(database_id)
    if not database:
        raise HTTPException(404, "数据库配置不存在")
    if database.get("schema_status") != "ready":
        raise HTTPException(409, "请先完成 Schema 同步")

    path = _database_store.schema_path(database_id)
    if not path.exists():
        raise HTTPException(409, "该数据库尚未同步 Schema")
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise HTTPException(500, "M-Schema 文件无法读取") from exc

    from nl2sql_agent.services.schema_ingest.relation_discovery import (
        discover_relation_candidates,
        tables_from_mschema,
    )

    config = ConfigLoader(CONFIG_DIR).load("schema_ingest.yaml") or {}
    discovery = config.get("relation_discovery") or {}
    if not discovery.get("enabled", True):
        raise HTTPException(409, "关系发现功能已关闭")
    datasource = _database_store.artifact_key(database_id)
    overrides = _review_store().overrides(datasource)
    candidates = discover_relation_candidates(
        tables_from_mschema(schema, overrides),
        schema.get("relations") or schema.get("foreign_keys") or [],
        min_confidence=float(discovery.get("min_confidence", 0.68)),
        inferred_confidence=float(discovery.get("inferred_confidence", 0.84)),
        max_candidates=int(discovery.get("max_candidates", 200)),
        allow_profile_inference=bool(discovery.get("allow_profile_inference", True)),
        warehouse_anchor_threshold=float(
            discovery.get("warehouse_anchor_threshold", 0.62)
        ),
        max_edges_per_identifier=int(
            discovery.get("max_edges_per_identifier", 25)
        ),
    )
    stored = _relation_store.replace_discovered(database_id, candidates)
    schema["relation_candidates"] = candidates
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_path.replace(path)
    _invalidate_deps(database_id)
    return {
        "status": "completed",
        "database_id": database_id,
        "discovered": len(candidates),
        "stored": stored,
        "message": f"发现 {len(candidates)} 条待确认关系",
    }


@router.post("/databases/{database_id}/relations")
async def api_create_database_relation(database_id: str, body: DatabaseRelationInput):
    values = _validate_relation(database_id, body.model_dump())
    try:
        relation = _relation_store.create(database_id, values)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "相同的表关系已经存在") from exc
    _invalidate_deps(database_id)
    return relation


@router.post("/databases/{database_id}/relations/batch-decision")
async def api_batch_decide_database_relations(
    database_id: str, body: DatabaseRelationBatchDecision
):
    if not _database_store.get(database_id):
        raise HTTPException(404, "数据库配置不存在")
    updated = _relation_store.decide_many(
        database_id, body.relation_ids, body.status
    )
    if not updated:
        raise HTTPException(409, "所选关系已处理或不属于当前数据库")
    _invalidate_deps(database_id)
    return {
        "status": "completed",
        "decision": body.status,
        "updated_ids": updated,
        "updated": len(updated),
    }


@router.put("/databases/{database_id}/relations/{relation_id}")
async def api_update_database_relation(
    database_id: str, relation_id: str, body: DatabaseRelationUpdate
):
    current = _relation_store.get(relation_id)
    if not current or current.get("database_id") != database_id:
        raise HTTPException(404, "表关系不存在")
    values = _validate_relation(
        database_id, body.model_dump(exclude_unset=True), current=current
    )
    try:
        relation = _relation_store.update(relation_id, values)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "相同的表关系已经存在") from exc
    _invalidate_deps(database_id)
    return relation


@router.delete("/databases/{database_id}/relations/{relation_id}")
async def api_delete_database_relation(database_id: str, relation_id: str):
    current = _relation_store.get(relation_id)
    if not current or current.get("database_id") != database_id:
        raise HTTPException(404, "表关系不存在")
    _relation_store.delete(relation_id)
    _invalidate_deps(database_id)
    return {"status": "deleted", "id": relation_id}


# ---------------- 企业知识管理 ----------------

class KnowledgeItemInput(BaseModel):
    knowledge_type: str
    name: str
    description: str = ""
    database_id: str | None = None
    namespace: str = "global"
    status: str = "draft"
    priority: int = 100
    payload: dict = Field(default_factory=dict)
    created_by: str = "admin"


def _ensure_knowledge_seeded() -> None:
    global _knowledge_seeded
    if _knowledge_seeded:
        return
    with _knowledge_seed_lock:
        if not _knowledge_seeded:
            seed_from_legacy_config(_knowledge_store, ConfigLoader(CONFIG_DIR))
            _knowledge_seeded = True


def _knowledge_schema(database_id: str | None) -> list[dict]:
    if not database_id:
        return []
    return _schema_options(database_id)


def _validate_knowledge_or_raise(values: dict) -> None:
    if values.get("knowledge_type") == "optimization_case":
        payload = values.setdefault("payload", {})
        if payload.get("case_type") == "sql_fallback" and values.get("database_id"):
            database = _database_store.get(values["database_id"])
            if database:
                payload["dialect"] = database.get("engine") or "mysql"
    errors = validate_knowledge(values, _knowledge_schema(values.get("database_id")))
    if errors:
        raise HTTPException(400, {"message": "知识校验失败", "errors": errors})


@router.get("/knowledge/summary")
async def api_knowledge_summary(database_id: str | None = None):
    _ensure_knowledge_seeded()
    return _knowledge_store.summary(database_id)


@router.get("/knowledge/items")
async def api_knowledge_items(
    knowledge_type: str | None = None,
    database_id: str | None = None,
    status: str | None = None,
    q: str | None = None,
):
    _ensure_knowledge_seeded()
    return _knowledge_store.list(
        knowledge_type=knowledge_type,
        database_id=database_id,
        status=status,
        query=q,
    )


@router.post("/knowledge/items")
async def api_create_knowledge(
    body: KnowledgeItemInput,
    admin_token: str | None = Header(default=None),
):
    if not _admin_ok(admin_token):
        raise HTTPException(403, "需要管理权限(Header X-Admin-Token)")
    _ensure_knowledge_seeded()
    values = body.model_dump()
    if values["database_id"] and not _database_store.get(values["database_id"]):
        raise HTTPException(404, "数据库配置不存在")
    if values["status"] == "published":
        _validate_knowledge_or_raise(values)
    try:
        item = _knowledge_store.create(values)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _invalidate_deps(values["database_id"])
    return item


@router.put("/knowledge/items/{item_id}")
async def api_update_knowledge(
    item_id: str,
    body: KnowledgeItemInput,
    admin_token: str | None = Header(default=None),
):
    if not _admin_ok(admin_token):
        raise HTTPException(403, "需要管理权限(Header X-Admin-Token)")
    _ensure_knowledge_seeded()
    current = _knowledge_store.get(item_id)
    if not current:
        raise HTTPException(404, "知识记录不存在")
    values = body.model_dump()
    if values["status"] == "published":
        _validate_knowledge_or_raise(values)
    try:
        item = _knowledge_store.update(item_id, values)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(400, str(exc)) from exc
    old_database = current.get("database_id")
    new_database = values.get("database_id")
    _invalidate_deps(new_database if old_database == new_database else None)
    return item


@router.post("/knowledge/items/{item_id}/publish")
async def api_publish_knowledge(
    item_id: str,
    admin_token: str | None = Header(default=None),
):
    if not _admin_ok(admin_token):
        raise HTTPException(403, "需要管理权限(Header X-Admin-Token)")
    _ensure_knowledge_seeded()
    current = _knowledge_store.get(item_id)
    if not current:
        raise HTTPException(404, "知识记录不存在")
    _validate_knowledge_or_raise(current)
    item = _knowledge_store.update(item_id, {**current, "status": "published"})
    _invalidate_deps(current.get("database_id"))
    return item


@router.delete("/knowledge/items/{item_id}")
async def api_delete_knowledge(
    item_id: str,
    admin_token: str | None = Header(default=None),
):
    if not _admin_ok(admin_token):
        raise HTTPException(403, "需要管理权限(Header X-Admin-Token)")
    current = _knowledge_store.get(item_id)
    if not current:
        raise HTTPException(404, "知识记录不存在")
    _knowledge_store.delete(item_id)
    _invalidate_deps(current.get("database_id"))
    return {"status": "deleted", "id": item_id}


# ---------------- 旧配置管理(兼容术语映射) ----------------

def _admin_ok(admin_token: str | None) -> bool:
    # Single-user/private deployments may reuse the platform access token.
    # Setting ADMIN_TOKEN immediately restores a distinct management boundary.
    expected = os.getenv("ADMIN_TOKEN") or os.getenv("PLATFORM_ACCESS_TOKEN")
    return bool(expected) and admin_token == expected


@router.get("/config/term-mapping")
async def get_term_mapping(business_line: str = "_global"):
    from nl2sql_agent.services.deps import CONFIG_DIR

    path = Path(CONFIG_DIR) / "term_mapping" / f"{business_line}.yaml"
    if not path.exists():
        raise HTTPException(404, f"术语映射 {business_line} 不存在")
    import yaml

    return {"business_line": business_line, "mapping": yaml.safe_load(path.read_text(encoding="utf-8")) or {}}


@router.put("/config/term-mapping")
async def put_term_mapping(
    payload: dict = Body(...),
    business_line: str = "_global",
    admin_token: str | None = Header(default=None),
):
    if not _admin_ok(admin_token):
        raise HTTPException(403, "需要管理权限(Header X-Admin-Token)")
    from nl2sql_agent.services.deps import CONFIG_DIR

    path = Path(CONFIG_DIR) / "term_mapping" / f"{business_line}.yaml"
    import yaml

    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    # 清缓存 → term_mapping 按 mtime 自动热更新
    get_deps().loader.reload()
    get_deps().term_mapping._refresh()  # noqa: SLF001
    return {"status": "ok", "business_line": business_line}


@router.get("/config/rules")
async def get_rules():
    from nl2sql_agent.services.deps import CONFIG_DIR

    out = {}
    for name in ("clarification_rules", "complexity_rules", "sensitive_rules", "settings"):
        path = Path(CONFIG_DIR) / f"{name}.yaml"
        if path.exists():
            import yaml

            out[name] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return out


# ---------------- 表结构 & 注释审核(前端补充字段注释) ----------------

def _review_store():
    from nl2sql_agent.services.schema_ingest.review_queue import ReviewStore

    return ReviewStore(DATA_DIR / "schema_ingest.db")


def _schema_rows_from_mschema(path: Path, overrides: dict) -> list[dict]:
    """Build a database-specific management view directly from M-Schema."""
    schema = json.loads(path.read_text(encoding="utf-8"))
    result: list[dict] = []
    for table_name, table in (schema.get("tables") or {}).items():
        columns = []
        for column_name, field in (table.get("fields") or {}).items():
            final = overrides.get((table_name, column_name))
            original = str(field.get("comment") or "")
            columns.append({
                "name": column_name,
                "type": field.get("raw_type") or field.get("type") or "",
                "comment": original,
                "eff_comment": final or original,
                "overridden": bool(final),
                "sensitive": bool(field.get("sensitive")),
            })
        result.append({
            "table_name": table_name,
            "comment": overrides.get((table_name, None)) or table.get("comment") or "",
            "columns": columns,
        })
    return result


@router.get("/schema")
async def api_schema(business_line: str = "risk_mart", database_id: str | None = None):
    """返回某系统的表结构(每表字段名/类型/已有注释/是否有 override)。供前端浏览与补充注释。"""
    database = _database_store.get(database_id) if database_id else None
    if database_id and not database:
        raise HTTPException(404, "数据库配置不存在")
    datasource = _database_store.artifact_key(database_id) if database_id else business_line
    overrides = _review_store().overrides(datasource)
    if database_id:
        path = _database_store.schema_path(database_id)
        if not path.exists():
            raise HTTPException(409, "该数据库尚未同步 Schema")
        try:
            return _schema_rows_from_mschema(path, overrides)
        except (OSError, ValueError, TypeError) as exc:
            raise HTTPException(500, "M-Schema 文件无法读取") from exc

    deps = get_deps(database_id)
    tables = deps.catalog.tables_for_scope([business_line])
    result = []
    for t in tables:
        cols = []
        for c in t.columns:
            final = overrides.get((t.name, c["name"]))
            cols.append(
                {
                    "name": c["name"],
                    "type": c["type"],
                    "comment": final or c.get("comment") or "",
                    "eff_comment": final or c.get("comment") or "",
                    "overridden": bool(final),
                    "sensitive": bool(c.get("sensitive")),
                }
            )
        result.append(
            {
                "table_name": t.name,
                "comment": overrides.get((t.name, None)) or t.comment or "",
                "columns": cols,
            }
        )
    return result


@router.get("/schema/review")
async def api_review_list(
    datasource: str = "risk_mart",
    status: str = "pending",
    database_id: str | None = None,
):
    """待审核注释队列。"""
    if database_id:
        if not _database_store.get(database_id):
            raise HTTPException(404, "数据库配置不存在")
        datasource = _database_store.artifact_key(database_id)
    return _review_store().list_reviews(status=status, datasource=datasource)


class ReviewApproveReq(BaseModel):
    edited_comment: str
    reviewer: str = "frontend"


@router.post("/schema/review/{review_id}/approve")
async def api_review_approve(review_id: int, body: ReviewApproveReq):
    ok = _review_store().approve(review_id, body.edited_comment, body.reviewer)
    if not ok:
        raise HTTPException(404, f"审核条目 {review_id} 不存在或不可通过")
    return {"status": "ok"}


class ReviewRejectReq(BaseModel):
    reason: str
    reviewer: str = "frontend"


@router.post("/schema/review/{review_id}/reject")
async def api_review_reject(review_id: int, body: ReviewRejectReq):
    ok = _review_store().reject(review_id, body.reason, body.reviewer)
    if not ok:
        raise HTTPException(404, f"审核条目 {review_id} 不存在或不可驳回")
    return {"status": "ok"}


class CommentReq(BaseModel):
    comment: str
    reviewer: str = "frontend"


@router.post("/schema/{table_name}/comment")
async def api_set_comment(
    table_name: str,
    body: CommentReq,
    business_line: str = "risk_mart",
    database_id: str | None = None,
):
    """补充/修改某张表的注释(写入系统覆盖层,不改数据库),并标记该表已解决评审未决项。"""
    review_store = _review_store()
    if database_id and not _database_store.get(database_id):
        raise HTTPException(404, "数据库配置不存在")
    datasource = _database_store.artifact_key(database_id) if database_id else business_line
    review_store.set_override(datasource, table_name, None, body.comment)
    return {"status": "ok", "table_name": table_name, "comment": body.comment}


@router.post("/schema/{table_name}/{column_name}/comment")
async def api_set_column_comment(
    table_name: str,
    column_name: str,
    body: CommentReq,
    business_line: str = "risk_mart",
    database_id: str | None = None,
):
    """补充/修改某字段注释(写入系统覆盖层,不改数据库)。"""
    review_store = _review_store()
    if database_id and not _database_store.get(database_id):
        raise HTTPException(404, "数据库配置不存在")
    datasource = _database_store.artifact_key(database_id) if database_id else business_line
    review_store.set_override(datasource, table_name, column_name, body.comment)
    return {"status": "ok", "table_name": table_name, "column_name": column_name, "comment": body.comment}


@router.post("/schema/reingest")
async def api_reingest(
    datasource: str = "risk_mart",
    business_line: str = "risk_mart",
    database_id: str | None = None,
):
    """审核/补充注释后重跑入库,更新 schema_catalog 与向量索引、重建映射。"""
    import sys
    from pathlib import Path as _P

    sys.path.insert(0, str(_P.cwd()))
    from nl2sql_agent.services.config_loader import ConfigLoader
    from nl2sql_agent.services.deps import CONFIG_DIR
    from nl2sql_agent.services.schema_ingest.diff_sync import sync
    from nl2sql_agent.services.schema_ingest.review_queue import ReviewStore

    database = _database_store.get(database_id) if database_id else None
    if database_id and not database:
        raise HTTPException(404, "数据库配置不存在")
    if database:
        datasource = _database_store.artifact_key(database_id)
        business_line = str(database.get("namespace") or business_line)
    deps = get_deps(database_id)
    config = ConfigLoader(CONFIG_DIR).load("schema_ingest.yaml") or {}
    store = ReviewStore(DATA_DIR / "schema_ingest.db")
    db_name = getattr(deps.executor, "conn_kwargs", {}).get("database") or datasource
    report = sync(datasource, db_name, deps, config, store, mode="incremental", business_line=business_line)
    if database_id:
        schema_payload = json.loads(
            _database_store.schema_path(database_id).read_text(encoding="utf-8")
        )
        _relation_store.replace_discovered(
            database_id, schema_payload.get("relation_candidates") or []
        )
        _database_store.set_schema_status(
            database_id,
            "ready",
            (
                f"入库 {report.ingested}，待审核 {report.queued}，跳过 {report.skipped}；"
                f"画像 {report.profiled_tables} 表/{report.profiled_columns} 字段，"
                f"跳过 {report.profile_skipped_columns} 字段；"
                f"发现 {report.relation_candidates} 条候选关系"
            ),
        )
        _invalidate_deps(database_id)
    return {
        "status": "ok",
        "ingested": report.ingested,
        "queued": report.queued,
        "skipped": report.skipped,
        "removed": report.removed,
        "profiled_tables": report.profiled_tables,
        "profiled_columns": report.profiled_columns,
        "profiled_rows": report.profiled_rows,
        "profile_skipped_columns": report.profile_skipped_columns,
        "relation_candidates": report.relation_candidates,
    }


@router.post("/schema/review/reingest")
async def api_review_reingest(database_id: str | None = None):
    """前端在审核页操作后调用:重跑入库,把 override 注释落到 schema_catalog/m-schema。"""
    return await api_reingest(database_id=database_id)
