"""Schema-independent query semantics enrichment.

The model may use open business vocabulary, while this module preserves the
small, universal SQL semantics that must never disappear during rewriting:
aggregation, grouping, ordering, Top-N and explicit output lists.
"""

from __future__ import annotations

import re

from nl2sql_agent.state import SemanticGraph, SemanticOrder, SemanticOutput, SemanticSubject


_OUTPUT_MARKER_RE = re.compile(r"(?:返回|展示|列出|输出|显示)(.+?)(?:[。；;]|$)")
_SPLIT_RE = re.compile(r"、|，|,|以及|及|和")
_GROUP_RE = re.compile(r"(?:统计|查询|计算)?每个([\u4e00-\u9fffA-Za-z_]{1,12}?)(?:的|累计|贷款|代偿|还款)")
_GROUP_BY_RE = re.compile(r"按([\u4e00-\u9fffA-Za-z_]{1,12}?)(?:维度)?(?:统计|汇总|分组|计算)")
_LIMIT_RE = re.compile(
    r"(?:前\s*|top\s*)(?P<prefix_limit>\d+)"
    r"|(?:限制)?(?:返回|取|保留)\s*(?P<return_limit>\d+)\s*(?:条|个|行|笔|名)?",
    re.IGNORECASE,
)
_ORDER_RE = re.compile(r"([\u4e00-\u9fffA-Za-z_]{2,18}?)(最高|最大|最多|最低|最小|最少)")
_DIRECTIONAL_ORDER_RES = (
    re.compile(
        r"(?:按|依据|根据)([\u4e00-\u9fffA-Za-z_]{1,20}?)(?:从)?"
        r"(高到低|低到高|降序|升序)(?:排列|排序)?"
    ),
    re.compile(
        r"([\u4e00-\u9fffA-Za-z_]{2,20}?)(降序|升序)(?:排列|排序)?"
    ),
)
_TOP_N_OUTPUT_RE = re.compile(
    r"^(?:(?:前|top)\d+(?:个|条|名|笔)?"
    r"(?:客户|产品|机构|借据|合同|申请|贷款|还款|代偿|记录|结果)?"
    r"|(?:限制)?(?:返回)?\d+(?:条|个|名|笔)(?:记录|结果)?)$",
    re.IGNORECASE,
)


def _is_query_control_output(value: str) -> bool:
    text = _clean_phrase(value)
    if _TOP_N_OUTPUT_RE.fullmatch(text):
        return True
    if any(pattern.fullmatch(text) for pattern in _DIRECTIONAL_ORDER_RES):
        return True
    return bool(
        _LIMIT_RE.search(text)
        and any(word in text for word in ("高到低", "低到高", "降序", "升序"))
    )


def _clean_phrase(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "")).strip("，。；;、")
    text = re.sub(r"^(?:请|帮我|查询|统计|计算|返回|展示|列出|输出|显示)", "", text)
    return text.strip("的")


def metric_semantics(label: str, *, grouped: bool = False) -> tuple[str, str | None, str | None]:
    """Return (base concept, aggregation, distinct grain)."""
    concept = _clean_phrase(label)
    if concept.endswith(("笔数", "数量", "个数")):
        suffix = next(item for item in ("笔数", "数量", "个数") if concept.endswith(item))
        grain = concept[: -len(suffix)] or concept
        return grain, "count_distinct", grain
    for prefix, aggregation in (
        ("平均", "avg"), ("累计", "sum"), ("合计", "sum"),
        ("总计", "sum"), ("最大", "max"), ("最高", "max"),
        ("最小", "min"), ("最低", "min"),
    ):
        if concept.startswith(prefix) and len(concept) > len(prefix):
            return concept[len(prefix):], aggregation, None
    if "总金额" in concept:
        return concept.replace("总金额", "金额"), "sum", None
    # “代偿总额” can be a real base field; only an outer 累计/合计 modifier
    # changes it. Generic grouped balances are additive unless the user asks
    # for an average/min/max explicitly.
    aliases = {
        "剩余本金": "贷款本金余额",
        "剩余贷款本金": "贷款本金余额",
    }
    base = aliases.get(concept, concept)
    if grouped and any(token in concept for token in ("金额", "余额", "本金", "利息", "总额")):
        return base, "sum", None
    return base, None, None


