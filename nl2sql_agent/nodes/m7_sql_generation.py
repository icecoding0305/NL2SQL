"""模块 7:SQL 生成(机械翻译节点)。

- 有计划:按 QueryPlan 构建 prompt;无计划:按自然语言 + few-shot 构建 prompt
- Schema 已在检索层按 data_scope 过滤；Prompt 不暴露命名空间取值，避免模型把它
  错当成 PLATFORM_CODE 等表字段值。模型同时输出 used_tables 供模块 8 交叉比对
- 每次重新生成时清空 validation_errors,避免把上一轮的失败带到本轮
- Prompt 模板来自 config/prompts/sql_from_plan.txt / sql_from_query.txt
"""

from __future__ import annotations

import json
from typing import Any

from nl2sql_agent.state import NL2SQLState, QueryCandidate
from nl2sql_agent.services.prompt_context import compact_schema_facts, conversation_facts, effective_query, prompt_json, term_facts
from nl2sql_agent.services.logical_planner import (
    build_query_mschema,
    query_mschema_runtime_kwargs,
)
from nl2sql_agent.services.sql_dialect import dialect_tips
from nl2sql_agent.services.sql_compiler import UnsupportedPlanError, compile_query_plan
from nl2sql_agent.services.sql_candidate_selector import rank_sql_candidates


def _retry_feedback(state: NL2SQLState, deps) -> str:
    """构建 SQL 重试反馈，模板来自 config/prompts/retry_feedback/sql_retry.txt。"""
    reasons = [*(state.validation_errors or [])]
    if state.execution_error:
        reasons.append(state.execution_error)
    if not reasons:
        return ""
    return deps.prompts.render("retry_feedback/sql_retry",
        previous_sql=state.generated_sql or "(空)",
        reasons=json.dumps(reasons[-5:], ensure_ascii=False),
        dialect=deps.config.dialect,
        dialect_tips=dialect_tips(deps.config.dialect),
    )


def _term_view(state: NL2SQLState, deps) -> tuple[str, str]:
    terms = term_facts(state, deps)
    label = "强约束：必须严格采用这些定义" if terms else "未命中已知术语，不得虚构业务口径"
    return label, prompt_json(terms)


def build_prompt_from_plan(state: NL2SQLState, deps) -> str:
    plan = state.query_plan
    terms_label, terms = _term_view(state, deps)
    execution_schema = build_query_mschema(
        state,
        "execution",
        **query_mschema_runtime_kwargs(state, deps),
    )
    return deps.prompts.render("sql_from_plan",
        user_query=prompt_json(effective_query(state)),
        dialect=deps.config.dialect,
        dialect_tips=dialect_tips(deps.config.dialect),
        query_plan=prompt_json(plan.model_dump()),
        retry_feedback=_retry_feedback(state, deps),
        schema_view=prompt_json(compact_schema_facts(
            state, execution_schema, include_semantics=False
        )),
        terms_label=terms_label,
        terms=terms,
        conversation_block=prompt_json(conversation_facts(state)),
    )


def build_prompt_from_query(state: NL2SQLState, deps) -> str:
    few_shots = deps.few_shot.retrieve(
        state.user_query,
        dialect=deps.config.dialect,
        available_tables={hit.table_name for hit in state.retrieved_schema},
    )
    terms_label, terms = _term_view(state, deps)
    few_shot_block = prompt_json([
        {"user_query": ex.get("user_query"), "sql": ex.get("sql")}
        for ex in few_shots
    ])
    return deps.prompts.render("sql_from_query",
        user_query=prompt_json(effective_query(state)),
        dialect=deps.config.dialect,
        dialect_tips=dialect_tips(deps.config.dialect),
        schema_view=prompt_json(compact_schema_facts(state, include_semantics=False)),
        retry_feedback=_retry_feedback(state, deps),
        few_shot_block=few_shot_block,
        terms_label=terms_label,
        terms=terms,
        conversation_block=prompt_json(conversation_facts(state)),
    )


