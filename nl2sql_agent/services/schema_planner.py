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
    SemanticGraph,
    SchemaPlan,
)


_COMPARISONS = {
    "不低于": ">=", "至少": ">=", "超过": ">", "大于": ">", "高于": ">",
    "不超过": "<=", "至多": "<=", "低于": "<", "少于": "<", "等于": "=",
}
_COMPARISON_RE = re.compile(
    rf"(?P<field>[\u4e00-\u9fffA-Za-z_][\u4e00-\u9fffA-Za-z_ ]{{0,23}}?)"
    rf"(?P<word>{'|'.join(sorted(_COMPARISONS, key=len, reverse=True))})"
    rf"\s*(?P<value>-?\d+(?:\.\d+)?)"
)
_TEXT_FILTER_RE = re.compile(
    r"(?P<field>[\u4e00-\u9fffA-Za-z_]{1,16}?)(?P<word>等于|为|是)"
    r"\s*(?P<value>[\u4e00-\u9fffA-Za-z0-9_.-]{1,32}?)(?:的|并且|且|$)"
)
_MEASURE_RE = re.compile(
    r"[\u4e00-\u9fffA-Za-z_]{1,18}?(?:金额|余额|总额|数量|笔数|比率|率)"
)
_ENTITY_WORDS = ("客户", "产品", "机构", "借据", "合同", "申请", "贷款", "还款", "代偿")
_GENERIC_PROJECTIONS = {"基本信息", "详细信息", "联系方式", "明细"}
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


def is_generic_projection(value: str) -> bool:
    """Match both bare and entity-qualified vague projections."""
    text = str(value or "")
    return (
        any(projection in text for projection in _GENERIC_PROJECTIONS)
        or any(f"{entity}信息" in text for entity in _ENTITY_WORDS)
    )


def _fallback_output_phrases(query: str) -> list[str]:
    """Conservative safety net for explicit result lists when the LLM is unavailable.

    This is intentionally not a business dictionary. It only recognizes a trailing
    natural-language list such as ``客户的姓名和地址``. Open-vocabulary output
    understanding remains the responsibility of the resolution model.
    """
    tail = query.rsplit("的", 1)[-1].strip(" ，。！？?") if "的" in query else ""
    if (
        not tail
        or tail in _ENTITY_WORDS
        or any(word in tail for word in _COMPARISONS)
        or re.search(r"\d", tail)
    ):
        return []
    parts = [item.strip(" 的") for item in re.split(r"以及|和|与|及|、", tail)]
    # A lone open-vocabulary tail (e.g. “逾期本金”“借据笔数”) may be a metric,
    # not a projection. The model handles that case; fallback only protects an
    # unmistakable explicit list. Generic entity projections are handled by the
    # existing entity policy rather than pretending they are one physical field.
    if len(parts) < 2:
        return []
    cleaned: list[str] = []
    for item in parts:
        # 保留“客户姓名”“借据编号”等实体限定词。去掉限定词会把明确的
        # 输出要求退化成“姓名”“编号”，从而在宽表中误选其他同名字段。
        if 1 <= len(item) <= 16 and item not in _ENTITY_WORDS:
            cleaned.append(item)
    return cleaned


def normalize_semantic_text(value: str) -> str:
    text = re.sub(r"[\s\-_./()（）:：,，]+", "", str(value or ""))
    for source, target in _GENERIC_ALIASES:
        text = text.replace(source, target)
    return text.lower()


def _clean_measure_phrase(value: str) -> str:
    text = value.strip()
    # 连续条件中的金额单位和连接词属于上一条件，例如
    # “贷款金额超过1000元且逾期本金余额大于0”。
    text = re.sub(r"^(?:亿元|万元|元)?(?:并且|且)", "", text)
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
    for entity in entities:
        phrase = f"{entity.text}信息"
        if phrase in query and phrase not in {item.text for item in attributes}:
            attributes.append(IntentSlot(text=phrase, role="attribute"))
    for phrase in _fallback_output_phrases(query):
        if phrase not in {item.text for item in attributes}:
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
        for slot in [*intent.measures, *intent.filters, *intent.attributes, *intent.dimensions]
        if slot.text and not is_generic_projection(slot.text)
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
                    column_comment=str(column.get("comment") or ""),
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


