"""字段驱动的查询理解与 Schema 子图规划。

术语库只处理复合业务口径；普通字段查询通过通用比较表达、字段注释、类型/角色
和关系图确定事实锚点、实体维表与桥接表。
"""

from __future__ import annotations

from collections import deque
from difflib import SequenceMatcher
import re
from typing import Iterable

from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.state import (
    FieldCandidate,
    IntentSlot,
    PlannedTable,
    QueryIntent,
    SchemaPlan,
)


_COMPARISONS = {
    "不低于": ">=", "至少": ">=", "超过": ">", "大于": ">", "高于": ">",
    "不超过": "<=", "至多": "<=", "低于": "<", "少于": "<", "等于": "=",
}
_COMPARISON_RE = re.compile(
    rf"(?P<field>[\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z_ ]{{0,23}}?)"
    rf"(?P<word>{'|'.join(sorted(_COMPARISONS, key=len, reverse=True))})"
    rf"(?P<value>-?\d+(?:\.\d+)?)"
)
_TEXT_FILTER_RE = re.compile(
    r"(?P<field>[\u4e00-\u9fffA-Za-z_]{1,16}?)(?P<word>等于|为)"
    r"(?P<value>[\u4e00-\u9fffA-Za-z_]{1,16}?)(?:的|并且|且|$)"
)
_MEASURE_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z_]{1,18}?(?:金额|余额|总额|数量|笔数|比率|率)"
)
_ENTITY_WORDS = ("客户", "产品", "机构", "借据", "合同", "申请", "贷款", "还款", "代偿")
_NUMERIC_TYPES = ("int", "decimal", "numeric", "number", "float", "double", "real")
_PREFIXES = ("请帮我统计", "帮我统计", "统计", "查询", "计算", "筛选", "查找", "查出")
_GENERIC_ALIASES = (
    ("总金额", "金额"), ("总额", "金额"), ("金额合计", "金额"),
    ("客户号", "客户编号"), ("客户id", "客户编号"), ("客户ID", "客户编号"),
    ("借据号", "借据编号"), ("借据编码", "借据编号"),
)
_CONCEPT_ALIASES = {
    "地区": ("地区", "区域", "省", "市", "区县", "地址"),
    "联系方式": ("手机号", "手机", "电话", "邮箱"),
    "时间": ("日期", "时间", "月份", "年度"),
}


def normalize_semantic_text(value: str) -> str:
    text = re.sub(r"[\s\-_./()（）:：,，]+", "", str(value or ""))
    for source, target in _GENERIC_ALIASES:
        text = text.replace(source, target)
    return text.lower()


def _clean_measure_phrase(value: str) -> str:
    text = value.strip()
    for prefix in _PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    # 比较字段通常位于最后一个结构助词之后，例如“筛选客户的代偿金额”。
    if "的" in text:
        left, right = text.rsplit("的", 1)
        # “客户的贷款金额”中左侧是实体上下文；“逾期的总金额”中左侧则是
        # 指标限定词，不能丢掉，否则会退化成所有金额字段都精确命中。
        text = right if any(left.endswith(entity) for entity in _ENTITY_WORDS) else left + right
    for aggregate in ("平均", "累计", "合计", "总计", "最大", "最小"):
        if text.startswith(aggregate):
            text = text[len(aggregate):]
            break
    return text.strip()


