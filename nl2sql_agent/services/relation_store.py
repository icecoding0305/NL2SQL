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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_database_relations_database "
                "ON database_relations(database_id)"
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
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO database_relations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(item.values()),
            )
        return self.get(item["id"]) or {}

    def update(self, relation_id: str, values: dict) -> dict | None:
        if not self.get(relation_id):
            return None
        allowed = {
            "source_table", "source_columns", "target_table", "target_columns",
            "cardinality", "preferred_join_type", "description", "enabled",
        }
        updates = {key: values[key] for key in allowed if key in values and values[key] is not None}
        for key in ("source_columns", "target_columns"):
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
                "status": "verified",
                "source": "user_configured",
            }
            for item in self.list(database_id, enabled_only=True)
        ]