def resolve_anchor_table_ambiguities(
    ambiguities: dict[str, list[FieldCandidate]],
    schema_plan: SchemaPlan,
) -> dict[str, list[FieldCandidate]]:
    """Remove cross-table ambiguity already resolved by a strong fact anchor.

    Repeated codes such as PRD_CODE commonly occur in several event tables. If
    the query's other measures establish one primary fact table and that table
    has one strong exact candidate for the slot, asking the user to choose a
    physical table adds no business information.
    """
    primary_facts = {
        item.table_name for item in schema_plan.anchor_tables
        if item.role == "primary_fact"
    }
    if len(primary_facts) != 1:
        return ambiguities
    primary_fact = next(iter(primary_facts))
    unresolved: dict[str, list[FieldCandidate]] = {}
    for slot, options in ambiguities.items():
        top = options[0] if options else None
        same_table = [item for item in options if item.table_name == primary_fact]
        context_resolved = bool(
            top
            and top.table_name == primary_fact
            and top.final_score >= 0.75
            and top.phrase_coverage >= 0.9
            and len({item.column_name for item in same_table}) == 1
        )
        if not context_resolved:
            unresolved[slot] = options
    return unresolved


def prefer_primary_fact_fields(
    candidates: list[FieldCandidate],
    plan: SchemaPlan | None,
    *,
    bonus: float = 0.25,
) -> list[FieldCandidate]:
    """Rerank duplicate business fields toward the selected primary fact.

    This resolves physical placement (for example several ``产品编码`` columns)
    internally after the measure anchor is known; it does not decide between
    genuinely different business definitions.
    """
    if plan is None:
        return candidates
    primary = next((
        item.table_name for item in plan.anchor_tables if item.role == "primary_fact"
    ), None)
    if not primary:
        return candidates
    reranked = [
        item.model_copy(update={
            "final_score": min(1.0, item.final_score + bonus),
            "evidence": list(dict.fromkeys([
                *item.evidence, "字段位于已确定的主事实表",
            ])),
        }) if item.table_name == primary and item.phrase_coverage >= 0.75 and item.semantic_role in {
            "attribute", "dimension", "entity",
        } else item
        for item in candidates
    ]
    return sorted(reranked, key=lambda item: -item.final_score)


def prefer_minimal_table_cover(
    candidates: list[FieldCandidate],
    intent: QueryIntent,
    *,
    max_bonus: float = 0.18,
) -> list[FieldCandidate]:
    """Prefer a coherent table that covers more requested query slots.

    Column-level relevance can otherwise select a different physical table for
    every field even when one slightly lower-scoring detail table contains all
    requested fields. This bounded set-cover tie-breaker changes only ordering;
    it does not inflate semantic confidence or invent relations.
    """
    slots = list(dict.fromkeys(
        item.text
        for item in [*intent.measures, *intent.filters, *intent.attributes, *intent.dimensions]
        if item.text and not is_generic_projection(item.text)
    ))
    if len(slots) < 2:
        return candidates

    requested = set(slots)
    covered_by_table: dict[str, set[str]] = {}
    for item in candidates:
        if (
            item.query_slot in requested
            and item.final_score >= 0.65
            and item.phrase_coverage >= 0.75
        ):
            covered_by_table.setdefault(item.table_name, set()).add(item.query_slot)

    slot_count = len(requested)

    def coherence_bonus(item: FieldCandidate) -> float:
        coverage = len(covered_by_table.get(item.table_name, set())) / slot_count
        return max_bonus * coverage * coverage

    reranked = sorted(
        candidates,
        key=lambda item: (
            -(item.final_score + coherence_bonus(item)),
            item.table_name,
            item.column_name,
        ),
    )
    return [
        item.model_copy(update={
            "evidence": list(dict.fromkeys([
                *item.evidence,
                "字段所在表可同时覆盖更多查询要求",
            ])),
        }) if len(covered_by_table.get(item.table_name, set())) > 1 else item
        for item in reranked
    ]


