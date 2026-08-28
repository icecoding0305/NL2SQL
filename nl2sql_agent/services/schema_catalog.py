"""Schema 目录:从 config/schema_catalog.yaml 加载各业务线的表结构。

提供按 data_scope 过滤的表/字段查询,供术语映射命中与向量检索兜底使用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.state import SchemaHit
from nl2sql_agent.services.text_encoding import clean_semantic_text


@dataclass
class TableDef:
    name: str
    comment: str
    business_line: str
    columns: list[dict] = field(default_factory=list)
    shared: bool = False  # 跨平台共享表:表级对所有用户可见,行级按 PLATFORM_CODE 过滤

    def column_names(self) -> set[str]:
        return {c["name"] for c in self.columns}

    def normalized_column_names(self) -> set[str]:
        return {str(c["name"]).casefold() for c in self.columns}


class SchemaCatalog:
    def __init__(
        self,
        loader: ConfigLoader,
        m_schema_path: str | Path | None = None,
        relation_overrides: list[dict] | None = None,
    ):
        data = self._load_runtime_data(loader, m_schema_path)
        self.relation_overrides = [dict(item) for item in (relation_overrides or [])]
        self.metadata = dict(data.get("_meta") or {})
        source_path = self.metadata.get("m_schema_path")
        if source_path and not Path(source_path).is_absolute():
            self.metadata["m_schema_path"] = str((loader.base_dir / source_path).resolve())
        self._tables_by_line: dict[str, list[TableDef]] = {}
        self._shared_tables: list[TableDef] = []
        for line, cfg in data.items():
            if line.startswith("_") or not isinstance(cfg, dict):
                continue
            for t in cfg.get("tables", []):
                tbl = TableDef(
                    name=t["name"],
                    comment=t.get("comment", ""),
                    business_line=t.get("business_line", line),
                    columns=[dict(c) for c in t.get("columns", [])],
                    shared=bool(t.get("shared", False)),
                )
                if tbl.shared:
                    self._shared_tables.append(tbl)
                else:
                    self._tables_by_line.setdefault(line, []).append(tbl)

    @staticmethod
    def _settings(loader: ConfigLoader) -> dict:
        try:
            return loader.load("settings.yaml") or {}
        except FileNotFoundError:
            return {}

    @staticmethod
    def _auto_mschema_path(loader: ConfigLoader) -> Path | None:
        """Resolve data/schema/<database>/m-schema.json from DATABASE_URL."""
        settings = SchemaCatalog._settings(loader)
        db_url = settings.get("database_url") or os.getenv("DATABASE_URL")
        if not db_url:
            return None
        try:
            database = urlsplit(str(db_url)).path.strip("/").split("/")[0]
        except ValueError:
            return None
        if not database:
            return None
        return loader.base_dir.parent.parent / "data" / "schema" / database / "m-schema.json"

    @classmethod
    def _configured_mschema_path(cls, loader: ConfigLoader) -> Path | None:
        settings = cls._settings(loader)
        source = settings.get("schema_source") or {}
        mode = str(source.get("mode", "catalog")).lower()
        if mode not in {"m_schema", "mschema", "auto"}:
            return None
        configured = source.get("m_schema_path")
        if configured and str(configured).lower() != "auto":
            path = Path(str(configured))
            return path if path.is_absolute() else (loader.base_dir / path).resolve()
        return cls._auto_mschema_path(loader)

    @staticmethod
    def _projection_from_mschema(path: Path) -> dict | None:
        """Build the runtime catalog view directly from an effective M-Schema."""
        if not path.exists():
            return None
        try:
            mschema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        namespace = str(mschema.get("namespace") or "default")
        tables = []
        for table_name, table in (mschema.get("tables") or {}).items():
            columns = []
            for column_name, field_data in (table.get("fields") or {}).items():
                column = {
                    "name": column_name,
                    "type": field_data.get("type", ""),
                    "comment": clean_semantic_text(field_data.get("comment", "")),
                    "raw_type": field_data.get("raw_type") or field_data.get("type", ""),
                    "nullable": bool(field_data.get("nullable", True)),
                    "primary_key": bool(field_data.get("primary_key", False)),
                    "unique": bool(field_data.get("unique", False)),
                    "indexed": bool(field_data.get("indexed", False)),
                    "category": field_data.get("category", ""),
                    "semantic_role": field_data.get("dim_or_meas", ""),
                    # 字段画像只供服务端值绑定与排序使用；Query M-Schema 的
                    # 提示词投影不会复制 examples/profile。
                    "examples": list(field_data.get("examples") or []),
                    "profile": dict(field_data.get("profile") or {}),
                }
                if field_data.get("time_granularity"):
                    column["time_granularity"] = field_data["time_granularity"]
                if field_data.get("sensitive"):
                    column["sensitive"] = True
                columns.append(column)
            tables.append({
                "name": table_name,
                "comment": clean_semantic_text(table.get("comment", "")),
                "business_line": namespace,
                "shared": bool(table.get("shared", False)),
                "columns": columns,
            })

        manifest = {}
        manifest_path = path.with_name("manifest.json")
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
        return {
            "_meta": {
                "source": "effective-m-schema",
                "datasource": mschema.get("db_id", ""),
                "m_schema_format_version": mschema.get("format_version", ""),
                "snapshot_id": manifest.get("snapshot_id", ""),
                "semantic_hash": manifest.get("semantic_hash", ""),
                "generated_at": manifest.get("generated_at", ""),
                "m_schema_path": str(path.resolve()),
            },
            namespace: {"tables": tables},
        }

    @classmethod
    def _load_runtime_data(
        cls, loader: ConfigLoader, m_schema_path: str | Path | None = None
    ) -> dict:
        explicit_path = m_schema_path is not None
        mschema_path = Path(m_schema_path) if explicit_path else cls._configured_mschema_path(loader)
        if mschema_path is not None:
            projection = cls._projection_from_mschema(mschema_path)
            if projection is not None:
                return projection
            if explicit_path:
                return {"_meta": {"source": "effective-m-schema", "m_schema_path": str(mschema_path)}}
        return loader.load("schema_catalog.yaml") or {}

    def tables_for_scope(self, data_scope: list[str]) -> list[TableDef]:
        """data_scope 命中的业务线表 + 所有共享表。

        共享表(如 dwd_ar_loan_info)表级对所有用户可见,行级权限由
        row_level_filter 按 PLATFORM_CODE 注入实现;非共享表仍按业务线过滤。
        """
        out: list[TableDef] = []
        for bl in data_scope:
            out.extend(self._tables_by_line.get(bl, []))
        out.extend(self._shared_tables)
        return out

    def hits_for_term(
        self,
        term: str,
        resolved_fields: list[str],
        data_scope: list[str],
        preferred_tables: list[str] | None = None,
    ) -> list[SchemaHit]:
        """术语解析出的字段 → 在 data_scope 内找到包含这些字段的表。

        返回整张表的列定义(生成 SQL 时需要用全表字段),并记录命中的术语。
        """
        want = {str(field).casefold() for field in resolved_fields}
        hits: list[SchemaHit] = []
        for tbl in self.tables_for_scope(data_scope):
            if want and want <= tbl.normalized_column_names():
                hits.append(
                    SchemaHit(
                        table_name=tbl.name,
                        table_comment=tbl.comment,
                        columns=[dict(c) for c in tbl.columns],
                        business_terms=[term],
                    )
                )
        preferred_order = {
            table_name: index for index, table_name in enumerate(preferred_tables or [])
        }
        return sorted(
            hits,
            key=lambda hit: (
                0 if hit.table_name in preferred_order else 1,
                preferred_order.get(hit.table_name, len(preferred_order)),
            ),
        )

    def hits_covering_term_fields(
        self, term: str, resolved_fields: list[str], data_scope: list[str]
    ) -> list[SchemaHit]:
        """当字段分散在多表时，用贪心集合覆盖返回能共同覆盖术语字段的表。

        仅在不存在单表完整命中时使用；如果可见表的字段并集仍不完整，则返回空，
        防止把部分口径误报成确定命中。
        """
        uncovered = {str(field).casefold() for field in resolved_fields}
        selected: list[TableDef] = []
        candidates = list(self.tables_for_scope(data_scope))
        while uncovered:
            best = max(
                candidates,
                key=lambda table: len(uncovered & table.normalized_column_names()),
                default=None,
            )
            if best is None or not (uncovered & best.normalized_column_names()):
                return []
            selected.append(best)
            uncovered -= best.normalized_column_names()
            candidates.remove(best)
        return [
            SchemaHit(
                table_name=table.name,
                table_comment=table.comment,
                columns=[dict(column) for column in table.columns],
                business_terms=[term],
            )
            for table in selected
        ]

    def find_table_with_column(self, column: str, data_scope: list[str]) -> str | None:
        wanted = str(column).casefold()
        for tbl in self.tables_for_scope(data_scope):
            if wanted in tbl.normalized_column_names():
                return tbl.name
        return None

    def all_sensitive_fields(self, data_scope: list[str]) -> set[str]:
        out: set[str] = set()
        for tbl in self.tables_for_scope(data_scope):
            for c in tbl.columns:
                if c.get("sensitive"):
                    out.add(c["name"])
        return out
