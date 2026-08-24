"""Versioned enterprise knowledge records for terminology, rules and examples."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


KNOWLEDGE_TYPES = {"term", "synonym", "business_rule", "optimization_case"}
KNOWLEDGE_STATUSES = {"draft", "published", "disabled"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class KnowledgeStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_items (
                    id TEXT PRIMARY KEY,
                    knowledge_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    database_id TEXT,
                    namespace TEXT NOT NULL DEFAULT 'global',
                    status TEXT NOT NULL DEFAULT 'draft',
                    priority INTEGER NOT NULL DEFAULT 100,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL DEFAULT '{}',
                    source TEXT NOT NULL DEFAULT 'manual',
                    source_key TEXT UNIQUE,
                    created_by TEXT NOT NULL DEFAULT 'system',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_scope
                ON knowledge_items(knowledge_type, database_id, namespace, status);
                CREATE TABLE IF NOT EXISTS knowledge_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    knowledge_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _decode(row: sqlite3.Row | dict) -> dict:
        item = dict(row)
        try:
            item["payload"] = json.loads(item.get("payload") or "{}")
        except (TypeError, ValueError):
            item["payload"] = {}
        return item

    def list(
        self,
        *,
        knowledge_type: str | None = None,
        database_id: str | None = None,
        status: str | None = None,
        query: str | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM knowledge_items WHERE 1=1"
        params: list[Any] = []
        if knowledge_type:
            sql += " AND knowledge_type=?"
            params.append(knowledge_type)
        if database_id:
            sql += " AND (database_id=? OR database_id IS NULL)"
            params.append(database_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        if query:
            sql += " AND (name LIKE ? OR description LIKE ?)"
            params.extend([f"%{query}%", f"%{query}%"])
        sql += " ORDER BY priority ASC, updated_at DESC, name ASC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, item_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_items WHERE id=?", (item_id,)
            ).fetchone()
        return self._decode(row) if row else None

    @staticmethod
    def _normalize(values: dict, current: dict | None = None) -> dict:
        data = dict(current or {})
        data.update(values)
        knowledge_type = str(data.get("knowledge_type") or "").strip()
        status = str(data.get("status") or "draft").strip()
        if knowledge_type not in KNOWLEDGE_TYPES:
            raise ValueError("不支持的知识类型")
        if status not in KNOWLEDGE_STATUSES:
            raise ValueError("不支持的知识状态")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("知识名称不能为空")
        payload = data.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("知识内容必须为对象")
        return {
            "knowledge_type": knowledge_type,
            "name": name,
            "description": str(data.get("description") or "").strip(),
            "database_id": data.get("database_id") or None,
            "namespace": str(data.get("namespace") or "global").strip(),
            "status": status,
            "priority": int(data.get("priority", 100)),
            "payload": payload,
            "source": str(data.get("source") or "manual"),
            "source_key": data.get("source_key") or None,
            "created_by": str(data.get("created_by") or "admin"),
        }

    def create(self, values: dict) -> dict:
        item = self._normalize(values)
        now = _now()
        row = {
            "id": values.get("id") or uuid.uuid4().hex[:12],
            **item,
            "version": 1,
            "payload": json.dumps(item["payload"], ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
            "published_at": now if item["status"] == "published" else None,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO knowledge_items "
                "(id,knowledge_type,name,description,database_id,namespace,status,priority,"
                "version,payload,source,source_key,created_by,created_at,updated_at,published_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"], row["knowledge_type"], row["name"], row["description"],
                    row["database_id"], row["namespace"], row["status"], row["priority"],
                    row["version"], row["payload"], row["source"], row["source_key"],
                    row["created_by"], row["created_at"], row["updated_at"],
                    row["published_at"],
                ),
            )
            self._snapshot(conn, row["id"], 1, row, item["created_by"])
        return self.get(row["id"]) or {}

    def update(self, item_id: str, values: dict) -> dict | None:
        current = self.get(item_id)
        if not current:
            return None
        item = self._normalize(values, current)
        version = int(current["version"]) + 1
        now = _now()
        published_at = (
            current.get("published_at") or now
            if item["status"] == "published" else None
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE knowledge_items SET knowledge_type=?,name=?,description=?,database_id=?,"
                "namespace=?,status=?,priority=?,version=?,payload=?,source=?,source_key=?,"
                "created_by=?,updated_at=?,published_at=? WHERE id=?",
                (
                    item["knowledge_type"], item["name"], item["description"],
                    item["database_id"], item["namespace"], item["status"], item["priority"],
                    version, json.dumps(item["payload"], ensure_ascii=False), item["source"],
                    item["source_key"], item["created_by"], now, published_at, item_id,
                ),
            )
            snapshot = {**current, **item, "version": version, "updated_at": now}
            self._snapshot(conn, item_id, version, snapshot, item["created_by"])
        return self.get(item_id)

    @staticmethod
    def _snapshot(
        conn: sqlite3.Connection, item_id: str, version: int, snapshot: dict, actor: str
    ) -> None:
        conn.execute(
            "INSERT INTO knowledge_versions "
            "(knowledge_id,version,snapshot,created_by,created_at) VALUES (?,?,?,?,?)",
            (item_id, version, json.dumps(snapshot, ensure_ascii=False, default=str), actor, _now()),
        )

    def delete(self, item_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM knowledge_items WHERE id=?", (item_id,))
        return cursor.rowcount > 0

    def summary(self, database_id: str | None = None) -> dict:
        items = self.list(database_id=database_id)
        by_type = {kind: 0 for kind in sorted(KNOWLEDGE_TYPES)}
        by_status = {status: 0 for status in sorted(KNOWLEDGE_STATUSES)}
        for item in items:
            by_type[item["knowledge_type"]] += 1
            by_status[item["status"]] += 1
        return {"total": len(items), "by_type": by_type, "by_status": by_status}

    def seed(self, values: dict) -> dict:
        source_key = values.get("source_key")
        if not source_key:
            return self.create(values)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM knowledge_items WHERE source_key=?", (source_key,)
            ).fetchone()
        return self._decode(row) if row else self.create(values)

    def runtime_bundle(self, database_id: str, namespace: str) -> dict:
        """Return published knowledge scoped to one database runtime."""
        items = self.list(database_id=database_id, status="published")
        scoped = [
            item for item in items
            if item.get("database_id") in {None, database_id}
            and item.get("namespace") in {"global", "_global", namespace}
        ]
        # Global records load first; database-specific records override them.
        scoped.sort(key=lambda item: (item.get("database_id") is not None, -item["priority"]))
        terms: dict[str, dict] = {}
        synonyms: dict[str, list[str]] = {}
        predicates: dict[str, dict] = {}
        plan_patterns: dict[str, dict] = {}
        sql_examples: dict[str, dict] = {}
        for item in scoped:
            payload = dict(item.get("payload") or {})
            if item["knowledge_type"] == "term":
                fields = [
                    binding.get("column") for binding in payload.get("bindings", [])
                    if binding.get("column")
                ] or list(payload.get("resolved_fields") or [])
                terms[item["name"]] = {
                    "business_line": namespace,
                    "resolved_fields": fields,
                    "definition": item.get("description") or payload.get("definition", ""),
                    "composite_metric": bool(payload.get("composite_metric", False)),
                    "aliases": [],
                }
            elif item["knowledge_type"] == "synonym":
                canonical = str(payload.get("canonical_term") or "")
                if canonical and payload.get("relation_type") in {"equivalent", "abbreviation"}:
                    synonyms.setdefault(canonical, []).extend(payload.get("aliases") or [])
            elif item["knowledge_type"] == "business_rule":
                rule_id = str(payload.pop("rule_id", item["id"]))
                payload.pop("rule_type", None)
                predicates[rule_id] = payload
            elif item["knowledge_type"] == "optimization_case":
                case_type = payload.pop("case_type", None)
                case_id = str(payload.get("id") or item["id"])
                payload["id"] = case_id
                payload["enabled"] = True
                if case_type == "plan_pattern":
                    plan_patterns[case_id] = payload
                elif case_type == "sql_fallback":
                    payload["verified"] = True
                    sql_examples[case_id] = payload
        for canonical, aliases in synonyms.items():
            if canonical in terms:
                terms[canonical]["aliases"] = list(dict.fromkeys(aliases))
        return {
            "terms": terms,
            "business_predicates": predicates,
            "plan_patterns": list(plan_patterns.values()),
            "sql_examples": list(sql_examples.values()),
        }
