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
from nl2sql_agent.services.semantic_query import enrich_semantic_graph
from nl2sql_agent.services.semantic_coverage import ensure_semantic_coverage
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
    fallback_atoms = [
        atom for atom in iter_semantic_atoms(fallback.predicate if fallback else None)
        if atom.materiality == "high"
    ]
    candidate_atoms = [
        atom for atom in iter_semantic_atoms(candidate.predicate)
        if atom.materiality == "high"
    ]

    # “来源文字出现过”不足以证明语义完整。例如模型只给出 exists(有逾期)，
    # 但规则已经解析为 exists + status(OVD_BAL > 0)，此时必须保留更精确的后者。
    def _concept(value: str | None) -> str:
        return re.sub(r"[\s._-]+", "", str(value or "")).lower()

    generic_concepts = {"比较", "条件", "筛选", "过滤", "comparison", "condition", "filter"}
    generic_projection_sources = {"基本信息", "详细信息", "联系方式", "明细"}

    def _covers(required) -> bool:
        required_concept = _concept(required.grounding_concept or required.concept)
        return any(
            actual.predicate_type == required.predicate_type
            and _concept(actual.grounding_concept or actual.concept) not in generic_concepts
            and (
                not required_concept
                or required_concept in _concept(actual.grounding_concept or actual.concept)
                or _concept(actual.grounding_concept or actual.concept) in required_concept
            )
            for actual in candidate_atoms
        )

    base = candidate if all(_covers(atom) for atom in fallback_atoms) else fallback
    if base is None or fallback is None:
        return base

    def _business_output(output):
        grounding = str(output.grounding_concept or "")
        # query-resolution 阶段只允许业务概念，不接受 customer.name 之类
        # 尚未落到 Schema 的逻辑路径，否则会被字段检索当成真实字段短语。
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", grounding):
            replacement = output.concept or output.source_text
            return output.model_copy(update={"grounding_concept": replacement})
        return output

    def _premature_projection(output) -> bool:
        source = re.sub(r"\s+", "", str(output.source_text or ""))
        concept = re.sub(r"\s+", "", str(output.concept or ""))
        return (
            source in generic_projection_sources
            and concept != source
        )

    outputs = [
        _business_output(output)
        for output in base.outputs
        if not _premature_projection(output)
    ]
    known_concepts = {
        re.sub(r"\s+", "", output.grounding_concept or output.concept).lower()
        for output in outputs
    }
    known_sources = {
        re.sub(r"\s+", "", output.source_text).lower()
        for output in outputs if output.source_text
    }
    used_ids = {output.id for output in outputs}
    for graph in (candidate, fallback):
        for output in graph.outputs:
            if _premature_projection(output):
                continue
            output = _business_output(output)
            concept = re.sub(r"\s+", "", output.grounding_concept or output.concept).lower()
            source = re.sub(r"\s+", "", output.source_text).lower()
            if concept in known_concepts or (source and source in known_sources):
                continue
            output_id = output.id
            if output_id in used_ids:
                index = len(outputs) + 1
                while f"output_{index}" in used_ids:
                    index += 1
                output_id = f"output_{index}"
            outputs.append(output.model_copy(update={"id": output_id}))
            known_concepts.add(concept)
            if source:
                known_sources.add(source)
            used_ids.add(output_id)

    subjects = list(base.subjects)
    known_subjects = {re.sub(r"\s+", "", item.concept).lower() for item in subjects}
    for graph in (candidate, fallback):
        for item in graph.subjects:
            key = re.sub(r"\s+", "", item.concept).lower()
            if key not in known_subjects:
                subjects.append(item)
                known_subjects.add(key)
    capabilities = list(dict.fromkeys([
        *base.capabilities, *candidate.capabilities, *fallback.capabilities,
    ]))
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
        predicate_config = deps.config.business_predicates
        resolved = _fallback_resolution(query, predicate_config)
        fallback_resolved = resolved
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
                if graph is not None:
                    graph = enrich_semantic_graph(query, graph)
                merged_intent = semantic_graph_to_query_intent(
                    graph, candidate.rewritten_query or query, predicate_config
                )
                graph_is_authoritative = bool(graph and graph.confidence >= 0.8)

                # LLM 负责语义理解，确定性解析负责守住用户显式表达的最低契约。
                # 某些推理模型可能耗尽预算后仍返回一个“合法但空”的对象；不能因此
                # 丢掉姓名、地址、逾期等直接写在问题中的返回项和筛选条件。
                # SemanticGraph 是唯一语义事实，ResolvedQuery 的兼容字段只从
                # 合并后的图派生，不能再次混入模型的“比较/customer.name”等占位词。
                merged_filters = [
                    {"subject": item.text, "operator": item.operator, "value": item.value}
                    for item in merged_intent.filters
                ]
                resolved = candidate.model_copy(update={
                    "original_query": query,
                    "rewritten_query": candidate.rewritten_query or fallback_resolved.rewritten_query,
                    "query_type": (
                        candidate.query_type
                        if candidate.query_type != "unknown"
                        else merged_intent.query_type
                    ),
                    "entities": list(dict.fromkeys([
                        *candidate.entities,
                        *fallback_resolved.entities,
                        *[item.text for item in merged_intent.entities],
                    ])),
                    "measures": list(dict.fromkeys([
                        *candidate.measures,
                        *fallback_resolved.measures,
                        *[item.text for item in merged_intent.measures],
                    ])),
                    "filters": merged_filters,
                    "dimensions": list(dict.fromkeys([
                        *candidate.dimensions,
                        *fallback_resolved.dimensions,
                        *[item.text for item in merged_intent.dimensions],
                    ])),
                    "semantic_graph": graph,
                    "attributes": list(dict.fromkeys([
                        *fallback_resolved.attributes,
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
                        *([] if graph_is_authoritative else candidate.unresolved_business_slots),
                        *(graph.unresolved_slots if graph else []),
                    ])),
                    "confidence": max(candidate.confidence, fallback_resolved.confidence),
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
        # Establish original-query coverage before deciding whether an LLM
        # unresolved slot needs user clarification.  Otherwise wording such as
        # "统计指标口径" can block a broad topic before schema-aware projection
        # has a chance to concretize it.
        preview_graph = resolved.semantic_graph or build_semantic_graph(
            resolved.rewritten_query, predicate_config
        )
        preview_graph = enrich_semantic_graph(query, preview_graph)
        preview_graph, _ = ensure_semantic_coverage(query, preview_graph)
        resolved = resolved.model_copy(update={"semantic_graph": preview_graph})
        broad_topics = [
            output.grounding_concept or output.source_text or output.concept
            for output in (preview_graph.outputs if preview_graph else [])
            if output.broad or any(
                str(value or "").endswith(("情况", "表现", "信息", "明细"))
                for value in (output.concept, output.grounding_concept, output.source_text)
            )
        ]
        if broad_topics:
            # Broad result topics are intentionally resolved after bounded Schema
            # retrieval.  Asking users to choose “逾期笔数/金额/客户数” here would
            # reintroduce the old metric-mapping and clarification bottleneck.
            def schema_resolvable(slot: str) -> bool:
                return (
                    any(marker in slot for marker in (
                        "统计粒度", "统计指标", "指标口径", "具体指标",
                        "具体内容", "返回内容", "返回字段",
                    ))
                    or any(topic.removesuffix("情况").removesuffix("表现") in slot for topic in broad_topics)
                )

            unresolved = [slot for slot in unresolved if not schema_resolvable(slot)]
            if preview_graph is not None:
                preview_graph = preview_graph.model_copy(update={
                    "unresolved_slots": [
                        slot for slot in preview_graph.unresolved_slots
                        if not schema_resolvable(slot)
                    ],
                })
            resolved = resolved.model_copy(update={
                "semantic_graph": preview_graph,
                "unresolved_business_slots": unresolved,
            })
        if time_question and "时间范围" not in unresolved:
            unresolved.append("时间范围")
            resolved = resolved.model_copy(update={"unresolved_business_slots": unresolved})

        questions = [time_question] if time_question else []
        questions.extend(f"请补充或明确：{slot}" for slot in unresolved if slot != "时间范围")
        semantic_graph = resolved.semantic_graph or build_semantic_graph(
            resolved.rewritten_query, predicate_config
        )
        semantic_graph = enrich_semantic_graph(query, semantic_graph)
        semantic_graph, semantic_coverage = ensure_semantic_coverage(query, semantic_graph)
        resolved = resolved.model_copy(update={"semantic_graph": semantic_graph})
        out = {
            "clarified_query": resolved.rewritten_query,
            "resolved_query": resolved,
            "semantic_graph": semantic_graph,
            "semantic_coverage": semantic_coverage,
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