def parse_query_intent(query: str) -> QueryIntent:
    """用少量通用语法抽取查询槽位，不绑定具体表或指标映射。"""
    entities = [IntentSlot(text=word, role="entity") for word in _ENTITY_WORDS if word in query]
    filters: list[IntentSlot] = []
    measures: list[IntentSlot] = []
    for match in _COMPARISON_RE.finditer(query):
        phrase = _clean_measure_phrase(match.group("field"))
        raw_value = match.group("value")
        value = float(raw_value) if "." in raw_value else int(raw_value)
        slot = IntentSlot(
            text=phrase,
            role="measure",
            operator=_COMPARISONS[match.group("word")],
            value=value,
        )
        filters.append(slot)
        if phrase and phrase not in {item.text for item in measures}:
            measures.append(IntentSlot(text=phrase, role="measure"))

    for match in _TEXT_FILTER_RE.finditer(query):
        phrase = _clean_measure_phrase(match.group("field"))
        if not phrase or any(item.text == phrase for item in filters):
            continue
        filters.append(IntentSlot(
            text=phrase,
            role="attribute",
            operator="=",
            value=match.group("value"),
        ))

    # 没有比较表达时，也从通用度量后缀提取指标；不维护具体业务指标名称。
    for segment in re.split(r"和|与|以及|、", query):
        for match in _MEASURE_RE.finditer(segment):
            phrase = _clean_measure_phrase(match.group(0))
            if phrase and phrase not in {item.text for item in measures}:
                measures.append(IntentSlot(text=phrase, role="measure"))

    # “代偿金额”中的“代偿”属于度量修饰词，不应再被当成独立实体；“客户”仍保留。
    measure_text = "".join(item.text for item in measures)
    entities = [item for item in entities if item.text not in measure_text]

    attributes: list[IntentSlot] = []
    for phrase in ("基本信息", "详细信息", "联系方式", "明细"):
        if phrase in query:
            attributes.append(IntentSlot(text=phrase, role="attribute"))

    dimensions: list[IntentSlot] = []
    dimension_match = re.search(r"按(.{1,12}?)(?:统计|汇总|分组|计算)", query)
    if dimension_match is None:
        dimension_match = re.search(r"各(.{1,8}?)(?:客户|产品|机构)(?:的|$)", query)
    if dimension_match:
        dimensions.append(IntentSlot(text=dimension_match.group(1), role="dimension"))

    if any(word in query for word in ("没有", "不存在", "未发生", "无")) and len(entities) > 1:
        query_type = "existence"
    elif len(measures) > 1:
        query_type = "multi_fact"
    elif filters:
        query_type = "fact_filter"
    elif any(word in query for word in ("统计", "合计", "平均", "总计", "数量", "多少")):
        query_type = "aggregation"
    elif "明细" in query:
        query_type = "event_detail"
    elif entities or attributes:
        query_type = "attribute_lookup"
    else:
        query_type = "unknown"
    return QueryIntent(
        query_type=query_type,
        entities=entities,
        measures=measures,
        attributes=attributes,
        filters=filters,
        dimensions=dimensions,
    )


def _phrase_scores(slot: str, column: dict) -> tuple[float, float]:
    wanted = normalize_semantic_text(slot)
    field_text = normalize_semantic_text(
        f"{column.get('name', '')}{column.get('comment', '')}"
    )
    if not wanted or not field_text:
        return 0.0, 0.0
    if wanted in field_text:
        return 1.0, 1.0
    for concept, aliases in _CONCEPT_ALIASES.items():
        if concept in wanted and any(normalize_semantic_text(alias) in field_text for alias in aliases):
            return 0.9, 0.85
    overlap = SequenceMatcher(None, wanted, field_text).find_longest_match().size / max(1, len(wanted))
    # 完整短语最重要；连续片段覆盖只作为召回证据，避免字符集合把
    # “逾期总金额”和“逾期本金余额”误判为完全相同。
    return overlap, overlap * overlap


def _is_numeric(column: dict) -> bool:
    raw = str(column.get("type") or column.get("raw_type") or "").lower()
    return any(raw.startswith(prefix) for prefix in _NUMERIC_TYPES)


def rank_field_candidates(
    intent: QueryIntent,
    tables: Iterable[TableDef],
    column_table_scores: dict[str, float] | None = None,
) -> list[FieldCandidate]:
    """为每个度量/过滤槽位独立排序字段，列级命中可独立产生表候选。"""
    column_table_scores = column_table_scores or {}
    slots = {
        slot.text: slot
        for slot in [*intent.measures, *intent.filters, *intent.dimensions]
        if slot.text
    }
    candidates: list[FieldCandidate] = []
    for slot_text, slot in slots.items():
        numeric_required = slot.role == "measure" or isinstance(slot.value, (int, float)) or any(
            item.text == slot_text and isinstance(item.value, (int, float))
            for item in intent.filters
        )
        for table in tables:
            entity_affinity = max(
                (_entity_table_score(entity.text, table) for entity in intent.entities),
                default=0.0,
            )
            for column in table.columns:
                if numeric_required and not _is_numeric(column):
                    continue
                coverage, lexical = _phrase_scores(slot_text, column)
                role = str(column.get("semantic_role") or column.get("dim_or_meas") or "")
                if numeric_required:
                    role_score = 1.0 if _is_numeric(column) and role in ("measure", "") else 0.35
                else:
                    role_score = 1.0 if not _is_numeric(column) or role == "dimension" else 0.5
                vector = max(0.0, float(column_table_scores.get(table.name, 0.0)))
                score = (
                    0.30 * lexical + 0.25 * coverage + 0.20 * vector
                    + 0.15 * role_score + 0.10 * entity_affinity
                )
                if coverage <= 0.25 and vector <= 0.25:
                    continue
                evidence = []
                if coverage >= 0.99:
                    evidence.append("字段名称或注释覆盖查询短语")
                if numeric_required and _is_numeric(column):
                    evidence.append("字段类型满足数值比较")
                if role == "measure":
                    evidence.append("字段角色为度量")
                if entity_affinity >= 0.9:
                    evidence.append("字段所在表同时匹配目标实体")
                candidates.append(FieldCandidate(
                    table_name=table.name,
                    column_name=str(column.get("name") or ""),
                    query_slot=slot_text,
                    semantic_role=role,
                    data_type=str(column.get("type") or column.get("raw_type") or ""),
                    vector_score=vector,
                    lexical_score=lexical,
                    phrase_coverage=coverage,
                    type_role_score=role_score,
                    final_score=min(1.0, score),
                    evidence=evidence,
                ))
    return sorted(candidates, key=lambda item: (-item.final_score, item.table_name, item.column_name))


