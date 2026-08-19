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
_LIMIT_RE = re.compile(r"(?:前|top\s*)(\d+)", re.IGNORECASE)
_ORDER_RE = re.compile(r"([\u4e00-\u9fffA-Za-z_]{2,18}?)(最高|最大|最多|最低|最小|最少)")


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
    if not match:
        return []
    result: list[tuple[str, list[int]]] = []
    for raw in _SPLIT_RE.split(match.group(1)):
        phrase = _clean_phrase(raw)
        if not phrase or len(phrase) > 20:
            continue
        start = query.find(phrase, match.start(1))
        result.append((phrase, [start, start + len(phrase)] if start >= 0 else []))
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

    outputs = list(graph.outputs)
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
    limit = int(limit_match.group(1)) if limit_match else graph.limit
    orders = list(graph.order_by)
    if not orders:
        order_match = _ORDER_RE.search(query)
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
        "group_by": groups or graph.group_by,
        "order_by": orders,
        "limit": limit,
        "capabilities": capabilities,
    })
