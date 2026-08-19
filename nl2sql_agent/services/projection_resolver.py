"""Model-led concretization for vague result projections after Schema retrieval."""

from __future__ import annotations

import json

from nl2sql_agent.state import (
    IntentSlot,
    ProjectionDecision,
    ProjectionFieldExclusion,
    ProjectionFieldSelection,
    SchemaPlan,
    SemanticOutput,
)
from nl2sql_agent.services.schema_planner import is_generic_projection


GENERIC_PROJECTIONS = {"基本信息", "详细信息", "联系方式", "明细"}
TOPIC_SUFFIXES = ("情况", "表现")
_DIRECT_IDENTIFIER_WORDS = ("身份证", "证件号码", "银行卡", "账号", "密码", "生物特征")


def vague_projection_request(query_intent, semantic_graph=None) -> str | None:
    # The semantic parser may propose a concrete metric for a broad phrase
    # (for example, turn "逾期情况" into "逾期笔数").  ``broad`` marks that
    # proposal as non-authoritative: the schema-aware resolver must still see
    # and concretize the user's original request after retrieval.
    if semantic_graph is not None:
        for output in semantic_graph.outputs:
            if not output.required or not output.broad:
                continue
            for value in (
                output.grounding_concept,
                output.source_text,
                output.concept,
            ):
                text = str(value or "").strip()
                if text.endswith(TOPIC_SUFFIXES):
                    return text
    for attribute in query_intent.attributes:
        if is_generic_projection(attribute.text) or attribute.text.endswith(TOPIC_SUFFIXES):
            return attribute.text
    return None


def _candidate_fields(
    schema_plan: SchemaPlan,
    tables: list,
    request: str = "基本信息",
    limit: int = 32,
    extra_table_names: set[str] | None = None,
) -> list[dict]:
    """Build a bounded projection-only candidate set, never a complete database Schema."""
    # 实体返回优先且尽量只使用实体/维度表。事实锚点可能也带姓名或客户号，
    # 但把它加入“客户基本信息”候选会诱导模型返回贷款状态、余额和日期。
    topic_projection = request.endswith(TOPIC_SUFFIXES)
    entity_names = {
        item.table_name for item in schema_plan.dimension_tables
        if item.role in {"entity", "dimension"}
    }
    if topic_projection:
        entity_names.update(item.table_name for item in schema_plan.anchor_tables)
        entity_names.update(extra_table_names or set())
    elif not entity_names:
        entity_names = {item.table_name for item in schema_plan.anchor_tables}
    stem = request
    for suffix in (*TOPIC_SUFFIXES, "信息", "明细"):
        stem = stem.removesuffix(suffix)
    candidates: list[dict] = []
    for table in tables:
        if table.name not in entity_names:
            continue
        for column in table.columns:
            comment = str(column.get("comment") or "").strip()
            if not comment:
                continue
            candidates.append({
                "table_name": table.name,
                "column_name": str(column.get("name") or ""),
                "business_label": comment,
                "type": str(column.get("type") or column.get("raw_type") or ""),
                "semantic_role": str(column.get("semantic_role") or ""),
                "primary_key": bool(column.get("primary_key")),
                "sensitive": bool(column.get("sensitive")),
                "topic_score": (
                    1.0 if stem and stem in comment
                    else 0.45 if stem and stem in str(getattr(table, "comment", "") or "")
                    else 0.0
                ),
            })
    if topic_projection:
        candidates.sort(key=lambda item: (
            -float(item.get("topic_score") or 0.0),
            not bool(item.get("primary_key")),
            item.get("table_name", ""),
        ))
    return candidates[:limit]


def _numeric_type(candidate: dict) -> bool:
    value = str(candidate.get("type") or "").lower()
    return value.startswith(("int", "decimal", "numeric", "float", "double", "real"))


def _topic_aggregation(label: str, candidate: dict, aggregate: bool) -> str | None:
    if not aggregate:
        return None
    if not _numeric_type(candidate):
        return None
    if any(word in label for word in ("金额", "余额", "本金", "利息", "总额", "数量", "次数")):
        return "sum"
    if any(word in label for word in ("比例", "比率", "率")):
        return "avg"
    if any(word in label for word in ("天数", "期限", "日期")):
        return "max"
    return None


