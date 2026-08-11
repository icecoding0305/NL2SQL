"""查询历史/审计/审批/反馈的持久化存储(SQLite)。

每条查询一行,存最终状态与完整 trace;审批动作、用户反馈单独记录。
供历史页、审计页、审批队列页查询。
"""

from __future__ import annotations

import decimal
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


def _json_default(o: Any) -> Any:
    """SQL 执行结果可能含 Decimal/日期,统一转成可 JSON 序列化的形式。"""
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (datetime,)):
        return o.isoformat()
    return str(o)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class QueryStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS queries (
                    trace_id        TEXT PRIMARY KEY,
                    user_id         TEXT,
                    user_query      TEXT,
                    data_scope      TEXT,
                    status          TEXT,            -- running / done / pending_review / error / rejected / blocked / cancelled
                    generated_sql   TEXT,
                    plan_json       TEXT,
                    logical_plan    TEXT,
                    query_mschema   TEXT,
                    retrieved_schema TEXT,
                    sensitive_reasons TEXT,
                    execution_result TEXT,
                    result_summary  TEXT,
                    final_answer    TEXT,
                    trace_steps     TEXT,
                    node_latencies  TEXT,
                    node_latency_history TEXT,
                    llm_calls       TEXT,
                    retry_count     INTEGER DEFAULT 0,
                    plan_retry_count INTEGER DEFAULT 0,
                    approved        INTEGER,
                    approver        TEXT,
                    next_node       TEXT,            -- 暂停在哪个节点(human_review / clarify_*)
                    retrieval_confidence REAL,
                    retrieval_candidates TEXT,
                    query_intent   TEXT,
                    resolved_query TEXT,
                    semantic_graph TEXT,
                    business_clarification TEXT,
                    decision_summary TEXT,
                    field_candidates TEXT,
                    field_ambiguities TEXT,
                    schema_plan    TEXT,
                    clarification_reason TEXT,
                    low_confidence_flag INTEGER DEFAULT 0,
                    execution_error TEXT,
                    risk_decision   TEXT DEFAULT 'pass',
                    created_at      TEXT,
                    finished_at     TEXT
                );
                CREATE TABLE IF NOT EXISTS feedbacks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id    TEXT,
                    node        TEXT,
                    feedback_type TEXT,
                    comment     TEXT,
                    created_at  TEXT
                );
                """
            )
            # 旧库迁移:补齐新增列
            for col in (
                "next_node TEXT",
                "logical_plan TEXT",
                "query_mschema TEXT",
                "retrieval_confidence REAL",
                "retrieval_candidates TEXT",
                "query_intent TEXT",
                "resolved_query TEXT",
                "semantic_graph TEXT",
                "business_clarification TEXT",
                "decision_summary TEXT",
                "field_candidates TEXT",
                "field_ambiguities TEXT",
                "schema_plan TEXT",
                "clarification_reason TEXT",
                "low_confidence_flag INTEGER DEFAULT 0",
                "execution_error TEXT",
                "result_summary TEXT",
                "risk_decision TEXT DEFAULT 'pass'",
                "node_latency_history TEXT",
                "llm_calls TEXT",
            ):
                try:
                    conn.execute(f"ALTER TABLE queries ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # 列已存在

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------- 查询记录 ----------------

    def save_query(self, trace_id: str, **fields: Any) -> None:
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM queries WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if exists:
                self._update(conn, trace_id, **fields)
            else:
                base = {
                    "trace_id": trace_id,
                    "user_query": "",
                    "data_scope": "[]",
                    "status": "running",
                    "created_at": _now(),
                }
                base.update(fields)
                cols = ", ".join(base.keys())
                placeholders = ", ".join("?" * len(base))
                conn.execute(
                    f"INSERT INTO queries ({cols}) VALUES ({placeholders})",
                    tuple(self._to_json(v) for v in base.values()),
                )

    def update_query(self, trace_id: str, **fields: Any) -> None:
        with self._lock, self._connect() as conn:
            self._update(conn, trace_id, **fields)

    def _update(self, conn: sqlite3.Connection, trace_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE queries SET {sets} WHERE trace_id = ?",
            tuple(self._to_json(v) for v in fields.values()) + (trace_id,),
        )

    @staticmethod
    def _to_json(v: Any) -> Any:
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, default=_json_default)
        return v

    def get_query(self, trace_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM queries WHERE trace_id = ?", (trace_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_queries(
        self,
        user_id: str | None = None,
        business_line: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM queries WHERE 1=1"
        params: list[Any] = []
        if user_id:
            sql += " AND user_id = ?"
            params.append(user_id)
        if business_line:
            sql += " AND data_scope LIKE ?"
            params.append(f"%{business_line}%")
        if start_date:
            sql += " AND created_at >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND created_at <= ?"
            params.append(end_date)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def list_pending_approvals(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM queries WHERE status = 'pending_review' "
                "ORDER BY created_at ASC"
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def delete_query(self, trace_id: str) -> bool:
        """删除一条会话及其反馈，返回会话是否存在。"""
        with self._lock, self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM queries WHERE trace_id = ?", (trace_id,)
            ).fetchone()
            if not exists:
                return False
            conn.execute("DELETE FROM feedbacks WHERE trace_id = ?", (trace_id,))
            conn.execute("DELETE FROM queries WHERE trace_id = ?", (trace_id,))
            return True

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        for key in (
            "data_scope", "plan_json", "logical_plan", "query_mschema", "retrieved_schema", "sensitive_reasons",
            "execution_result", "result_summary", "trace_steps", "node_latencies", "node_latency_history",
            "llm_calls", "retrieval_candidates",
            "query_intent", "resolved_query", "semantic_graph", "business_clarification", "decision_summary",
            "field_candidates", "field_ambiguities", "schema_plan",
        ):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json.loads(d[key])
                except (ValueError, TypeError):
                    pass
        return d

    # ---------------- 反馈 ----------------

    def add_feedback(self, trace_id: str, node: str, feedback_type: str, comment: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO feedbacks (trace_id, node, feedback_type, comment, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (trace_id, node, feedback_type, comment, _now()),
            )

    def list_feedbacks(self, trace_id: str | None = None) -> list[dict]:
        with self._connect() as conn:
            if trace_id:
                rows = conn.execute(
                    "SELECT * FROM feedbacks WHERE trace_id = ? ORDER BY id", (trace_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM feedbacks ORDER BY id").fetchall()
        return [dict(r) for r in rows]