def make_sql_generation_node(deps):
    def sql_generation_node(state: NL2SQLState) -> NL2SQLState | dict:
        execution_schema = (
            build_query_mschema(
                state,
                "execution",
                **query_mschema_runtime_kwargs(state, deps),
            )
            if state.query_plan is not None else None
        )
        alternatives = sorted(
            (
                item for item in state.query_candidates
                if item.stage == "sql"
                and item.source == "model_sql_candidate"
                and item.status == "generated"
                and not item.selected
                and item.sql
            ),
            key=lambda item: -(item.score or 0.0),
        )
        if state.retry_count > 0 and alternatives:
            selected_id = alternatives[0].candidate_id
            candidates = [
                item.model_copy(update={"selected": item.candidate_id == selected_id})
                for item in state.query_candidates
            ]
            selected = alternatives[0]
            return {
                "generated_sql": selected.sql,
                "used_tables": list(selected.metadata.get("used_tables") or []),
                "validation_errors": [],
                "execution_error": None,
                "sql_generation_source": "model",
                "query_candidates": candidates,
            }
        if state.query_plan is not None:
            try:
                sql, used_tables = compile_query_plan(state.query_plan, deps.config.dialect)
                candidates = [item.model_copy(update={"selected": False}) for item in state.query_candidates]
                candidates.append(QueryCandidate(
                    candidate_id=f"sql_{state.retry_count + 1}",
                    stage="sql",
                    source="deterministic_compiler",
                    schema_profile="execution",
                    status="compiled",
                    query_plan=state.query_plan,
                    logical_plan=state.logical_plan,
                    sql=sql,
                    score=state.query_plan.confidence,
                    selected=True,
                ))
                return {
                    "generated_sql": sql,
                    "used_tables": used_tables,
                    "validation_errors": [],
                    "execution_error": None,
                    "sql_generation_source": "deterministic",
                    "query_candidates": candidates,
                    "query_mschema_execution": execution_schema,
                }
            except (UnsupportedPlanError, ValueError):
                # Compatibility path for old/incomplete plans. It remains validated
                # by the same AST safety node after model translation.
                pass
        if state.query_plan is not None:
            prompt = build_prompt_from_plan(state, deps)
        else:
            prompt = build_prompt_from_query(state, deps)
        # SQL 专用模型(如有配置),否则回退主模型
        llm = deps.sql_llm or deps.llm
        candidate_policy = deps.config.clarification_rules.get(
            "sql_candidate_refinement", {}
        )
        multi_candidate = bool(candidate_policy.get("enabled", True)) and state.retry_count == 0
        candidate_count = (
            max(1, min(3, int(candidate_policy.get("candidate_count", 2))))
            if multi_candidate else 1
        )
        results = []
        for index in range(candidate_count):
            strategy = (
                "\n\n候选策略：采用最保守、最直接且与 QueryPlan 一致的 SQL 结构。"
                if index == 0 else
                "\n\n候选策略：独立重新推导等价 SQL，重点检查 JOIN、聚合粒度和过滤条件，"
                "不得改变 QueryPlan 业务语义。"
            )
            try:
                results.append(llm.complete_sql(prompt + strategy))
            except Exception:  # noqa: BLE001 - one failed candidate must not discard others
                if not results and index == candidate_count - 1:
                    raise
        ranked = rank_sql_candidates(results, state, deps.config.dialect)
        if not ranked:
            raise ValueError("SQL 候选生成未返回可解析结果")
        result = ranked[0][0]
        candidates = [item.model_copy(update={"selected": False}) for item in state.query_candidates]
        source = (
            "model_sql_refiner" if state.retry_count > 0
            else "model_sql_candidate" if len(ranked) > 1
            else "model_sql_fallback"
        )
        base_index = sum(1 for item in candidates if item.stage == "sql") + 1
        for index, (candidate_result, candidate_score, preliminary_errors) in enumerate(ranked):
            candidates.append(QueryCandidate(
                candidate_id=f"sql_{base_index + index}",
                stage="sql",
                source=source,
                schema_profile=(
                    "execution" if execution_schema is not None
                    else state.query_mschema.profile if state.query_mschema else "precision"
                ),
                status="generated",
                query_plan=state.query_plan,
                logical_plan=state.logical_plan,
                sql=candidate_result.sql,
                validation_errors=preliminary_errors,
                score=candidate_score,
                selected=index == 0,
                metadata={
                    "selection_method": "deterministic_ast_score",
                    "candidate_rank": index + 1,
                    "refinement_round": state.retry_count,
                    "used_tables": list(candidate_result.used_tables),
                },
            ))
        out: dict[str, Any] = {
            "generated_sql": result.sql,
            "used_tables": list(result.used_tables),
            # 重新生成后清空上一轮的校验错误
            "validation_errors": [],
            "execution_error": None,
            "sql_generation_source": "model",
            "query_candidates": candidates,
            "query_mschema_execution": execution_schema,
        }
        return out

    return sql_generation_node