def _explicit_outputs(query: str) -> list[tuple[str, list[int]]]:
    match = _OUTPUT_MARKER_RE.search(query)
    result: list[tuple[str, list[int]]] = []
    if match:
        for raw in _SPLIT_RE.split(match.group(1)):
            phrase = _clean_phrase(raw)
            if not phrase or len(phrase) > 20 or _is_query_control_output(phrase):
                continue
            start = query.find(phrase, match.start(1))
            result.append((phrase, [start, start + len(phrase)] if start >= 0 else []))

    # A grouped metric list is also an explicit result contract, even when the
    # natural-language possessive is omitted: “每个客户累计A、累计B和累计C”.
    # This is generic SQL grammar, not a dictionary of business metrics.
    known = {item[0] for item in result}
    for group in _group_concepts(query):
        marker = f"每个{group}"
        marker_start = query.find(marker)
        if marker_start < 0:
            continue
        tail_start = marker_start + len(marker)
        tail = query[tail_start:].lstrip("的")
        for raw in _SPLIT_RE.split(tail):
            phrase = _clean_phrase(raw)
            if not phrase or len(phrase) > 20 or _is_query_control_output(phrase):
                continue
            _, aggregation, _ = metric_semantics(phrase, grouped=True)
            if not aggregation or phrase in known:
                continue
            start = query.find(phrase, tail_start)
            result.append((phrase, [start, start + len(phrase)] if start >= 0 else []))
            known.add(phrase)
    return result


def _group_concepts(query: str) -> list[str]:
    concepts: list[str] = []
    for pattern in (_GROUP_RE, _GROUP_BY_RE):
        for match in pattern.finditer(query):
            concept = _clean_phrase(match.group(1)).removesuffix("维度")
            if concept and concept not in concepts:
                concepts.append(concept)
    return concepts