def _fallback_topic_decision(
    *, request: str, target_entity: str, query: str, candidates: list[dict]
) -> ProjectionDecision:
    """Schema-driven fallback for broad topics when the model is unavailable."""
    stem = request
    for suffix in (*TOPIC_SUFFIXES, "信息", "明细"):
        stem = stem.removesuffix(suffix)
    aggregate = any(word in query for word in ("统计", "汇总", "计算"))
    relevant = [
        item for item in candidates
        if stem and stem in str(item.get("business_label") or "")
        and not item.get("sensitive")
    ]
    selected: list[ProjectionFieldSelection] = []
    for item in relevant:
        aggregation = _topic_aggregation(
            str(item.get("business_label") or ""), item, aggregate
        )
        if aggregate and aggregation is None:
            continue
        selected.append(ProjectionFieldSelection(
            table_name=str(item["table_name"]),
            column_name=str(item["column_name"]),
            business_label=str(item["business_label"]),
            reason=f"字段含义与“{request}”直接相关",
            aggregation=aggregation,
        ))
        if len(selected) >= 4:
            break
    return ProjectionDecision(
        request=request,
        target_entity=target_entity,
        understood_description=(
            f"按{target_entity}汇总{request}" if aggregate else f"返回{target_entity}的{request}"
        ),
        selected_fields=selected,
        missing_concepts=[] if selected else [request],
        confidence=0.78 if selected else 0.0,
    )


def _explicitly_requested(query: str, candidate: dict) -> bool:
    return any(
        value and str(value).lower() in query.lower()
        for value in (candidate.get("business_label"), candidate.get("column_name"))
    )


def _validated_decision(
    raw: ProjectionDecision,
    *,
    request: str,
    target_entity: str,
    query: str,
    candidates: list[dict],
    max_fields: int = 8,
) -> ProjectionDecision:
    available = {
        (item["table_name"], item["column_name"]): item for item in candidates
    }
    selected: list[ProjectionFieldSelection] = []
    excluded = list(raw.excluded_fields)
    seen: set[tuple[str, str]] = set()
    for choice in raw.selected_fields:
        key = (choice.table_name, choice.column_name)
        candidate = available.get(key)
        if candidate is None or key in seen:
            continue
        label = str(candidate["business_label"])
        if request.endswith(TOPIC_SUFFIXES):
            topic_stem = request
            for suffix in TOPIC_SUFFIXES:
                topic_stem = topic_stem.removesuffix(suffix)
            if topic_stem and topic_stem not in label:
                excluded.append(ProjectionFieldExclusion(
                    business_label=label,
                    reason=f"字段名称未直接体现“{topic_stem}”主题",
                ))
                continue
        if (
            any(word in label for word in _DIRECT_IDENTIFIER_WORDS)
            and not _explicitly_requested(query, candidate)
        ):
            excluded.append(ProjectionFieldExclusion(
                business_label=label,
                reason="属于直接身份或账户标识，用户未明确要求",
            ))
            continue
        aggregation = choice.aggregation
        if aggregation and not _numeric_type(candidate) and aggregation != "count_distinct":
            aggregation = None
        if request.endswith(TOPIC_SUFFIXES) and aggregation is None:
            aggregation = _topic_aggregation(
                label, candidate, any(word in query for word in ("统计", "汇总", "计算"))
            )
        if (
            request.endswith(TOPIC_SUFFIXES)
            and any(word in query for word in ("统计", "汇总", "计算"))
            and aggregation is None
        ):
            excluded.append(ProjectionFieldExclusion(
                business_label=label,
                reason="统计型主题不返回无法确定聚合方式的明细字段",
            ))
            continue
        selected.append(choice.model_copy(update={
            "business_label": label,
            "aggregation": aggregation,
        }))
        seen.add(key)
        if len(selected) >= max_fields:
            break

    # 主键可作为结果粒度键，但不能单独冒充“基本信息”。
    readable = [
        item for item in selected
        if not available[(item.table_name, item.column_name)].get("primary_key")
    ]
    if selected and not readable:
        excluded.extend(ProjectionFieldExclusion(
            business_label=item.business_label,
            reason="仅返回实体主键不能满足模糊信息要求",
        ) for item in selected)
        selected = []

    return raw.model_copy(update={
        "request": request,
        "target_entity": target_entity,
        "selected_fields": selected,
        "excluded_fields": list({
            (item.business_label, item.reason): item for item in excluded
        }.values()),
    })


