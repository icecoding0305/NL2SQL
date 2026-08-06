"""待审核队列 + 注释覆盖层 + 结构快照(本地 SQLite)。

- schema_comment_review:待审核记录(表级/字段级草稿注释)
- schema_metadata_override:审核通过的最终注释(构建 embedding 文本时优先取)
- schema_snapshot:结构快照(增量同步 diff 依据)

用本地 SQLite 存系统元数据,不进被检索的业务库(避免 DDL 权限与污染业务表)。
"""

from __future__ import annotations

import sqlite3
import threading
import json
from datetime import datetime
from pathlib import Path


class ReviewStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_comment_review (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    datasource TEXT, table_name TEXT, column_name TEXT,
                    draft_comment TEXT, status TEXT DEFAULT 'pending',
                    draft_confidence REAL DEFAULT 0,
                    evidence_json TEXT,
                    validation_errors TEXT,
                    structure_hash TEXT,
                    reviewer TEXT, reviewed_at TEXT, reject_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS schema_metadata_override (
                    datasource TEXT, table_name TEXT, column_name TEXT,
                    final_comment TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (datasource, table_name, column_name)
                );
                CREATE TABLE IF NOT EXISTS schema_snapshot (
                    datasource TEXT, table_name TEXT, structure_hash TEXT,
                    last_synced_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (datasource, table_name)
                );
                """
            )
            for column in (
                "draft_confidence REAL DEFAULT 0",
                "evidence_json TEXT",
                "validation_errors TEXT",
                "structure_hash TEXT",
            ):
                try:
                    conn.execute(f"ALTER TABLE schema_comment_review ADD COLUMN {column}")
                except sqlite3.OperationalError:
                    pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ---------------- 审核队列 ----------------

    def add_review(
        self,
        datasource: str,
        table_name: str,
        column_name: str | None,
        draft_comment: str,
        *,
        confidence: float = 0.0,
        evidence: dict | None = None,
        validation_errors: list[str] | None = None,
        structure_hash: str = "",
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO schema_comment_review "
                "(datasource, table_name, column_name, draft_comment, draft_confidence, "
                "evidence_json, validation_errors, structure_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datasource, table_name, column_name, draft_comment, confidence,
                    json.dumps(evidence or {}, ensure_ascii=False),
                    json.dumps(validation_errors or [], ensure_ascii=False),
                    structure_hash,
                ),
            )
            conn.commit()
            return cur.lastrowid

    def list_reviews(self, status: str = "pending", datasource: str | None = None) -> list[dict]:
        sql = "SELECT * FROM schema_comment_review WHERE status = ?"
        params: list = [status]
        if datasource:
            sql += " AND datasource = ?"
            params.append(datasource)
        sql += " ORDER BY id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._review_to_dict(r) for r in rows]

    def get_review(self, id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schema_comment_review WHERE id = ?", (id,)).fetchone()
        return self._review_to_dict(row) if row else None

    @staticmethod
    def _review_to_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        for key in ("evidence_json", "validation_errors"):
            if isinstance(data.get(key), str):
                try:
                    data[key] = json.loads(data[key])
                except ValueError:
                    pass
        return data

    def approve(self, id: int, edited_comment: str, reviewer: str) -> bool:
        """审核通过:写入覆盖层 + 更新状态。返回是否找到记录。"""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM schema_comment_review WHERE id = ?", (id,)).fetchone()
            if not row:
                return False
            conn.execute(
                "INSERT OR REPLACE INTO schema_metadata_override "
                "(datasource, table_name, column_name, final_comment, updated_at) VALUES (?,?,?,?,?)",
                (row["datasource"], row["table_name"], row["column_name"],
                 edited_comment, datetime.now().isoformat(timespec="seconds")),
            )
            conn.execute(
                "UPDATE schema_comment_review SET status='approved', reviewer=?, reviewed_at=? WHERE id=?",
                (reviewer, datetime.now().isoformat(timespec="seconds"), id),
            )
            conn.commit()
            return True

    def reject(self, id: int, reason: str, reviewer: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE schema_comment_review SET status='rejected', reject_reason=?, reviewer=?, "
                "reviewed_at=? WHERE id=? AND status='pending'",
                (reason, reviewer, datetime.now().isoformat(timespec="seconds"), id),
            )
            conn.commit()
            return cur.rowcount > 0

    def pending_count(self, datasource: str | None = None) -> int:
        sql = "SELECT COUNT(*) c FROM schema_comment_review WHERE status='pending'"
        params: list = []
        if datasource:
            sql += " AND datasource=?"
            params.append(datasource)
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    # ---------------- 覆盖层 ----------------

    def overrides(self, datasource: str | None = None) -> dict[tuple[str, str | None], str]:
        sql = "SELECT table_name, column_name, final_comment FROM schema_metadata_override"
        params: list = []
        if datasource:
            sql += " WHERE datasource = ?"
            params.append(datasource)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return {(r["table_name"], r["column_name"]): r["final_comment"] for r in rows}

    def set_override(self, datasource: str, table_name: str, column_name: str | None, comment: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_metadata_override "
                "(datasource, table_name, column_name, final_comment, updated_at) VALUES (?,?,?,?,?)",
                (datasource, table_name, column_name, comment, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()

    # ---------------- 结构快照 ----------------

    def load_snapshot(self, datasource: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT table_name, structure_hash FROM schema_snapshot WHERE datasource=?",
                (datasource,),
            ).fetchall()
        return {r["table_name"]: r["structure_hash"] for r in rows}

    def update_snapshot(self, datasource: str, table_name: str, structure_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_snapshot (datasource, table_name, structure_hash, last_synced_at) "
                "VALUES (?,?,?,?)",
                (datasource, table_name, structure_hash, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()

    def delete_snapshot(self, datasource: str, table_name: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM schema_snapshot WHERE datasource=? AND table_name=?",
                (datasource, table_name),
            )
            conn.commit()