def ground_output_bindings(
    graph: SemanticGraph | None,
    candidates: list[FieldCandidate],
    overrides: dict[str, str] | None = None,
    tables: list[TableDef] | None = None,
) -> dict[str, dict]:
    """Bind required outputs; broad result-only concepts may expand safely."""
    if graph is None:
        return {}
    overrides = overrides or {}
    by_slot: dict[str, list[FieldCandidate]] = {}
    for candidate in candidates:
        by_slot.setdefault(candidate.query_slot, []).append(candidate)

    # Grouping dimensions should normally live with the measures they group.
    # Infer the dominant fact table from high-confidence aggregate outputs so
    # duplicate dimension names in unrelated event tables do not win merely
    # because another column happens to be a primary key.
    fact_table_votes: dict[str, int] = {}
    for output in graph.outputs:
        if not output.required or not output.aggregation or output.aggregation == "count_distinct":
            continue
        concept = output.grounding_concept or output.concept
        metric_options = by_slot.get(concept, [])
        if not metric_options:
            continue
        selected_metric = max(metric_options, key=lambda item: item.final_score)
        fact_table_votes[selected_metric.table_name] = (
            fact_table_votes.get(selected_metric.table_name, 0) + 1
        )
    preferred_fact_tables = {
        table for table, votes in fact_table_votes.items()
        if votes == max(fact_table_votes.values(), default=0)
    }

    bindings: dict[str, dict] = {}
    for output in graph.outputs:
        if not output.required:
            continue
        concept = output.grounding_concept or output.concept
        if output.aggregation == "count_distinct" and tables:
            grain = normalize_semantic_text(output.distinct_grain or concept)
            identifiers: list[tuple[float, TableDef, dict]] = []
            for table in tables:
                table_text = normalize_semantic_text(f"{table.name}{table.comment}")
                table_affinity = 0.35 if grain and grain in table_text else 0.0
                for column in table.columns:
                    name = str(column.get("name") or "")
                    comment = str(column.get("comment") or "")
                    column_text = normalize_semantic_text(f"{name}{comment}")
                    identifier_hint = any(token in comment for token in ("编号", "编码", "号码", "号"))
                    score = table_affinity
                    if column.get("primary_key"):
                        score += 1.0
                    elif column.get("unique"):
                        score += 0.85
                    elif identifier_hint:
                        score += 0.45
                    if grain and grain in column_text:
                        score += 0.35
                    if re.search(r"(?:^|_)(?:id|no|code)$", name, re.IGNORECASE):
                        score += 0.2
                    if score >= 0.65:
                        identifiers.append((score, table, column))
            if identifiers:
                score, table, column = max(
                    identifiers,
                    key=lambda item: (item[0], bool(item[2].get("primary_key")), bool(item[2].get("unique"))),
                )
                bindings[output.id] = {
                    "concept": output.concept,
                    "grounding_concept": concept,
                    "table_name": table.name,
                    "column_name": str(column.get("name") or ""),
                    "confidence": min(1.0, score),
                    "required": True,
                    "aggregation": output.aggregation,
                    "distinct_grain": output.distinct_grain,
                    "binding_mode": "derived",
                    "bindings": [{
                        "table_name": table.name,
                        "column_name": str(column.get("name") or ""),
                        "label": output.concept,
                        "confidence": min(1.0, score),
                    }],
                }
                continue
        options = by_slot.get(concept, [])
        override = overrides.get(concept)
        if override:
            options = [
                item for item in options
                if f"{item.table_name}.{item.column_name}" == override
            ]
        # A grouping entity is a result grain, not merely a readable label.
        # Prefer a non-null primary/unique identifier over another semantically
        # similar code (for example CUST_ID over a nullable source CORE_NO).
        is_grouping_entity = any(
            normalize_semantic_text(group) == normalize_semantic_text(concept)
            for group in graph.group_by
        ) and not output.aggregation
        if is_grouping_entity and tables and not override:
            column_meta = {
                (table.name, str(column.get("name") or "")): column
                for table in tables
                for column in table.columns
            }
            # First remove weak semantic matches.  Only then may identifier
            # quality break ties; otherwise an unrelated PK can displace a
            # strong category key such as PRD_CODE for “每个产品”.
            top_semantic_score = max((item.final_score for item in options), default=0.0)
            semantic_floor = max(0.42, top_semantic_score * 0.85)
            relevant_options = [
                item for item in options
                if item.final_score >= semantic_floor
                and item.phrase_coverage >= 0.65
            ]
            if relevant_options:
                options = relevant_options
            options = sorted(
                options,
                key=lambda item: (
                    item.table_name in preferred_fact_tables,
                    bool(column_meta.get((item.table_name, item.column_name), {}).get("primary_key")),
                    bool(column_meta.get((item.table_name, item.column_name), {}).get("unique")),
                    not bool(column_meta.get((item.table_name, item.column_name), {}).get("nullable", True)),
                    item.final_score,
                ),
                reverse=True,
            )
        if not options or options[0].final_score < 0.42:
            continue
        selected = options[0]
        selected_fields = [selected]
        normalized_concept = normalize_semantic_text(concept)
        if normalized_concept == "地址" and not override:
            # “地址”是宽泛返回概念，可展开客户本人同表的完整地址字段。
            # 排除单位/配偶地址以及省市区、邮编等不同主体或不同粒度字段。
            excluded = ("单位", "工作", "配偶", "省份", "省", "城市", "市", "区县", "区", "邮编")
            expanded = [
                item for item in options
                if item.table_name == selected.table_name
                and item.final_score >= max(0.42, selected.final_score * 0.82)
                and item.phrase_coverage >= 0.75
                and "地址" in item.column_comment
                and not any(word in item.column_comment for word in excluded)
            ]
            if expanded:
                selected_fields = expanded[:5]
        physical_bindings = [
            {
                "table_name": item.table_name,
                "column_name": item.column_name,
                "label": item.column_comment or output.concept,
                "confidence": item.final_score,
            }
            for item in selected_fields
        ]
        bindings[output.id] = {
            "concept": output.concept,
            "grounding_concept": concept,
            "table_name": selected.table_name,
            "column_name": selected.column_name,
            "confidence": selected.final_score,
            "required": True,
            "aggregation": output.aggregation,
            "distinct_grain": output.distinct_grain,
            "binding_mode": "expanded" if len(physical_bindings) > 1 else "exact",
            "bindings": physical_bindings,
        }
    return bindings


