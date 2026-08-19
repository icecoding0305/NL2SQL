"""HTTP 服务入口(FastAPI)。

- POST /query    发起查询(注入 user_id / data_scope)
- POST /approve  人工确认(恢复被 interrupt 暂停的流程)
- GET  /thread/{id}  查看线程状态

运行:
    uvicorn nl2sql_agent.main:app
需要环境变量 ANTHROPIC_API_KEY 与 ANTHROPIC_MODEL(以及数据库连接配置)。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langgraph.types import Command
from pydantic import BaseModel, Field

from nl2sql_agent.api import get_deps, router as api_router
from nl2sql_agent.graph import build_graph
from nl2sql_agent.services.checkpoint import create_sqlite_checkpointer
from nl2sql_agent.services.deps import load_env
from nl2sql_agent.security import platform_access_required, verify_platform_token

load_env()  # 加载项目根目录 .env(ANTHROPIC_API_KEY / ANTHROPIC_MODEL / NL2SQL_DEMO 等)


class QueryRequest(BaseModel):
    user_query: str
    user_id: str
    data_scope: list[str] = Field(description="用户可访问的业务线列表")
    conversation_history: list[dict] = Field(default_factory=list)
    thread_id: Optional[str] = None


class ApproveRequest(BaseModel):
    thread_id: str
    approved: bool
    comment: str = ""


app = FastAPI(title="NL2SQL Agent", version="0.1.0")


@app.middleware("http")
async def require_platform_access(request: Request, call_next):
    """Protect business and administration APIs behind one shared access code."""
    path = request.url.path
    protected = (
        path.startswith("/api/")
        or path == "/query"
        or path == "/approve"
        or path.startswith("/thread/")
    )
    if protected and path != "/api/access/status":
        if not verify_platform_token(request.headers.get("X-Platform-Token")):
            return JSONResponse(status_code=401, content={"detail": "访问密码无效或已失效"})
    return await call_next(request)


@app.get("/api/access/status")
def access_status():
    return {"required": platform_access_required()}


@app.post("/api/access/verify")
def access_verify():
    return {"status": "ok"}

# 前端开发服务器跨域(Web 前端默认 http://localhost:5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)

_saver = None
_graph = None


@app.on_event("startup")
def warm_query_dependencies() -> None:
    """Pay local model/index cold-start cost before accepting the first query."""
    if os.getenv("NL2SQL_DEMO") == "1":
        return
    deps = get_deps()
    scopes = list(getattr(deps.catalog, "_tables_by_line", {}).keys())
    if scopes:
        deps.vector_store.search_scored("schema retrieval warmup", top_k=1, data_scope=[scopes[0]])


def get_graph():
    """构建(并缓存)编译后的图。

    默认需要 ANTHROPIC_MODEL / ANTHROPIC_API_KEY 与数据库连接;
    设置环境变量 NL2SQL_DEMO=1 时改用 FakeLLM + InMemoryExecutor,便于无外部依赖演示。
    """
    global _graph, _saver
    if _graph is None:
        data_dir = Path(__file__).resolve().parent.parent / "data"
        _saver = create_sqlite_checkpointer(data_dir / "langgraph_checkpoints.db")
        if os.getenv("NL2SQL_DEMO") == "1":
            from nl2sql_agent.testing import build_test_deps

            deps = build_test_deps()
        else:
            deps = get_deps()
        _graph = build_graph(deps, checkpointer=_saver)
    return _graph


@app.post("/query")
def query(req: QueryRequest):
    graph = get_graph()
    thread_id = req.thread_id or f"th-{req.user_id}-{int(time.time() * 1000)}"
    config = {"configurable": {"thread_id": thread_id}}
    result = graph.invoke(
        {
            "user_query": req.user_query,
            "user_id": req.user_id,
            "data_scope": req.data_scope,
            "conversation_history": req.conversation_history,
        },
        config,
    )
    snap = graph.get_state(config)
    if snap.next:
        # 流程被 interrupt 暂停在人工确认
        vals = snap.values
        return {
            "thread_id": thread_id,
            "status": "human_review_pending",
            "sensitive_reasons": vals.get("sensitive_reasons", []),
            "sql": vals.get("generated_sql"),
            "need_clarification": vals.get("need_clarification", False),
            "clarification_questions": vals.get("clarification_questions", []),
            "business_clarification": (
                vals["business_clarification"].model_dump()
                if vals.get("business_clarification") else None
            ),
            "decision_summary": (
                vals["decision_summary"].model_dump()
                if vals.get("decision_summary") else None
            ),
        }
    # invoke 结果可能不含从未被写入的默认字段,统一用 get_state 的完整快照
    vals = snap.values
    status = "blocked" if vals.get("blocked_reason") else "done"
    return {
        "thread_id": thread_id,
        "status": status,
        "final_answer": vals.get("final_answer"),
        "result_summary": (
            vals["result_summary"].model_dump() if vals.get("result_summary") else None
        ),
        "execution_result": vals.get("execution_result"),
        "sql": vals.get("generated_sql"),
        "trace_id": vals.get("trace_id"),
        "trace_steps": vals.get("trace_steps"),
        "decision_summary": (
            vals["decision_summary"].model_dump()
            if vals.get("decision_summary") else None
        ),
    }


@app.post("/approve")
def approve(req: ApproveRequest):
    if not get_deps().config.approval_enabled:
        raise HTTPException(status_code=404, detail="查询审批功能已临时关闭")
    graph = get_graph()
    config = {"configurable": {"thread_id": req.thread_id}}
    graph.invoke(
        Command(resume={"approved": req.approved, "comment": req.comment}), config
    )
    snap = graph.get_state(config)
    if snap.next:
        return {"status": "still_pending", "next": snap.next}
    vals = snap.values
    return {
        "status": "done",
        "final_answer": vals.get("final_answer"),
        "result_summary": (
            vals["result_summary"].model_dump() if vals.get("result_summary") else None
        ),
        "execution_result": vals.get("execution_result"),
        "trace_steps": vals.get("trace_steps"),
    }


@app.get("/thread/{thread_id}")
def thread(thread_id: str):
    snap = get_graph().get_state({"configurable": {"thread_id": thread_id}})
    return {"next": list(snap.next) if snap.next else [], "values": snap.values}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("nl2sql_agent.main:app", host="0.0.0.0", port=8000, reload=True)