def find_field_ambiguities(
    candidates: list[FieldCandidate],
    overrides: dict[str, str] | None = None,
    *,
    relative_gap: float = 0.12,
    minimum_score: float = 0.5,
) -> dict[str, list[FieldCandidate]]:
    """只把同一业务槽位的近分字段视为口径歧义，不比较不同角色的表。"""
    overrides = overrides or {}
    by_slot: dict[str, list[FieldCandidate]] = {}
    for candidate in candidates:
        by_slot.setdefault(candidate.query_slot, []).append(candidate)
    ambiguities: dict[str, list[FieldCandidate]] = {}
    for slot, options in by_slot.items():
        if (
            slot in overrides
            or len(options) < 2
            or options[0].final_score < minimum_score
            or options[0].phrase_coverage < 0.75
        ):
            continue
        top_score = options[0].final_score
        close = [
            item for item in options
            if item.final_score >= minimum_score
            and item.phrase_coverage >= 0.65
            and (top_score - item.final_score) / max(top_score, 1e-9) < relative_gap
        ]
        distinct = {(item.table_name, item.column_name) for item in close}
        if len(distinct) > 1:
            ambiguities[slot] = close[:5]
    return ambiguities


def _entity_table_score(entity: str, table: TableDef) -> float:
    wanted = normalize_semantic_text(entity)
    comment = normalize_semantic_text(table.comment)
    if wanted and wanted in comment:
        return 0.95
    column_text = normalize_semantic_text("".join(str(c.get("comment", "")) for c in table.columns))
    if wanted and wanted in column_text:
        return 0.55
    return 0.0


def _usable_relation(relation: dict) -> bool:
    relation_type = str(relation.get("relation_type") or "foreign_key")
    status = str(relation.get("status") or "")
    return relation_type == "foreign_key" or status in {"verified", "approved"}


def _shortest_path(
    start: str, target: str, relations: list[dict], allowed_tables: set[str], max_hops: int,
) -> tuple[list[str], list[dict]]:
    adjacency: dict[str, list[tuple[str, dict]]] = {}
    for relation in relations:
        left = str(relation.get("source_table") or "")
        right = str(relation.get("target_table") or "")
        if not _usable_relation(relation) or left not in allowed_tables or right not in allowed_tables:
            continue
        adjacency.setdefault(left, []).append((right, relation))
        adjacency.setdefault(right, []).append((left, relation))
    queue = deque([(start, [start], [])])
    visited = {start}
    while queue:
        node, path, edges = queue.popleft()
        if len(edges) >= max_hops:
            continue
        for neighbor, relation in adjacency.get(node, []):
            if neighbor in visited:
                continue
            next_path = [*path, neighbor]
            next_edges = [*edges, relation]
            if neighbor == target:
                return next_path, next_edges
            visited.add(neighbor)
            queue.append((neighbor, next_path, next_edges))
    return [], []


