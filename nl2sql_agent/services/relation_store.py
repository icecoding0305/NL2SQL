"""Persistent, database-scoped user-verified table relationships."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DatabaseRelationStore:
    """Store relationships separately from generated M-Schema snapshots.

    Schema synchronization can safely replace m-schema.json without deleting
    relationships explicitly confirmed by an administrator.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS database_relations (
                    id TEXT PRIMARY KEY,
                    database_id TEXT NOT NULL,
                    source_table TEXT NOT NULL,
                    source_columns TEXT NOT NULL,
                    target_table TEXT NOT NULL,
                    target_columns TEXT NOT NULL,
                    cardinality TEXT NOT NULL DEFAULT 'many_to_one',
                    preferred_join_type TEXT NOT NULL DEFAULT 'inner',
                    description TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'verified',
                    source TEXT NOT NULL DEFAULT 'user_configured',
                    confidence REAL NOT NULL DEFAULT 1.0,
                    evidence TEXT NOT NULL DEFAULT '[]',
                    validation_summary TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_database_relations_database "
                "ON database_relations(database_id)"
            )
            existing_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(database_relations)").fetchall()
            }
            migrations = {
                "status": "TEXT NOT NULL DEFAULT 'verified'",
                "source": "TEXT NOT NULL DEFAULT 'user_configured'",
                "confidence": "REAL NOT NULL DEFAULT 1.0",
                "evidence": "TEXT NOT NULL DEFAULT '[]'",
                "validation_summary": "TEXT NOT NULL DEFAULT '{}'",
            }
            for column, definition in migrations.items():
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE database_relations ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_database_relation_edge "
                "ON database_relations(database_id, source_table, source_columns, "
                "target_table, target_columns)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _decode(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        item["source_columns"] = json.loads(item.get("source_columns") or "[]")
        item["target_columns"] = json.loads(item.get("target_columns") or "[]")
        item["evidence"] = json.loads(item.get("evidence") or "[]")
        item["validation_summary"] = json.loads(item.get("validation_summary") or "{}")
        item["enabled"] = bool(item.get("enabled"))
        return item

    def list(self, database_id: str, *, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM database_relations WHERE database_id=?"
        params: list[object] = [database_id]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY source_table, target_table, created_at"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, relation_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM database_relations WHERE id=?", (relation_id,)
            ).fetchone()
        return self._decode(row) if row else None

    def create(self, database_id: str, values: dict) -> dict:
        now = _now()
        item = {
            "id": values.get("id") or uuid.uuid4().hex[:12],
            "database_id": database_id,
            "source_table": str(values["source_table"]).strip(),
            "source_columns": json.dumps(values["source_columns"], ensure_ascii=False),
            "target_table": str(values["target_table"]).strip(),
            "target_columns": json.dumps(values["target_columns"], ensure_ascii=False),
            "cardinality": str(values.get("cardinality") or "many_to_one"),
            "preferred_join_type": str(values.get("preferred_join_type") or "inner"),
            "description": str(values.get("description") or "").strip(),
            "enabled": int(bool(values.get("enabled", True))),
            "status": str(values.get("status") or "verified"),
            "source": str(values.get("source") or "user_configured"),
            "confidence": float(values.get("confidence", 1.0)),
            "evidence": json.dumps(values.get("evidence") or [], ensure_ascii=False),
            "validation_summary": json.dumps(
                values.get("validation_summary") or {}, ensure_ascii=False
            ),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            columns = list(item)
            conn.execute(
                f"INSERT INTO database_relations ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(item[column] for column in columns),
            )
        return self.get(item["id"]) or {}

    def update(self, relation_id: str, values: dict) -> dict | None:
        if not self.get(relation_id):
            return None
        allowed = {
            "source_table", "source_columns", "target_table", "target_columns",
            "cardinality", "preferred_join_type", "description", "enabled",
            "status", "source", "confidence", "evidence", "validation_summary",
        }
        updates = {key: values[key] for key in allowed if key in values and values[key] is not None}
        for key in ("source_columns", "target_columns"):
            if key in updates:
                updates[key] = json.dumps(updates[key], ensure_ascii=False)
        for key in ("evidence", "validation_summary"):
            if key in updates:
                updates[key] = json.dumps(updates[key], ensure_ascii=False)
        if "enabled" in updates:
            updates["enabled"] = int(bool(updates["enabled"]))
        updates["updated_at"] = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE database_relations SET "
                + ",".join(f"{key}=?" for key in updates)
                + " WHERE id=?",
                (*updates.values(), relation_id),
            )
        return self.get(relation_id)

    def decide_many(
        self, database_id: str, relation_ids: list[str], status: str
    ) -> list[str]:
        """Confirm or reject pending candidates in one transaction."""
        unique_ids = list(dict.fromkeys(str(item) for item in relation_ids if item))
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM database_relations WHERE database_id=? "
                f"AND status IN ('candidate','inferred') AND id IN ({placeholders})",
                (database_id, *unique_ids),
            ).fetchall()
            matched = [str(row["id"]) for row in rows]
            if matched:
                matched_placeholders = ",".join("?" for _ in matched)
                conn.execute(
                    f"UPDATE database_relations SET status=?,enabled=?,updated_at=? "
                    f"WHERE database_id=? AND id IN ({matched_placeholders})",
                    (status, int(status == "verified"), _now(), database_id, *matched),
                )
        return matched

    def delete(self, relation_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM database_relations WHERE id=?", (relation_id,))
        return cursor.rowcount > 0

    def delete_for_database(self, database_id: str) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM database_relations WHERE database_id=?", (database_id,)
            )
        return cursor.rowcount

    def runtime_relations(self, database_id: str) -> list[dict]:
        """Return the M-Schema-compatible verified overlay consumed by planning."""
        return [
            {
                "id": item["id"],
                "source_table": item["source_table"],
                "source_columns": item["source_columns"],
                "target_table": item["target_table"],
                "target_columns": item["target_columns"],
                "cardinality": item["cardinality"],
                "preferred_join_type": item["preferred_join_type"],
                "description": item["description"],
                "constraint_name": f"user_relation_{item['id']}",
                "relation_type": "user_defined",
                "status": item.get("status") or "verified",
                "source": item.get("source") or "user_configured",
                "confidence": float(item.get("confidence") or 0.0),
                "evidence": item.get("evidence") or [],
            }
            for item in self.list(database_id, enabled_only=True)
            if item.get("status") in {"verified", "confirmed"}
        ]

    def replace_discovered(self, database_id: str, candidates: list[dict]) -> int:
        """Upsert the latest discovery snapshot without touching verified rows."""
        active_ids: set[str] = set()
        for candidate in candidates:
            source_columns = json.dumps(candidate.get("source_columns") or [], ensure_ascii=False)
            target_columns = json.dumps(candidate.get("target_columns") or [], ensure_ascii=False)
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT id,status,source FROM database_relations WHERE database_id=? "
                    "AND source_table=? AND source_columns=? AND target_table=? AND target_columns=?",
                    (
                        database_id, candidate.get("source_table"), source_columns,
                        candidate.get("target_table"), target_columns,
                    ),
                ).fetchone()
            if row:
                active_ids.add(str(row["id"]))
                if row["status"] not in {"verified", "confirmed", "rejected"}:
                    self.update(str(row["id"]), {
                        **candidate,
                        "enabled": False,
                        "source": "schema_relation_discovery",
                    })
                continue
            created = self.create(database_id, {
                **candidate,
                "enabled": False,
                "source": "schema_relation_discovery",
                "description": "系统根据字段、键和安全样本自动发现的候选关系",
            })
            if created.get("id"):
                active_ids.add(str(created["id"]))

        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM database_relations WHERE database_id=? "
                "AND source='schema_relation_discovery' AND status IN ('candidate','inferred')",
                (database_id,),
            ).fetchall()
            stale = [str(row["id"]) for row in rows if str(row["id"]) not in active_ids]
            if stale:
                conn.executemany(
                    "DELETE FROM database_relations WHERE id=?", [(item,) for item in stale]
                )
        return len(active_ids)
