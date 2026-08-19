"""Original-question coverage contract and conservative semantic repair.

The SemanticGraph is authoritative downstream, so losing a high-impact phrase
before schema retrieval is unrecoverable.  This module keeps schema-independent
query actions and broad requested topics visible without inventing physical
fields or metric definitions.
"""

from __future__ import annotations

import re

from nl2sql_agent.services.semantic_parser import iter_semantic_atoms
from nl2sql_agent.state import SemanticGraph, SemanticOutput, SemanticPredicate


_ACTION_RE = re.compile(r"(?:请|帮我)?(统计|计算|汇总|查询|查找|列出|展示|返回|排名)")
_BROAD_TOPIC_RE = re.compile(r"[\u4e00-\u9fffA-Za-z]{1,12}(?:情况|表现|信息|明细)")
_BROAD_SUFFIXES = ("情况", "表现", "信息", "明细")
_GENERIC_TOPICS = {"基本信息", "详细信息", "联系信息", "联系方式", "明细"}


def _clean(value: str | None) -> str:
    return re.sub(r"[\s，。；;、]", "", str(value or ""))


def _action(query: str) -> tuple[str, str, list[int]]:
    match = _ACTION_RE.search(query)
    if not match:
        return "unknown", "", []
    word = match.group(1)
    action = (
        "aggregate" if word in {"统计", "计算", "汇总"}
        else "rank" if word == "排名"
        else "detail" if word in {"列出", "展示", "返回"}
        else "lookup"
    )
    return action, word, list(match.span(1))


def _broad_topics(query: str) -> list[tuple[str, list[int]]]:
    topics: list[tuple[str, list[int]]] = []
    seen: set[str] = set()
    for match in _BROAD_TOPIC_RE.finditer(query):
        phrase = match.group(0)
        # The bounded regex may include context such as “上海的客户的逾期情况”.
        # Only the closest noun phrase after the final possessive marker is the
        # requested topic.
        topic = phrase.rsplit("的", 1)[-1]
        if not topic.endswith(_BROAD_SUFFIXES) or topic in seen:
            continue
        start = match.end() - len(topic)
        topics.append((topic, [start, match.end()]))
        seen.add(topic)
    return topics


def _topic_covered(topic: str, graph: SemanticGraph) -> list[str]:
    stem = topic
    for suffix in _BROAD_SUFFIXES:
        stem = stem.removesuffix(suffix)
    covered: list[str] = []
    for output in graph.outputs:
        text = _clean(f"{output.concept}{output.grounding_concept}{output.source_text}")
        if topic in text or (stem and stem in text):
            covered.append(output.id)
    for atom in iter_semantic_atoms(graph.predicate):
        text = _clean(f"{atom.concept}{atom.grounding_concept}{atom.source_text}")
        if topic in text or (stem and stem in text):
            covered.append(atom.atom_id)
    return list(dict.fromkeys(covered))


def _remove_spurious_topic_existence(
    predicate: SemanticPredicate | None,
    topics: set[str],
    query: str,
) -> SemanticPredicate | None:
    if predicate is None:
        return None
    if predicate.predicate_type in {"and", "or", "not"}:
        children = [
            child for item in predicate.children
            if (child := _remove_spurious_topic_existence(item, topics, query)) is not None
        ]
        if not children:
            return None
        if len(children) == 1 and predicate.predicate_type in {"and", "or"}:
            return children[0]
        return predicate.model_copy(update={"children": children})
    if (
        predicate.predicate_type in {"exists", "not_exists"}
        and not predicate.children
        and predicate.concept in topics
        and not any(marker in query for marker in (
            f"有{predicate.concept.removesuffix('情况')}",
            f"存在{predicate.concept.removesuffix('情况')}",
            f"没有{predicate.concept.removesuffix('情况')}",
        ))
    ):
        return None
    return predicate