def build_schema_plan(
    intent: QueryIntent,
    tables: list[TableDef],
    candidates: list[FieldCandidate],
    relations: list[dict],
    *,
    overrides: dict[str, str] | None = None,
    max_hops: int = 3,
) -> SchemaPlan:
    """选择字段证据最强、表数量最少且关系连通的 Schema 子图。"""
    overrides = overrides or {}
    by_slot: dict[str, list[FieldCandidate]] = {}
    for candidate in candidates:
        by_slot.setdefault(candidate.query_slot, []).append(candidate)

    selected: list[FieldCandidate] = []
    unresolved: list[str] = []
    ordered_slots = list(dict.fromkeys(
        item.text
        for item in [*intent.measures, *intent.filters, *intent.dimensions]
        if item.text
    ))
    for slot in ordered_slots:
        options = by_slot.get(slot, [])
        override = overrides.get(slot)
        if override:
            options = [item for item in options if f"{item.table_name}.{item.column_name}" == override]
        if not options or options[0].final_score < 0.42:
            unresolved.append(slot)
            continue
        selected.append(options[0])
        if options[0].phrase_coverage < 0.65:
            unresolved.append(f"字段证据不足:{slot}")

    table_by_name = {table.name: table for table in tables}
    anchors: dict[str, PlannedTable] = {}
    dimensions: dict[str, PlannedTable] = {}
    dimension_slots = {item.text for item in intent.dimensions}
    for candidate in selected:
        if candidate.query_slot in dimension_slots:
            existing_dimension = dimensions.get(candidate.table_name)
            if existing_dimension:
                if candidate.column_name not in existing_dimension.selected_columns:
                    existing_dimension.selected_columns.append(candidate.column_name)
                existing_dimension.score = max(existing_dimension.score, candidate.final_score)
            else:
                dimensions[candidate.table_name] = PlannedTable(
                    table_name=candidate.table_name,
                    role="dimension",
                    selected_columns=[candidate.column_name],
                    reason=f"提供分组维度“{candidate.query_slot}”",
                    score=candidate.final_score,
                )
            continue
        existing = anchors.get(candidate.table_name)
        if existing:
            if candidate.column_name not in existing.selected_columns:
                existing.selected_columns.append(candidate.column_name)
            existing.score = max(existing.score, candidate.final_score)
        else:
            anchors[candidate.table_name] = PlannedTable(
                table_name=candidate.table_name,
                role=(
                    "primary_fact" if intent.measures and not anchors
                    else "secondary_fact" if intent.measures
                    else "entity"
                ),
                selected_columns=[candidate.column_name],
                reason=f"承载查询字段“{candidate.query_slot}”",
                score=candidate.final_score,
            )

    for entity in intent.entities:
        ranked = sorted(
            ((table, _entity_table_score(entity.text, table)) for table in tables),
            key=lambda pair: (-pair[1], pair[0].name),
        )
        if not ranked or ranked[0][1] < 0.5:
            unresolved.append(entity.text)
            continue
        table, score = ranked[0]
        if table.name not in anchors:
            key_columns = [
                str(column.get("name")) for column in table.columns
                if column.get("primary_key") or entity.text in str(column.get("comment", ""))
            ][:8]
            if table.name in dimensions:
                dimensions[table.name].selected_columns = list(dict.fromkeys([
                    *dimensions[table.name].selected_columns,
                    *key_columns,
                ]))
                dimensions[table.name].reason += f"；提供“{entity.text}”实体属性"
                dimensions[table.name].score = max(dimensions[table.name].score, score)
            else:
                dimensions[table.name] = PlannedTable(
                    table_name=table.name,
                    role="entity",
                    selected_columns=key_columns,
                    reason=f"提供“{entity.text}”实体属性",
                    score=score,
                )

    bridges: dict[str, PlannedTable] = {}
    selected_relations: list[dict] = []
    connected_targets = list(anchors) + list(dimensions)
    if len(connected_targets) > 1:
        root = connected_targets[0]
        for target in connected_targets[1:]:
            path, edges = _shortest_path(root, target, relations, set(table_by_name), max_hops)
            if not path:
                unresolved.append(f"关联路径:{root}->{target}")
                continue
            selected_relations.extend(edge for edge in edges if edge not in selected_relations)
            for table_name in path[1:-1]:
                if table_name not in anchors and table_name not in dimensions:
                    relation_columns: list[str] = []
                    for edge in edges:
                        if edge.get("source_table") == table_name:
                            relation_columns.extend(edge.get("source_columns", []))
                        if edge.get("target_table") == table_name:
                            relation_columns.extend(edge.get("target_columns", []))
                    bridges[table_name] = PlannedTable(
                        table_name=table_name,
                        role="bridge",
                        selected_columns=list(dict.fromkeys(relation_columns)),
                        reason=f"连接 {root} 与 {target}",
                        score=1.0,
                    )

    scores = [item.score for item in [*anchors.values(), *dimensions.values()]]
    confidence = sum(scores) / len(scores) if scores else 0.0
    if unresolved:
        confidence *= 0.65
    return SchemaPlan(
        anchor_tables=list(anchors.values()),
        dimension_tables=list(dimensions.values()),
        bridge_tables=list(bridges.values()),
        relations=selected_relations,
        unresolved_slots=list(dict.fromkeys(unresolved)),
        confidence=min(1.0, confidence),
    )


def plan_table_names(plan: SchemaPlan) -> list[str]:
    return list(dict.fromkeys([
        *(item.table_name for item in plan.anchor_tables),
        *(item.table_name for item in plan.bridge_tables),
        *(item.table_name for item in plan.dimension_tables),
    ]))
