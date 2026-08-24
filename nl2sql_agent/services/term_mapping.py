"""术语映射服务。

- 按业务线命名空间加载 config/term_mapping/{business_line}.yaml + _global.yaml
- 检索时先按 data_scope 命中的业务线命名空间查,查不到再查全局,避免跨业务线同名指标冲突
- 支持文件改动热更新(通过 ConfigLoader 的 mtime 缓存)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from nl2sql_agent.services.config_loader import ConfigLoader


class TermResolutionStatus(str, Enum):
    FOUND = "found"          # 唯一映射
    AMBIGUOUS = "ambiguous"  # 同一词在不同命名空间/条目下口径不一致
    NOT_FOUND = "not_found"  # 该业务线命名空间 + 全局都查不到


@dataclass
class TermEntry:
    term: str
    business_line: str
    resolved_fields: list[str]
    definition: str
    composite_metric: bool = False
    aliases: list[str] = field(default_factory=list)


@dataclass
class TermResolution:
    status: TermResolutionStatus
    entries: list[TermEntry] = field(default_factory=list)


class TermMappingService:
    def __init__(self, loader: ConfigLoader, overlay_entries: dict[str, dict] | None = None):
        self.loader = loader
        self.overlay_entries = dict(overlay_entries or {})
        self._namespaces: dict[str, dict[str, TermEntry]] = {}
        self._global: dict[str, TermEntry] = {}
        self._mtimes: tuple[int, ...] = ()
        self._refresh()

    # ---------- 加载与热更新 ----------
    def _refresh(self) -> None:
        self._namespaces = {}
        self._global = {}
        mapping_dir: Path = self.loader.base_dir / "term_mapping"
        files = sorted(mapping_dir.glob("*.yaml"))
        for f in files:
            data = self.loader.load(f"term_mapping/{f.name}") or {}
            ns_name = f.stem  # _global.yaml -> _global
            for term, cfg in data.items():
                entry = TermEntry(
                    term=term,
                    business_line=cfg.get("business_line", ns_name),
                    resolved_fields=[str(x) for x in cfg.get("resolved_fields", [])],
                    definition=cfg.get("definition", ""),
                    composite_metric=bool(cfg.get("composite_metric", False)),
                    aliases=[str(x) for x in cfg.get("aliases", [])],
                )
                if ns_name == "_global":
                    self._global[term] = entry
                else:
                    self._namespaces.setdefault(ns_name, {})[term] = entry
        for term, cfg in self.overlay_entries.items():
            namespace = str(cfg.get("business_line") or "global")
            entry = TermEntry(
                term=term,
                business_line=namespace,
                resolved_fields=[str(x) for x in cfg.get("resolved_fields", [])],
                definition=str(cfg.get("definition") or ""),
                composite_metric=bool(cfg.get("composite_metric", False)),
                aliases=[str(x) for x in cfg.get("aliases", [])],
            )
            self._namespaces.setdefault(namespace, {})[term] = entry
        self._mtimes = self._current_mtimes()

    def _current_mtimes(self) -> tuple[int, ...]:
        mapping_dir = self.loader.base_dir / "term_mapping"
        return tuple(
            (mapping_dir / f).stat().st_mtime_ns
            for f in sorted(mapping_dir.glob("*.yaml"))
        )

    def _refresh_if_changed(self) -> None:
        if self._current_mtimes() != self._mtimes:
            self._refresh()

    # ---------- 查询 ----------
    def resolve(self, term: str, data_scope: list[str]) -> TermResolution:
        """按 data_scope 命名空间解析术语,查不到再查全局。"""
        self._refresh_if_changed()
        entries: list[TermEntry] = []
        for bl in data_scope:
            ns = self._namespaces.get(bl)
            if ns and term in ns:
                entries.append(ns[term])
        if not entries and term in self._global:
            entries.append(self._global[term])
        if not entries:
            return TermResolution(status=TermResolutionStatus.NOT_FOUND)
        defs = {e.definition for e in entries}
        if len(defs) > 1:
            return TermResolution(status=TermResolutionStatus.AMBIGUOUS, entries=entries)
        return TermResolution(status=TermResolutionStatus.FOUND, entries=[entries[0]])

    def extract_terms(self, text: str, data_scope: list[str]) -> list[str]:
        """从查询里抽取命中的术语(最长优先),返回主术语名。

        别名(aliases)也参与匹配——用户说"放款金额"应命中主术语"贷款金额"。
        只匹配 data_scope 命中的业务线命名空间 + 全局映射。
        """
        self._refresh_if_changed()
        alias_map: dict[str, str] = {}  # 候选词(主名/别名) -> 主术语名

        def _add(entries: dict[str, TermEntry]) -> None:
            for e in entries.values():
                alias_map[e.term] = e.term
                for a in e.aliases:
                    alias_map.setdefault(a, e.term)

        _add(self._global)
        for bl in data_scope:
            _add(self._namespaces.get(bl, {}))

        found: list[str] = []
        working = text
        for cand in sorted(alias_map, key=len, reverse=True):
            if cand and cand in working:
                main = alias_map[cand]
                if main not in found:
                    found.append(main)
                # 从工作文本移除已命中的候选,避免子串重复匹配
                working = working.replace(cand, " ")
        return found

    def all_term_names(self) -> set[str]:
        """库里(任意业务线 + 全局)所有已知术语名 + 别名,用于"查不到"判定。"""
        self._refresh_if_changed()
        names: set[str] = set()

        def _add(entries: dict[str, TermEntry]) -> None:
            for e in entries.values():
                names.add(e.term)
                names.update(e.aliases)

        _add(self._global)
        for ns in self._namespaces.values():
            _add(ns)
        return names
