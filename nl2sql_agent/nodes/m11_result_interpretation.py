"""模块 11:结果解释。

把结构化查询结果转成自然语言摘要,同时保留原始数据(execution_result)供用户核实,
不是把数字直接扔给用户。LLM 不可用时用确定性摘要兜底。
Prompt 模板来自 config/prompts/result_summary.txt。
"""

from __future__ import annotations

import json

from nl2sql_agent.services.prompt_context import effective_query, prompt_json
from nl2sql_agent.state import NL2SQLState


def sanitize_rows_for_llm(
    rows: list[dict], state: NL2SQLState, deps, *, max_rows: int = 50, max_text_length: int = 200
) -> list[dict]:
    """外发给 LLM 前按 Schema 与安全规则脱敏，并限制文本单元格长度。"""
    sensitive = {
        str(rule.get("name", "")).lower()
        for rule in deps.config.sensitive_rules.get("sensitive_fields", [])
    }
    sensitive.update(
        str(column.get("name", "")).lower()
        for hit in state.retrieved_schema
        for column in hit.columns
        if column.get("sensitive")
    )
    # 追踪敏感字段的 SELECT 别名，例如 IDNUM AS cert，防止别名绕过脱敏。
    if state.generated_sql:
        try:
            from sqlglot import exp

            expression = deps.sql.parse(state.generated_sql, deps.config.dialect)
            for projection in getattr(expression, "expressions", []):
                source_columns = {
                    str(column.name).lower()
                    for column in projection.find_all(exp.Column)
                }
                if source_columns & sensitive and projection.alias_or_name:
                    sensitive.add(str(projection.alias_or_name).lower())
        except Exception:  # SQL 已在上游校验；别名分析失败时仍保留字段名脱敏
            pass
    sanitized: list[dict] = []
    for row in rows[:max_rows]:
        clean: dict = {}
        for key, value in row.items():
            if str(key).lower() in sensitive:
                clean[key] = "[已脱敏]"
            elif isinstance(value, str) and len(value) > max_text_length:
                clean[key] = value[:max_text_length] + "…[已截断]"
            else:
                clean[key] = value
        sanitized.append(clean)
    return sanitized


def deterministic_summary(query: str, rows: list[dict]) -> str:
    if not rows:
        return f"针对「{query}」未返回结果。"
    head = rows[:3]
    return (
        f"查询「{query}」共返回 {len(rows)} 行结果。"
        f"示例:{json.dumps(head, ensure_ascii=False, default=str)}"
        "(完整结果见数据表格,请核对。)"
    )


def _should_use_llm_summary(state: NL2SQLState, deps) -> bool:
    mode = str(deps.config.performance.get("result_summary_mode", "auto")).lower()
    if mode in {"always", "true"}:
        return True
    if mode in {"never", "false"}:
        return False
    plan = state.query_plan
    if plan is None:
        return True
    # Detail/entity result sets are already displayed as a table. An extra remote
    # model call adds latency without adding factual information.
    return bool(plan and (plan.metric_logic or plan.group_by))


def make_result_interpretation_node(deps):
    def result_interpretation_node(state: NL2SQLState) -> NL2SQLState | dict:
        rows = state.execution_result
        if rows is None:
            return {"final_answer": "未获得查询结果。"}
        if not _should_use_llm_summary(state, deps):
            return {"final_answer": deterministic_summary(effective_query(state), rows)}
        try:
            # 构建列含义映射，帮助 LLM 理解缩写列名（如 OVD_BAL → 逾期本金余额）
            safe_rows = sanitize_rows_for_llm(rows, state, deps)
            result_columns = {str(key) for row in safe_rows for key in row}
            query_tables = state.query_mschema.tables if state.query_mschema else []
            column_meanings = json.dumps({
                column.name: column.comment
                for table in query_tables
                for column in table.columns
                if column.name in result_columns
            }, ensure_ascii=False)
            prompt = deps.prompts.render("result_summary",
                user_query=prompt_json(effective_query(state)),
                column_meanings=column_meanings,
                row_count=len(safe_rows),
                total_row_count=len(rows),
                rows_truncated=str(len(rows) > len(safe_rows)).lower(),
                rows=prompt_json(safe_rows),
            )
            answer = deps.llm.complete(prompt, max_tokens=500)
            if not answer or not answer.strip():
                answer = deterministic_summary(effective_query(state), rows)
        except Exception:  # noqa: BLE001 - LLM 不可用时降级为确定性摘要
            answer = deterministic_summary(effective_query(state), rows)
        return {"final_answer": answer}

    return result_interpretation_node