def output_binding_fields(binding: dict) -> list[dict]:
    """Read new one-to-many bindings while remaining compatible with old states."""
    fields = binding.get("bindings")
    if isinstance(fields, list) and fields:
        return [item for item in fields if isinstance(item, dict)]
    if binding.get("table_name") and binding.get("column_name"):
        return [binding]
    return []


def _entity_table_score(entity: str, table: TableDef) -> float:
    wanted = normalize_semantic_text(entity)
    comment = normalize_semantic_text(table.comment)
    if wanted and wanted in comment:
        return 0.95
    column_text = normalize_semantic_text("".join(str(c.get("comment", "")) for c in table.columns))
    if wanted and wanted in column_text:
        return 0.55
    return 0.0


def _profile_table_score(table: TableDef) -> float:
    """Prefer readable entity profiles over measure-heavy aggregate tables."""
    readable = [
        column for column in table.columns
        if column.get("comment")
        and not column.get("primary_key")
        and not _is_numeric(column)
    ]
    return min(0.25, len(readable) / 12 * 0.25)


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

    dimension_slots = {item.text for item in intent.dimensions}
    attribute_slots = {item.text for item in intent.attributes}
    fact_slots = {item.text for item in [*intent.measures, *intent.filters]}
    # If every factual measure/filter points to the same table, that table is
    # the established row grain. Prefer an equally strong grouping/output key
    # on it instead of adding a second event table solely for a duplicated code.
    fact_context_tables = {
        by_slot[slot][0].table_name
        for slot in fact_slots
        if by_slot.get(slot) and by_slot[slot][0].final_score >= 0.65
    }
    fact_context_table = (
        next(iter(fact_context_tables)) if len(fact_context_tables) == 1 else None
    )

    selected: list[FieldCandidate] = []
    unresolved: list[str] = []
    ordered_slots = list(dict.fromkeys(
        item.text
        for item in [*intent.measures, *intent.filters, *intent.attributes, *intent.dimensions]
        if item.text and not is_generic_projection(item.text)
    ))
    for slot in ordered_slots:
        options = by_slot.get(slot, [])
        override = overrides.get(slot)
        if override:
            options = [item for item in options if f"{item.table_name}.{item.column_name}" == override]
        elif fact_context_table and slot in dimension_slots | attribute_slots and options:
            semantic_floor = max(0.65, options[0].final_score * 0.85)
            contextual = [
                item for item in options
                if item.table_name == fact_context_table
                and item.final_score >= semantic_floor
                and item.phrase_coverage >= 0.65
            ]
            if contextual:
                options = [max(contextual, key=lambda item: item.final_score)]
        if not options or options[0].final_score < 0.42:
            unresolved.append(slot)
            continue
        selected.append(options[0])
        if options[0].phrase_coverage < 0.65:
            unresolved.append(f"字段证据不足:{slot}")

    table_by_name = {table.name: table for table in tables}
    anchors: dict[str, PlannedTable] = {}
    dimensions: dict[str, PlannedTable] = {}
    for candidate in selected:
        # 同一字段可同时出现在 WHERE 与 SELECT 中；筛选/度量的事实角色
        # 优先，不能因为它也是返回属性就把事实锚点降级为维表。
        is_result_dimension = (
            candidate.query_slot in dimension_slots
            or candidate.query_slot in attribute_slots
        ) and candidate.query_slot not in fact_slots
        if is_result_dimension:
            existing_dimension = dimensions.get(candidate.table_name)
            if existing_dimension:
                if candidate.column_name not in existing_dimension.selected_columns:
                    existing_dimension.selected_columns.append(candidate.column_name)
                existing_dimension.score = max(existing_dimension.score, candidate.final_score)
            else:
                dimensions[candidate.table_name] = PlannedTable(
                    table_name=candidate.table_name,
                    role="entity" if candidate.query_slot in attribute_slots else "dimension",
                    selected_columns=[candidate.column_name],
                    reason=(
                        f"提供返回属性“{candidate.query_slot}”"
                        if candidate.query_slot in attribute_slots
                        else f"提供分组维度“{candidate.query_slot}”"
                    ),
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

    wants_profile = any(is_generic_projection(item.text) for item in intent.attributes)
    for entity in intent.entities:
        # 明确列举返回字段时，如果已选字段所在的事实表已经覆盖该实体，
        # 就直接复用该表；只有“基本信息”等宽泛投影才主动寻找画像表。
        entity_text = normalize_semantic_text(entity.text)
        covered_by_anchor = any(
            candidate.table_name in anchors
            and (
                entity_text in normalize_semantic_text(candidate.column_comment)
                or entity_text in normalize_semantic_text(
                    table_by_name[candidate.table_name].comment
                )
            )
            for candidate in selected
        )
        if covered_by_anchor and not wants_profile:
            continue

        def entity_rank(table: TableDef) -> float:
            score = _entity_table_score(entity.text, table)
            if wants_profile:
                score += _profile_table_score(table)
                if table.name in anchors:
                    score += 0.18
                elif any(
                    _shortest_path(
                        anchor_name, table.name, relations, set(table_by_name), max_hops
                    )[0]
                    for anchor_name in anchors
                ):
                    score += 0.22
            return score

        ranked = sorted(
            ((table, entity_rank(table)) for table in tables),
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

    # 同一物理表可能同时承载筛选度量和返回属性。关系规划前将它合并为
    # 一个事实锚点，避免产生 table -> table 的伪自关联路径。
    for table_name in set(anchors) & set(dimensions):
        dimension = dimensions.pop(table_name)
        anchor = anchors[table_name]
        anchor.selected_columns = list(dict.fromkeys([
            *anchor.selected_columns,
            *dimension.selected_columns,
        ]))
        anchor.score = max(anchor.score, dimension.score)
        anchor.reason = f"{anchor.reason}；{dimension.reason}"

    bridges: dict[str, PlannedTable] = {}
    selected_relations: list[dict] = []
    connected_targets = list(dict.fromkeys([*anchors, *dimensions]))
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


def extend_schema_plan_for_output_bindings(
    plan: SchemaPlan,
    bindings: dict[str, dict],
    tables: list[TableDef],
    relations: list[dict],
    *,
    max_hops: int = 3,
) -> SchemaPlan:
    """Ensure every grounded required output belongs to the planned relation subgraph."""
    updated = plan.model_copy(deep=True)
    table_by_name = {table.name: table for table in tables}
    planned = {
        item.table_name: item
        for item in [
            *updated.anchor_tables, *updated.dimension_tables, *updated.bridge_tables,
        ]
    }
    required_by_table: dict[str, list[str]] = {}
    for binding in bindings.values():
        for field in output_binding_fields(binding):
            table = str(field.get("table_name") or "")
            column = str(field.get("column_name") or "")
            if table and column:
                required_by_table.setdefault(table, []).append(column)

    root = next(iter(planned), None)
    selected_relations = list(updated.relations)
    for table_name, columns in required_by_table.items():
        if table_name in planned:
            planned[table_name].selected_columns = list(dict.fromkeys([
                *planned[table_name].selected_columns, *columns,
            ]))
            continue
        if table_name not in table_by_name:
            updated.unresolved_slots.append(f"输出字段表不存在:{table_name}")
            continue
        if root is None:
            root = table_name
            path, edges = [table_name], []
        else:
            path, edges = _shortest_path(
                root, table_name, relations, set(table_by_name), max_hops
            )
        if not path:
            updated.unresolved_slots.append(f"输出关联路径:{root}->{table_name}")
            continue
        selected_relations.extend(edge for edge in edges if edge not in selected_relations)
        for path_table in path[1:-1]:
            if path_table in planned:
                continue
            relation_columns: list[str] = []
            for edge in edges:
                if edge.get("source_table") == path_table:
                    relation_columns.extend(edge.get("source_columns", []))
                if edge.get("target_table") == path_table:
                    relation_columns.extend(edge.get("target_columns", []))
            bridge = PlannedTable(
                table_name=path_table,
                role="bridge",
                selected_columns=list(dict.fromkeys(relation_columns)),
                reason=f"连接 {root} 与 {table_name}",
                score=1.0,
            )
            updated.bridge_tables.append(bridge)
            planned[path_table] = bridge
        anchor = PlannedTable(
            table_name=table_name,
            role="secondary_fact" if updated.anchor_tables else "primary_fact",
            selected_columns=list(dict.fromkeys(columns)),
            reason="承载已确认的用户返回字段",
            score=float(bindings[next(
                key for key, value in bindings.items()
                if any(
                    field.get("table_name") == table_name
                    for field in output_binding_fields(value)
                )
            )].get("confidence") or 0.75),
        )
        updated.anchor_tables.append(anchor)
        planned[table_name] = anchor

    # Relation keys are part of the minimal query schema even when they were not
    # direct user outputs.
    for relation in selected_relations:
        for table_name, columns in (
            (relation.get("source_table"), relation.get("source_columns", [])),
            (relation.get("target_table"), relation.get("target_columns", [])),
        ):
            if table_name in planned:
                planned[table_name].selected_columns = list(dict.fromkeys([
                    *planned[table_name].selected_columns, *columns,
                ]))
    updated.relations = selected_relations
    updated.unresolved_slots = list(dict.fromkeys(updated.unresolved_slots))
    return updated
