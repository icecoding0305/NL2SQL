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

from nl2sql_agent.state import NL2SQLState
from nl2sql_agent.services.prompt_context import compact_schema_facts, conversation_facts, effective_query, prompt_json, term_facts
from nl2sql_agent.services.sql_dialect import dialect_tips
from nl2sql_agent.services.sql_compiler import UnsupportedPlanError, compile_query_plan


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
    return deps.prompts.render("sql_from_plan",
        user_query=prompt_json(effective_query(state)),
        dialect=deps.config.dialect,
        dialect_tips=dialect_tips(deps.config.dialect),
        query_plan=prompt_json(plan.model_dump()),
        retry_feedback=_retry_feedback(state, deps),
        schema_view=prompt_json(compact_schema_facts(state, include_semantics=False)),
        terms_label=terms_label,
        terms=terms,
        conversation_block=prompt_json(conversation_facts(state)),
    )


def build_prompt_from_query(state: NL2SQLState, deps) -> str:
    few_shots = deps.few_shot.retrieve(state.user_query)
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
        if state.query_plan is not None:
            try:
                sql, used_tables = compile_query_plan(state.query_plan, deps.config.dialect)
                return {
                    "generated_sql": sql,
                    "used_tables": used_tables,
                    "validation_errors": [],
                    "execution_error": None,
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
        result = llm.complete_sql(prompt)  # 同时返回 sql 与 used_tables
        out: dict[str, Any] = {
            "generated_sql": result.sql,
            "used_tables": list(result.used_tables),
            # 重新生成后清空上一轮的校验错误
            "validation_errors": [],
            "execution_error": None,
        }
        return out

    return sql_generation_node
