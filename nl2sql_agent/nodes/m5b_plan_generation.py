"""模块 5b:生成查询计划(把"业务理解"和"语法生成"拆开)。

调用 LLM,要求严格输出符合 QueryPlan schema 的结构化 JSON(structured output +
parse + 重试),不能是自由文本或自然语言描述——否则就等于没拆分。
QueryPlan 在类型层面不允许表达非 SELECT 操作,危险操作结构上不可表达。
Prompt 模板来自 config/prompts/plan_generation.txt。
"""

from __future__ import annotations

from typing import Any

from nl2sql_agent.services.deterministic_planner import build_deterministic_query_plan
from nl2sql_agent.services.prompt_context import compact_schema_facts, conversation_facts, effective_query, prompt_json, term_facts
from nl2sql_agent.services.logical_planner import (
    build_logical_plan,
    build_query_mschema_bundle,
    query_mschema_runtime_kwargs,
)
from nl2sql_agent.services.llm import (
    LLMOutputTruncatedError,
    LLMResponseError,
    classify_llm_error,
)
from nl2sql_agent.services.plan_normalizer import normalize_structural_coverage
from nl2sql_agent.state import NL2SQLState, QueryCandidate, QueryPlan


def _select_planning_schema(state: NL2SQLState, precision, recall):
    """Escalate only when retry evidence or contract coverage requires it."""
    return recall if state.plan_validation_errors or precision.warnings else precision


def build_plan_prompt(state: NL2SQLState, deps, query_mschema=None) -> str:
    terms = term_facts(state, deps)

    terms_label = (
        "必须严格照此定义，不得自创"
        if terms
        else "未命中已知术语；只能使用明确字段含义，无法确定时降低 confidence，不得自创口径"
    )
    terms_json = prompt_json(terms)

    retry_feedback = ""
    if state.plan_validation_errors:
        previous = state.query_plan.model_dump() if state.query_plan is not None else None
        retry_feedback = deps.prompts.render("retry_feedback/plan_retry",
            previous_plan=prompt_json(previous),
            errors=prompt_json(state.plan_validation_errors[-5:]),
        )

    if query_mschema is None:
        precision_schema, recall_schema = build_query_mschema_bundle(
            state, **query_mschema_runtime_kwargs(state, deps)
        )
        query_mschema = _select_planning_schema(state, precision_schema, recall_schema)
    schema_view = compact_schema_facts(state, query_mschema)
    few_shot_block = prompt_json(deps.few_shot.retrieve_patterns(effective_query(state)))
    return deps.prompts.render("plan_generation",
        user_query=prompt_json(effective_query(state)),
        schema_view=prompt_json(schema_view),
        terms_label=terms_label,
        terms=terms_json,
        retry_feedback=retry_feedback,
        few_shot_block=few_shot_block,
        conversation_block=prompt_json(conversation_facts(state)),
    )


def make_plan_generation_node(deps):
    def plan_generation_node(state: NL2SQLState) -> NL2SQLState | dict:
        precision_schema, recall_schema = build_query_mschema_bundle(
            state, **query_mschema_runtime_kwargs(state, deps)
        )
        query_mschema = _select_planning_schema(state, precision_schema, recall_schema)
        if query_mschema.warnings:
            return {
                "query_plan": None,
                "logical_plan": None,
                "query_mschema": query_mschema,
                "query_mschema_precision": precision_schema,
                "query_mschema_recall": recall_schema,
                "plan_normalizations": [],
                "plan_generation_error_kind": "schema_context_incomplete",
                "plan_validation_errors": [
                    f"Query M-Schema 不完整：{warning}"
                    for warning in query_mschema.warnings[:8]
                ],
            }
        prompt = build_plan_prompt(state, deps, query_mschema)
        try:
            deterministic_cfg = deps.config.clarification_rules.get("deterministic_plan", {})
            deterministic_plan = (
                build_deterministic_query_plan(
                    state,
                    min_confidence=float(deterministic_cfg.get("min_confidence", 0.78)),
                )
                if deterministic_cfg.get("enabled", True)
                else None
            )
            if deterministic_plan is not None:
                plan, normalizations = normalize_structural_coverage(
                    deterministic_plan,
                    state.semantic_graph,
                    state.output_bindings,
                    state.semantic_bindings,
                )
                logical_plan = build_logical_plan(plan, state)
                candidate = QueryCandidate(
                    candidate_id=f"plan_{state.plan_retry_count + 1}",
                    stage="plan",
                    source="deterministic_compiler",
                    schema_profile=precision_schema.profile,
                    status="normalized",
                    query_plan=plan,
                    logical_plan=logical_plan,
                    score=plan.confidence,
                    selected=True,
                    metadata={
                        "normalizations": normalizations,
                        "generation_mode": "deterministic_simple",
                    },
                )
                return {
                    "query_plan": plan,
                    "query_mschema": precision_schema,
                    "query_mschema_precision": precision_schema,
                    "query_mschema_recall": recall_schema,
                    "logical_plan": logical_plan,
                    "query_candidates": [*state.query_candidates, candidate],
                    "plan_normalizations": normalizations,
                    "plan_validation_errors": [],
                    "plan_generation_error_kind": None,
                }
            # plan_generation 走节点级模型(配置里保持 deepseek-v4-pro 保质量,flash
            # 对嵌套 QueryPlan JSON 不稳定);内部重试 2→1,校验失败由模块 6 图级兜底。
            raw_plan = deps.llm_for("plan_generation").complete_structured(prompt, QueryPlan, retries=0)
            plan, normalizations = normalize_structural_coverage(
                raw_plan,
                state.semantic_graph,
                state.output_bindings,
                state.semantic_bindings,
            )
            logical_plan = build_logical_plan(plan, state)
            candidate = QueryCandidate(
                candidate_id=f"plan_{state.plan_retry_count + 1}",
                stage="plan",
                source="plan_model",
                schema_profile=query_mschema.profile,
                status="normalized",
                query_plan=plan,
                logical_plan=logical_plan,
                score=plan.confidence,
                selected=True,
                metadata={"normalizations": normalizations},
            )
            out: dict[str, Any] = {
                "query_plan": plan,
                "query_mschema": query_mschema,
                "query_mschema_precision": precision_schema,
                "query_mschema_recall": recall_schema,
                "logical_plan": logical_plan,
                "query_candidates": [*state.query_candidates, candidate],
                "plan_normalizations": normalizations,
                # 成功生成即清空历史校验错误,避免把上一轮的失败带到下一轮
                "plan_validation_errors": [],
                "plan_generation_error_kind": None,
            }
            return out
        except LLMOutputTruncatedError as e:
            return {
                "query_plan": None,
                "logical_plan": None,
                "plan_normalizations": [],
                "plan_generation_error_kind": "output_truncated",
                "plan_validation_errors": [f"计划生成失败（模型输出被截断）：{e}"],
            }
        except LLMResponseError as e:
            return {
                "query_plan": None,
                "logical_plan": None,
                "plan_normalizations": [],
                "plan_generation_error_kind": "empty_response",
                "plan_validation_errors": [f"计划生成失败（模型未返回有效正文）：{e}"],
            }
        except Exception as e:  # noqa: BLE001
            # 结构化解析失败:记入 plan_validation_errors,由 plan_validation 判定重试
            error_kind = classify_llm_error(e)
            return {
                "query_plan": None,
                "logical_plan": None,
                "plan_normalizations": [],
                "plan_generation_error_kind": error_kind,
                "plan_validation_errors": [f"计划生成失败(结构化输出解析): {e}"],
            }

    return plan_generation_node