def resolve_vague_projection(
    state, deps, query_intent, schema_plan, visible_tables, field_candidates=None
):
    """Ask the model to choose real fields from a bounded query-scoped candidate set."""
    request = vague_projection_request(query_intent, state.semantic_graph)
    if request is None or schema_plan is None:
        return None
    target_entity = next(
        (item.text for item in query_intent.entities), "查询对象"
    )
    topic_tables = {
        item.table_name for item in (field_candidates or [])
        if item.query_slot == request or request.removesuffix("情况") in item.query_slot
    }
    candidates = _candidate_fields(
        schema_plan, visible_tables, request, extra_table_names=topic_tables
    )
    if not candidates:
        return ProjectionDecision(
            request=request,
            target_entity=target_entity,
            understood_description=f"返回{target_entity}的{request}",
            missing_concepts=[request],
            confidence=0.0,
        )
    prompt = deps.prompts.render(
        "projection_resolution",
        user_query=json.dumps(state.clarified_query or state.user_query, ensure_ascii=False),
        target_entity=json.dumps(target_entity, ensure_ascii=False),
        projection_request=json.dumps(request, ensure_ascii=False),
        candidate_fields=json.dumps(candidates, ensure_ascii=False, indent=2),
    )
    try:
        raw = deps.llm_for("projection_resolution").complete_structured(
            prompt, ProjectionDecision, retries=0
        )
    except Exception:  # noqa: BLE001 - leave an auditable partial decision
        if request.endswith(TOPIC_SUFFIXES):
            return _fallback_topic_decision(
                request=request,
                target_entity=target_entity,
                query=state.clarified_query or state.user_query,
                candidates=candidates,
            )
        return ProjectionDecision(
            request=request,
            target_entity=target_entity,
            understood_description=f"返回{target_entity}的{request}",
            missing_concepts=[f"{request}字段选择失败"],
            confidence=0.0,
        )
    validated = _validated_decision(
        raw,
        request=request,
        target_entity=target_entity,
        query=state.clarified_query or state.user_query,
        candidates=candidates,
    )
    if request.endswith(TOPIC_SUFFIXES) and not validated.selected_fields:
        return _fallback_topic_decision(
            request=request,
            target_entity=target_entity,
            query=state.clarified_query or state.user_query,
            candidates=candidates,
        )
    return validated


def materialize_projection_decision(
    decision: ProjectionDecision | None,
    semantic_graph,
    query_intent,
    schema_plan: SchemaPlan,
):
    """Convert a validated user-facing decision into enforceable semantic bindings."""
    if decision is None or not decision.selected_fields or semantic_graph is None:
        return semantic_graph, query_intent, schema_plan, {}
    subject_id = next(
        (
            subject.id for subject in semantic_graph.subjects
            if subject.concept == decision.target_entity
        ),
        semantic_graph.subjects[0].id if semantic_graph.subjects else "subject_1",
    )
    used_ids = {output.id for output in semantic_graph.outputs}
    outputs = [
        output for output in semantic_graph.outputs
        if decision.request not in output.concept
        and decision.request not in output.source_text
    ]
    bindings: dict[str, dict] = {}
    for index, field in enumerate(decision.selected_fields, start=1):
        output_id = f"profile_output_{index}"
        while output_id in used_ids:
            index += 1
            output_id = f"profile_output_{index}"
        used_ids.add(output_id)
        outputs.append(SemanticOutput(
            id=output_id,
            subject_id=subject_id,
            concept=field.business_label,
            grounding_concept=field.business_label,
            source_text=decision.request,
            required=True,
            confidence=decision.confidence,
            aggregation=field.aggregation,
            distinct_grain=field.distinct_grain,
        ))
        bindings[output_id] = {
            "concept": field.business_label,
            "grounding_concept": field.business_label,
            "table_name": field.table_name,
            "column_name": field.column_name,
            "confidence": decision.confidence,
            "required": True,
            "aggregation": field.aggregation,
            "distinct_grain": field.distinct_grain,
            "binding_mode": "exact",
            "bindings": [{
                "table_name": field.table_name,
                "column_name": field.column_name,
                "label": field.business_label,
                "confidence": decision.confidence,
            }],
        }
    graph = semantic_graph.model_copy(update={
        "outputs": outputs,
        "capabilities": list(dict.fromkeys([
            *semantic_graph.capabilities, "entity_output",
        ])),
    })
    materialized_attributes = [
        IntentSlot(text=item.business_label, role="attribute")
        for item in decision.selected_fields if not item.aggregation
    ]
    materialized_measures = [
        IntentSlot(text=item.business_label, role="measure")
        for item in decision.selected_fields if item.aggregation
    ]
    intent = query_intent.model_copy(update={
        "attributes": [
            *[
                item for item in query_intent.attributes
                if not is_generic_projection(item.text)
                and not item.text.endswith(TOPIC_SUFFIXES)
            ],
            *materialized_attributes,
        ],
        "measures": list({
            item.text: item for item in [*query_intent.measures, *materialized_measures]
        }.values()),
    })

    selected_by_table: dict[str, list[str]] = {}
    for field in decision.selected_fields:
        selected_by_table.setdefault(field.table_name, []).append(field.column_name)

    def expand(planned):
        return planned.model_copy(update={
            "selected_columns": list(dict.fromkeys([
                *planned.selected_columns,
                *selected_by_table.get(planned.table_name, []),
            ])),
        })

    plan = schema_plan.model_copy(update={
        "anchor_tables": [expand(item) for item in schema_plan.anchor_tables],
        "dimension_tables": [expand(item) for item in schema_plan.dimension_tables],
        "bridge_tables": [expand(item) for item in schema_plan.bridge_tables],
        "unresolved_slots": [
            slot for slot in schema_plan.unresolved_slots
            if decision.request not in slot
        ],
        "confidence": max(schema_plan.confidence, decision.confidence),
    })
    return graph, intent, plan, bindings