def enrich_semantic_graph(query: str, graph: SemanticGraph) -> SemanticGraph:
    """Merge deterministic query-contract facts into the model graph."""
    groups = _group_concepts(query)
    grouped = bool(groups)
    subjects = list(graph.subjects)
    subject_by_concept = {item.concept: item.id for item in subjects}

    def subject_id(concept: str) -> str:
        if concept not in subject_by_concept:
            identifier = f"subject_{len(subjects) + 1}"
            subject_by_concept[concept] = identifier
            subjects.append(SemanticSubject(id=identifier, kind="entity", concept=concept))
        return subject_by_concept[concept]

    outputs = [
        item for item in graph.outputs
        if not _is_query_control_output(item.source_text or item.concept)
    ]
    known_sources = {_clean_phrase(item.source_text or item.concept) for item in outputs}
    for phrase, span in _explicit_outputs(query):
        if phrase in known_sources:
            continue
        base, aggregation, grain = metric_semantics(phrase, grouped=grouped)
        outputs.append(SemanticOutput(
            id=f"output_{len(outputs) + 1}",
            subject_id=subjects[0].id if subjects else subject_id("查询对象"),
            concept=phrase,
            grounding_concept=base,
            source_text=phrase,
            source_span=span,
            required=True,
            confidence=0.98,
            aggregation=aggregation,
            distinct_grain=grain,
        ))
        known_sources.add(phrase)

    enriched_outputs: list[SemanticOutput] = []
    used_ids: set[str] = set()
    for output in outputs:
        semantic_label = output.source_text or output.concept
        if _clean_phrase(semantic_label).startswith("每个") and output.concept:
            semantic_label = output.concept
        base, aggregation, grain = metric_semantics(
            semantic_label,
            grouped=grouped,
        )
        output_id = output.id
        if output_id in used_ids:
            output_id = f"output_{len(used_ids) + 1}"
        used_ids.add(output_id)
        enriched_outputs.append(output.model_copy(update={
            "id": output_id,
            "grounding_concept": base or output.grounding_concept or output.concept,
            "aggregation": aggregation or output.aggregation,
            "distinct_grain": grain or output.distinct_grain,
        }))

    # Enrichment is intentionally called after both fallback and model merge.
    # Keep it idempotent and collapse repeated grouping outputs introduced by
    # those two sources.
    deduplicated: list[SemanticOutput] = []
    output_keys: set[tuple[str, str | None]] = set()
    for output in enriched_outputs:
        concept_key = _clean_phrase(output.concept).removeprefix("每个")
        key = (concept_key, output.aggregation)
        if key in output_keys:
            continue
        if concept_key in groups and output.aggregation is None:
            output = output.model_copy(update={
                "concept": concept_key,
                "grounding_concept": concept_key,
                "source_text": f"每个{concept_key}",
            })
        deduplicated.append(output)
        output_keys.add(key)
    enriched_outputs = deduplicated

    # A grouping dimension is necessarily part of the result contract.
    existing = {_clean_phrase(item.grounding_concept or item.concept) for item in enriched_outputs}
    for concept in groups:
        if concept in existing:
            continue
        enriched_outputs.insert(0, SemanticOutput(
            id=f"output_{len(used_ids) + 1}",
            subject_id=subject_id(concept),
            concept=concept,
            grounding_concept=concept,
            source_text=f"每个{concept}",
            required=True,
            confidence=0.98,
        ))
        used_ids.add(enriched_outputs[0].id)
        existing.add(concept)

    limit_match = _LIMIT_RE.search(query)
    limit_value = (
        limit_match.group("prefix_limit") or limit_match.group("return_limit")
        if limit_match else None
    )
    limit = int(limit_value) if limit_value else graph.limit
    orders = list(graph.order_by)
    if not orders:
        order_match = next(
            (match for pattern in _DIRECTIONAL_ORDER_RES if (match := pattern.search(query))),
            None,
        )
        if order_match:
            raw_concept = _clean_phrase(order_match.group(1))
            candidates = [
                item for item in enriched_outputs
                if _clean_phrase(item.concept) in raw_concept
                or raw_concept.endswith(_clean_phrase(item.concept))
            ]
            selected = max(candidates, key=lambda item: len(item.concept), default=None)
            concept = selected.concept if selected else raw_concept
            base = selected.grounding_concept if selected else metric_semantics(concept)[0]
            orders.append(SemanticOrder(
                concept=concept,
                grounding_concept=base,
                direction="desc" if order_match.group(2) in {"高到低", "降序"} else "asc",
                source_text=order_match.group(0),
            ))
        order_match = None if orders else _ORDER_RE.search(query)
        if order_match:
            raw_concept = _clean_phrase(order_match.group(1))
            # Keep the closest requested output concept, not the leading verb.
            candidates = [
                item for item in enriched_outputs
                if _clean_phrase(item.concept) in raw_concept or raw_concept.endswith(_clean_phrase(item.concept))
            ]
            selected = max(candidates, key=lambda item: len(item.concept), default=None)
            concept = selected.concept if selected else raw_concept
            base = selected.grounding_concept if selected else metric_semantics(concept)[0]
            orders.append(SemanticOrder(
                concept=concept,
                grounding_concept=base,
                direction="desc" if order_match.group(2) in {"最高", "最大", "最多"} else "asc",
                source_text=order_match.group(0),
            ))

    # Natural Top-N questions often omit “统计每个”, for example
    # “按贷款总金额从高到低返回前3个产品”. If the model retains only the
    # entity projection, preserve the explicitly named aggregate ordering
    # measure as an output contract and infer the single entity as group grain.
    # Restrict this repair to one non-aggregate output to avoid guessing the
    # grain for requests containing multiple descriptive attributes.
    effective_groups = groups or list(graph.group_by)
    non_aggregate_outputs = [item for item in enriched_outputs if not item.aggregation]
    entity_subjects = [item for item in subjects if item.kind == "entity"]
    if limit and orders and not non_aggregate_outputs and len(entity_subjects) == 1:
        entity = entity_subjects[0]
        output_id = f"output_{len(enriched_outputs) + 1}"
        while output_id in used_ids:
            output_id = f"output_{int(output_id.split('_')[-1]) + 1}"
        entity_output = SemanticOutput(
            id=output_id,
            subject_id=entity.id,
            concept=entity.concept,
            grounding_concept=entity.concept,
            source_text=entity.concept,
            required=True,
            confidence=0.95,
        )
        enriched_outputs.append(entity_output)
        used_ids.add(output_id)
        non_aggregate_outputs = [entity_output]
    if limit and orders and len(non_aggregate_outputs) == 1:
        for order in orders:
            order_keys = {
                _clean_phrase(order.concept),
                _clean_phrase(order.grounding_concept or ""),
            } - {""}
            already_bound = any(
                order_keys & ({
                    _clean_phrase(item.concept),
                    _clean_phrase(item.grounding_concept or ""),
                } - {""})
                for item in enriched_outputs
            )
            base, aggregation, grain = metric_semantics(order.concept, grouped=True)
            if already_bound or not aggregation:
                continue
            entity_output = non_aggregate_outputs[0]
            output_id = f"output_{len(enriched_outputs) + 1}"
            while output_id in used_ids:
                output_id = f"output_{int(output_id.split('_')[-1]) + 1}"
            enriched_outputs.append(SemanticOutput(
                id=output_id,
                subject_id=entity_output.subject_id,
                concept=order.concept,
                grounding_concept=base,
                source_text=order.source_text or order.concept,
                required=True,
                confidence=0.95,
                aggregation=aggregation,
                distinct_grain=grain,
            ))
            used_ids.add(output_id)
            if not effective_groups:
                effective_groups = [entity_output.grounding_concept or entity_output.concept]

    capabilities = list(graph.capabilities)
    if any(item.aggregation for item in enriched_outputs) and "aggregation" not in capabilities:
        capabilities.append("aggregation")
    if orders and "ordering" not in capabilities:
        capabilities.append("ordering")
    if limit and "top_n" not in capabilities:
        capabilities.append("top_n")
    return graph.model_copy(update={
        "subjects": subjects,
        "outputs": enriched_outputs,
        "group_by": effective_groups,
        "order_by": orders,
        "limit": limit,
        "capabilities": capabilities,
    })