def ensure_semantic_coverage(query: str, graph: SemanticGraph) -> tuple[SemanticGraph, dict]:
    """Repair uncovered broad result topics and return an auditable report."""
    action, action_text, action_span = _action(query)
    outputs = list(graph.outputs)
    groups = list(graph.group_by)
    repaired: list[str] = []
    mentions: list[dict] = []

    topic_matches = _broad_topics(query)
    topic_names = {topic for topic, _ in topic_matches}
    for index, (topic, span) in enumerate(topic_matches, 1):
        covered_by = _topic_covered(topic, graph.model_copy(update={"outputs": outputs}))
        mention_id = f"topic_{index}"
        if not covered_by:
            subject_id = graph.subjects[0].id if graph.subjects else "subject_1"
            output_id = f"coverage_output_{index}"
            used_ids = {item.id for item in outputs}
            while output_id in used_ids:
                output_id += "_1"
            outputs.append(SemanticOutput(
                id=output_id,
                subject_id=subject_id,
                concept=topic,
                grounding_concept=topic,
                source_text=topic,
                source_span=span,
                required=True,
                confidence=0.9,
                broad=True,
            ))
            covered_by = [output_id]
            repaired.append(topic)

        mentions.append({
            "id": mention_id,
            "text": topic,
            "role": "requested_topic",
            "source_span": span,
            "materiality": "high",
            "covered_by": covered_by,
        })

        # “统计……客户的逾期情况” describes a per-customer aggregate unless
        # the user explicitly supplied another grouping dimension.  This only
        # establishes business grain; aggregation fields are selected later
        # from the query-scoped schema candidates.
        if action == "aggregate" and not groups:
            prefix = query[: span[0]]
            entity = next(
                (item.concept for item in graph.subjects if f"{item.concept}的" in prefix),
                None,
            )
            if entity:
                groups.append(entity)
                if not any(
                    _clean(item.grounding_concept or item.concept) == _clean(entity)
                    and not item.aggregation
                    for item in outputs
                ):
                    used_ids = {item.id for item in outputs}
                    group_output_id = "coverage_group_1"
                    while group_output_id in used_ids:
                        group_output_id += "_1"
                    subject = next(
                        (item for item in graph.subjects if item.concept == entity),
                        graph.subjects[0] if graph.subjects else None,
                    )
                    outputs.insert(0, SemanticOutput(
                        id=group_output_id,
                        subject_id=subject.id if subject else "subject_1",
                        concept=entity,
                        grounding_concept=entity,
                        source_text=entity,
                        required=True,
                        confidence=0.9,
                    ))

    if action_text:
        has_aggregate = any(item.aggregation for item in outputs) or bool(groups)
        mentions.insert(0, {
            "id": "query_action",
            "text": action_text,
            "role": "query_action",
            "source_span": action_span,
            "materiality": "high",
            "covered_by": ["semantic_graph.query_action"] if action != "aggregate" or has_aggregate else [],
        })

    capabilities = list(graph.capabilities)
    if action == "aggregate" and "aggregation" not in capabilities:
        capabilities.append("aggregation")
    updated = graph.model_copy(update={
        "outputs": outputs,
        "group_by": groups,
        "query_action": action if action != "unknown" else graph.query_action,
        "predicate": _remove_spurious_topic_existence(
            graph.predicate, topic_names, query
        ),
        "capabilities": capabilities,
    })
    report = {
        "query_action": updated.query_action,
        "mentions": mentions,
        "repaired_mentions": repaired,
        "uncovered_mentions": [
            item["text"] for item in mentions
            if item["materiality"] == "high" and not item["covered_by"]
        ],
    }
    return updated, report


def refresh_semantic_coverage(
    query: str, graph: SemanticGraph | None, report: dict | None = None
) -> dict:
    """Re-evaluate coverage after schema-driven projection materialization."""
    refreshed = dict(report or {})
    if graph is None:
        return refreshed
    mentions = []
    for item in refreshed.get("mentions", []):
        current = dict(item)
        if current.get("role") == "requested_topic":
            current["covered_by"] = _topic_covered(str(current.get("text") or ""), graph)
        elif current.get("role") == "query_action" and graph.query_action == "aggregate":
            current["covered_by"] = (
                ["semantic_graph.aggregate_contract"]
                if any(output.aggregation for output in graph.outputs) and bool(graph.group_by)
                else []
            )
        mentions.append(current)
    refreshed["mentions"] = mentions
    refreshed["uncovered_mentions"] = [
        item.get("text") for item in mentions
        if item.get("materiality") == "high" and not item.get("covered_by")
    ]
    refreshed["query_action"] = graph.query_action
    return refreshed
