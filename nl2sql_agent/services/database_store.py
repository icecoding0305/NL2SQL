"""Persistent database connection registry used by the query router."""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class DatabaseConfigStore:
    def __init__(self, path: str | Path, project_root: str | Path):
        self.path = str(path)
        self.project_root = Path(project_root)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS database_configs (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    engine TEXT NOT NULL DEFAULT 'mysql',
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    database_name TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    namespace TEXT NOT NULL DEFAULT 'risk_mart',
                    is_default INTEGER NOT NULL DEFAULT 0,
                    schema_status TEXT NOT NULL DEFAULT 'not_synced',
                    schema_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        self._seed_from_environment()

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _seed_from_environment(self) -> None:
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM database_configs LIMIT 1").fetchone():
                return
        url = os.getenv("DATABASE_URL")
        if not url:
            return
        parsed = urlsplit(url)
        database_name = parsed.path.strip("/")
        if not parsed.hostname or not database_name:
            return
        schema_path = self.schema_path_for_name(database_name)
        self.create({
            "id": "default",
            "name": database_name,
            "engine": "mysql" if parsed.scheme.startswith("mysql") else "postgres",
            "host": parsed.hostname,
            "port": parsed.port or (3306 if parsed.scheme.startswith("mysql") else 5432),
            "database_name": database_name,
            "username": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "namespace": "risk_mart",
            "is_default": True,
            "schema_status": "ready" if schema_path.exists() else "not_synced",
        })

    def schema_path_for_name(self, database_name: str) -> Path:
        return self.project_root / "data" / "schema" / database_name / "m-schema.json"

    def artifact_key(self, database_id: str) -> str:
        record = self.get(database_id, include_secret=True)
        if not record:
            raise KeyError(database_id)
        # Keep the environment-seeded database compatible with its existing
        # data/schema/<database> snapshot. New UI-created connections use their
        # stable id so same-named databases on different hosts never collide.
        return record["database_name"] if database_id == "default" else database_id

    def schema_path(self, database_id: str) -> Path:
        return self.schema_path_for_name(self.artifact_key(database_id))

    @staticmethod
    def _public(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        item["password_configured"] = bool(item.pop("password", ""))
        item["is_default"] = bool(item.get("is_default"))
        return item

    def list(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM database_configs ORDER BY is_default DESC, name, id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, database_id: str | None = None, *, include_secret: bool = False) -> dict | None:
        with self._connect() as conn:
            if database_id:
                row = conn.execute(
                    "SELECT * FROM database_configs WHERE id=?", (database_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM database_configs ORDER BY is_default DESC, created_at LIMIT 1"
                ).fetchone()
        if not row:
            return None
        return dict(row) if include_secret else self._public(row)

    def create(self, values: dict) -> dict:
        now = _now()
        item = {
            "id": values.get("id") or uuid.uuid4().hex[:12],
            "name": str(values["name"]).strip(),
            "engine": str(values.get("engine") or "mysql").lower(),
            "host": str(values["host"]).strip(),
            "port": int(values["port"]),
            "database_name": str(values["database_name"]).strip(),
            "username": str(values["username"]).strip(),
            "password": str(values.get("password") or ""),
            "namespace": str(values.get("namespace") or "risk_mart").strip(),
            "is_default": int(bool(values.get("is_default"))),
            "schema_status": str(values.get("schema_status") or "not_synced"),
            "schema_message": values.get("schema_message"),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock, self._connect() as conn:
            if item["is_default"]:
                conn.execute("UPDATE database_configs SET is_default=0")
            conn.execute(
                "INSERT INTO database_configs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(item.values()),
            )
            if not conn.execute("SELECT 1 FROM database_configs WHERE is_default=1").fetchone():
                conn.execute("UPDATE database_configs SET is_default=1 WHERE id=?", (item["id"],))
        return self.get(item["id"]) or {}

    def update(self, database_id: str, values: dict) -> dict | None:
        current = self.get(database_id, include_secret=True)
        if not current:
            return None
        allowed = {"name", "engine", "host", "port", "database_name", "username", "namespace"}
        updates = {key: values[key] for key in allowed if key in values and values[key] is not None}
        if values.get("password"):
            updates["password"] = values["password"]
        updates["updated_at"] = _now()
        if any(key in updates for key in {"engine", "host", "port", "database_name", "username", "password"}):
            updates["schema_status"] = "not_synced"
            updates["schema_message"] = None
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE database_configs SET " + ",".join(f"{key}=?" for key in updates) + " WHERE id=?",
                (*updates.values(), database_id),
            )
        return self.get(database_id)

    def set_default(self, database_id: str) -> bool:
        if not self.get(database_id):
            return False
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE database_configs SET is_default=0")
            conn.execute(
                "UPDATE database_configs SET is_default=1, updated_at=? WHERE id=?",
                (_now(), database_id),
            )
        return True

    def set_schema_status(self, database_id: str, status: str, message: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE database_configs SET schema_status=?, schema_message=?, updated_at=? WHERE id=?",
                (status, message, _now(), database_id),
            )

    def delete(self, database_id: str) -> bool:
        current = self.get(database_id)
        if not current:
            return False
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM database_configs WHERE id=?", (database_id,))
            if current.get("is_default"):
                replacement = conn.execute(
                    "SELECT id FROM database_configs ORDER BY created_at LIMIT 1"
                ).fetchone()
                if replacement:
                    conn.execute("UPDATE database_configs SET is_default=1 WHERE id=?", (replacement[0],))
        return cur.rowcount > 0

    def connection_url(self, database_id: str | None = None) -> str:
        item = self.get(database_id, include_secret=True)
        if not item:
            raise KeyError(database_id or "default")
        scheme = "mysql" if item["engine"] == "mysql" else "postgresql"
        return (
            f"{scheme}://{quote(item['username'], safe='')}:{quote(item['password'], safe='')}"
            f"@{item['host']}:{int(item['port'])}/{quote(item['database_name'], safe='')}"
        )
