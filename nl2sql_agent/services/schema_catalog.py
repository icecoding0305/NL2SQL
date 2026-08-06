"""Schema 目录:从 config/schema_catalog.yaml 加载各业务线的表结构。

提供按 data_scope 过滤的表/字段查询,供术语映射命中与向量检索兜底使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.state import SchemaHit


@dataclass
class TableDef:
    name: str
    comment: str
    business_line: str
    columns: list[dict] = field(default_factory=list)
    shared: bool = False  # 跨平台共享表:表级对所有用户可见,行级按 PLATFORM_CODE 过滤

    def column_names(self) -> set[str]:
        return {c["name"] for c in self.columns}


class SchemaCatalog:
    def __init__(self, loader: ConfigLoader):
        data = loader.load("schema_catalog.yaml") or {}
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

    def hits_for_term(self, term: str, resolved_fields: list[str], data_scope: list[str]) -> list[SchemaHit]:
        """术语解析出的字段 → 在 data_scope 内找到包含这些字段的表。

        返回整张表的列定义(生成 SQL 时需要用全表字段),并记录命中的术语。
        """
        want = set(resolved_fields)
        hits: list[SchemaHit] = []
        for tbl in self.tables_for_scope(data_scope):
            if want and want <= tbl.column_names():
                hits.append(
                    SchemaHit(
                        table_name=tbl.name,
                        columns=[dict(c) for c in tbl.columns],
                        business_terms=[term],
                    )
                )
        return hits

    def hits_covering_term_fields(
        self, term: str, resolved_fields: list[str], data_scope: list[str]
    ) -> list[SchemaHit]:
        """当字段分散在多表时，用贪心集合覆盖返回能共同覆盖术语字段的表。

        仅在不存在单表完整命中时使用；如果可见表的字段并集仍不完整，则返回空，
        防止把部分口径误报成确定命中。
        """
        uncovered = set(resolved_fields)
        selected: list[TableDef] = []
        candidates = list(self.tables_for_scope(data_scope))
        while uncovered:
            best = max(
                candidates,
                key=lambda table: len(uncovered & table.column_names()),
                default=None,
            )
            if best is None or not (uncovered & best.column_names()):
                return []
            selected.append(best)
            uncovered -= best.column_names()
            candidates.remove(best)
        return [
            SchemaHit(
                table_name=table.name,
                columns=[dict(column) for column in table.columns],
                business_terms=[term],
            )
            for table in selected
        ]

    def find_table_with_column(self, column: str, data_scope: list[str]) -> str | None:
        for tbl in self.tables_for_scope(data_scope):
            if column in tbl.column_names():
                return tbl.name
        return None

    def all_sensitive_fields(self, data_scope: list[str]) -> set[str]:
        out: set[str] = set()
        for tbl in self.tables_for_scope(data_scope):
            for c in tbl.columns:
                if c.get("sensitive"):
                    out.add(c["name"])
        return out
