"""模块 2：问题理解、改写与业务消歧。

优先把口语问题规范化为结构化 ResolvedQuery；只有会实质改变结果且无法从
会话/治理规则确定的业务信息才阻断。物理表和字段不属于用户澄清内容。
"""

from __future__ import annotations

import json
import re

from nl2sql_agent.nodes.m2_clarify_time_range import _check_time_range, _history_text
from nl2sql_agent.services.semantic_parser import (
    build_semantic_graph,
    iter_semantic_atoms,
    semantic_graph_to_query_intent,
)
from nl2sql_agent.state import DecisionSummary, NL2SQLState, ResolvedQuery


def _fallback_resolution(query: str, predicate_config: dict | None = None) -> ResolvedQuery:
    """LLM 不可用时保持确定性、非猜测的最小改写。"""
    normalized = re.sub(r"\s+", " ", query).strip()
    graph = build_semantic_graph(normalized, predicate_config)
    intent = semantic_graph_to_query_intent(graph, normalized, predicate_config)
    return ResolvedQuery(
        original_query=query,
        rewritten_query=normalized,
        query_type=intent.query_type,
        entities=[item.text for item in intent.entities],
        measures=[item.text for item in intent.measures],
        attributes=[item.text for item in intent.attributes],
        filters=[
            {"subject": item.text, "operator": item.operator, "value": item.value}
            for item in intent.filters
        ],
        dimensions=[item.text for item in intent.dimensions],
        assumptions=graph.assumptions,
        unresolved_business_slots=graph.unresolved_slots,
        confidence=graph.confidence if intent.query_type != "unknown" else 0.45,
        semantic_graph=graph,
    )


def _resolution_prompt(state: NL2SQLState, deps) -> str:
    history = [
        {"role": item.get("role"), "content": item.get("content")}
        for item in (state.conversation_history or [])[-6:]
        if isinstance(item, dict)
    ]
    return deps.prompts.render(
        "query_resolution",
        user_query=json.dumps(state.user_query, ensure_ascii=False),
        conversation_history=json.dumps(history, ensure_ascii=False),
    )


def _decision_summary(resolved: ResolvedQuery) -> DecisionSummary:
    steps: list[str] = []
    if resolved.filters:
        steps.append("根据用户给出的业务条件筛选数据")
    if resolved.measures:
        steps.append("计算或返回所需业务指标")
    if resolved.attributes:
        steps.append("返回目标对象的业务属性")
    if resolved.dimensions:
        steps.append("按照指定业务维度组织结果")
    if not steps:
        steps.append("定位与问题相关的数据并返回结果")
    return DecisionSummary(
        understood_query=resolved.rewritten_query,
        business_steps=steps,
        assumptions=[item.content for item in resolved.assumptions],
        confidence={"question_understanding": resolved.confidence},
        warnings=[f"待补充：{slot}" for slot in resolved.unresolved_business_slots],
    )


def _prefer_complete_graph(candidate, fallback):
    """模型主导理解，但确定性识别出的条件和显式输出都不允许静默遗漏。"""
    if candidate is None:
        return fallback
    fallback_sources = {
        atom.source_text
        for atom in iter_semantic_atoms(fallback.predicate if fallback else None)
        if atom.materiality == "high" and atom.source_text
    }
    candidate_text = " ".join(
        atom.source_text
        for atom in iter_semantic_atoms(candidate.predicate)
        if atom.materiality == "high"
    )
    base = candidate if all(source in candidate_text for source in fallback_sources) else fallback
    if base is None or fallback is None:
        return base

    outputs = list(base.outputs)
    known_concepts = {
        re.sub(r"\s+", "", output.grounding_concept or output.concept).lower()
        for output in outputs
    }
    used_ids = {output.id for output in outputs}
    for graph in (candidate, fallback):
        for output in graph.outputs:
            concept = re.sub(r"\s+", "", output.grounding_concept or output.concept).lower()
            if concept in known_concepts:
                continue
            output_id = output.id
            if output_id in used_ids:
                index = len(outputs) + 1
                while f"output_{index}" in used_ids:
                    index += 1
                output_id = f"output_{index}"
            outputs.append(output.model_copy(update={"id": output_id}))
            known_concepts.add(concept)
            used_ids.add(output_id)

    subjects = list(base.subjects)
    known_subjects = {(item.id, item.concept) for item in subjects}
    for graph in (candidate, fallback):
        for item in graph.subjects:
            key = (item.id, item.concept)
            if key not in known_subjects:
                subjects.append(item)
                known_subjects.add(key)
    capabilities = list(base.capabilities)
    if outputs and "entity_output" not in capabilities:
        capabilities.append("entity_output")
    return base.model_copy(update={
        "subjects": subjects,
        "outputs": outputs,
        "capabilities": capabilities,
    })


def _should_use_llm_resolution(state: NL2SQLState, resolved: ResolvedQuery, config: dict) -> bool:
    """Use the LLM only when deterministic understanding is materially uncertain."""
    mode = config.get("use_llm", "auto")
    if mode is True or str(mode).lower() in {"true", "always"}:
        return True
    if mode is False or str(mode).lower() in {"false", "never"}:
        return False
    if state.conversation_history or resolved.unresolved_business_slots:
        return True
    if resolved.query_type in {"unknown", "multi_fact", "composite_metric"}:
        return True
    if resolved.confidence < float(config.get("auto_min_confidence", 0.7)):
        return True
    markers = config.get("llm_required_markers", [
        "最新", "最早", "排名", "前", "top", "同比", "环比", "占比",
        "增长率", "分别", "每个", "或者", "任一", "除外", "连续",
    ])
    query = resolved.original_query.lower()
    return any(str(marker).lower() in query for marker in markers)


def make_query_resolution_node(deps):
    def query_resolution_node(state: NL2SQLState) -> NL2SQLState | dict:
        query = state.user_query.strip()
        predicate_config = deps.loader.load("business_predicates.yaml")
        resolved = _fallback_resolution(query, predicate_config)
        resolution_config = deps.config.clarification_rules.get("query_resolution", {})
        use_llm = _should_use_llm_resolution(state, resolved, resolution_config)
        if use_llm:
            try:
                candidate = deps.llm_for("query_resolution").complete_structured(
                    _resolution_prompt(state, deps), ResolvedQuery, retries=1
                )
                # 原始问题由服务端锁定，避免模型改写审计事实。
                fallback_graph = resolved.semantic_graph
                graph = _prefer_complete_graph(candidate.semantic_graph, fallback_graph)
                resolved = candidate.model_copy(update={
                    "original_query": query,
                    "semantic_graph": graph,
                    "attributes": list(dict.fromkeys([
                        *candidate.attributes,
                        *[
                            output.grounding_concept or output.concept
                            for output in (graph.outputs if graph else [])
                            if output.required
                        ],
                    ])),
                    "assumptions": [
                        *candidate.assumptions,
                        *[
                            item for item in (graph.assumptions if graph else [])
                            if item.content not in {known.content for known in candidate.assumptions}
                        ],
                    ],
                    "unresolved_business_slots": list(dict.fromkeys([
                        *candidate.unresolved_business_slots,
                        *(graph.unresolved_slots if graph else []),
                    ])),
                })
            except Exception:  # noqa: BLE001 - 降级到确定性解析，不中断查询服务
                pass

        rules = deps.config.clarification_rules
        time_rule = rules.get("time_range_missing", {})
        context_query = resolved.rewritten_query
        if state.conversation_history:
            context_query += "\n" + _history_text(state)
        time_question = (
            _check_time_range(context_query, time_rule)
            if rules.get("enabled", True) and time_rule.get("enabled", True)
            else None
        )

        unresolved = list(resolved.unresolved_business_slots)
        if time_question and "时间范围" not in unresolved:
            unresolved.append("时间范围")
            resolved = resolved.model_copy(update={"unresolved_business_slots": unresolved})

        questions = [time_question] if time_question else []
        questions.extend(f"请补充或明确：{slot}" for slot in unresolved if slot != "时间范围")
        semantic_graph = resolved.semantic_graph or build_semantic_graph(
            resolved.rewritten_query, predicate_config
        )
        out = {
            "clarified_query": resolved.rewritten_query,
            "resolved_query": resolved,
            "semantic_graph": semantic_graph,
            "query_intent": semantic_graph_to_query_intent(
                semantic_graph, resolved.rewritten_query, predicate_config
            ),
            "decision_summary": _decision_summary(resolved),
            "need_clarification": bool(questions),
            "clarification_questions": questions,
            "clarification_reason": (
                "missing_time_range" if time_question else "business_ambiguity" if questions else None
            ),
        }
        if questions:
            out["final_answer"] = "需要补充业务信息：" + "；".join(questions)
        return out

    return query_resolution_node
