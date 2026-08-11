"""模块 11:结果解释。

把结构化查询结果转成自然语言摘要,同时保留原始数据(execution_result)供用户核实,
不是把数字直接扔给用户。LLM 不可用时用确定性摘要兜底。
Prompt 模板来自 config/prompts/result_summary.txt。
"""

from __future__ import annotations

from nl2sql_agent.services.prompt_context import effective_query, prompt_json
from nl2sql_agent.services.schema_planner import output_binding_fields
from nl2sql_agent.state import NL2SQLState, ResultSummary


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


def render_result_summary(summary: ResultSummary) -> str:
    """Render a readable compatibility string for history and conversation context."""
    sections = [summary.headline, summary.overview]
    if summary.key_findings:
        sections.append("关键发现：\n" + "\n".join(f"- {item}" for item in summary.key_findings))
    if summary.caveats:
        sections.append("说明：\n" + "\n".join(f"- {item}" for item in summary.caveats))
    return "\n\n".join(section for section in sections if section)


def deterministic_result_summary(
    query: str,
    rows: list[dict],
    state: NL2SQLState,
    deps,
) -> ResultSummary:
    """Produce a factual business summary without exposing raw row JSON."""
    if not rows:
        return ResultSummary(
            status="empty",
            headline="未找到符合条件的数据",
            overview=f"按照“{query}”所描述的条件查询后，本次没有返回记录。",
            key_findings=["当前结果集为空，不代表相关业务数据一定从未存在。"],
            caveats=["可以检查筛选条件、时间范围和当前数据权限是否符合预期。"],
            row_count=0,
            summarized_row_count=0,
        )

    plan = state.query_plan
    grain = plan.output_grain if plan else None
    entity = grain.entity if grain and grain.entity else "目标对象"
    if grain and grain.level == "entity":
        overview = f"本次返回 {len(rows)} 行符合条件的{entity}结果，每行代表一个{entity}。"
    elif grain and grain.level in {"aggregate", "global"}:
        overview = f"已完成查询所要求的汇总计算，本次返回 {len(rows)} 行汇总结果。"
    elif grain and grain.level == "record":
        overview = f"本次返回 {len(rows)} 行符合条件的业务明细。"
    else:
        overview = f"本次查询共返回 {len(rows)} 行结果。"

    findings: list[str] = []
    output_labels = list(dict.fromkeys(
        field.concept or field.alias or field.column or "返回值"
        for field in (plan.output_fields if plan else [])
    ))
    if output_labels:
        findings.append(f"结果包含：{'、'.join(output_labels)}。")
    if plan and plan.group_by:
        findings.append(f"结果共形成 {len(rows)} 个分组。")

    # A single aggregate/global row is safe and useful to summarize. Values have
    # already passed execution; sensitive outputs are described but never copied.
    if len(rows) == 1 and plan and grain and grain.level in {"aggregate", "global"}:
        row = sanitize_rows_for_llm(rows, state, deps, max_rows=1)[0]
        values: list[str] = []
        for field in plan.output_fields:
            key = field.alias or field.column
            if not key or key not in row:
                continue
            value = row[key]
            label = field.concept or key
            values.append(f"{label}为 {value if value is not None else '空值'}")
        if values:
            findings.append("；".join(values[:5]) + "。")

    caveats: list[str] = ["详细记录可在下方结果数据中查看。"]
    for binding in state.output_bindings.values():
        if binding.get("binding_mode") != "expanded":
            continue
        labels = [
            str(item.get("label") or item.get("column_name"))
            for item in output_binding_fields(binding)
        ]
        caveats.append(
            f"“{binding.get('concept', '返回字段')}”存在多个同主体字段，"
            f"本次已同时返回：{'、'.join(labels)}。"
        )
    execution_limit = int(getattr(deps.config, "execution_limit", 0) or 0)
    truncated = bool(execution_limit and len(rows) >= execution_limit)
    if truncated:
        caveats.append(
            f"本次结果达到系统单次返回上限 {execution_limit} 行，可能仍有更多符合条件的数据。"
        )
    if state.low_confidence_flag:
        caveats.append("本次 Schema 匹配证据偏低，建议结合字段口径核对结果。")
    for assumption in (state.resolved_query.assumptions if state.resolved_query else []):
        if assumption.materiality in {"medium", "high"}:
            caveats.append(f"本次采用口径：{assumption.content}。")

    return ResultSummary(
        status="partial" if truncated else "success",
        headline=f"已完成查询，共返回 {len(rows)} 行结果",
        overview=overview,
        key_findings=findings[:5],
        caveats=list(dict.fromkeys(caveats))[:5],
        row_count=len(rows),
        summarized_row_count=len(rows),
        truncated=truncated,
    )


def deterministic_summary(query: str, rows: list[dict]) -> str:
    """Backward-compatible plain summary used by external callers."""
    if not rows:
        return f"按照“{query}”所描述的条件查询后，本次没有返回记录。"
    return f"已完成“{query}”查询，本次共返回 {len(rows)} 行结果；详细记录可在结果数据中查看。"


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
        query = effective_query(state)
        fallback = deterministic_result_summary(query, rows, state, deps)
        if not rows or not _should_use_llm_summary(state, deps):
            return {
                "result_summary": fallback,
                "final_answer": render_result_summary(fallback),
            }
        try:
            # 构建列含义映射，帮助 LLM 理解缩写列名（如 OVD_BAL → 逾期本金余额）
            safe_rows = sanitize_rows_for_llm(rows, state, deps)
            result_columns = {str(key) for row in safe_rows for key in row}
            query_tables = state.query_mschema.tables if state.query_mschema else []
            column_meanings = {
                column.name: column.comment
                for table in query_tables
                for column in table.columns
                if column.name in result_columns
            }
            plan_context = {
                "output_fields": [
                    {"concept": field.concept, "alias": field.alias, "column": field.column}
                    for field in (state.query_plan.output_fields if state.query_plan else [])
                ],
                "output_grain": (
                    state.query_plan.output_grain.model_dump() if state.query_plan else None
                ),
                "group_by": state.query_plan.group_by if state.query_plan else [],
            }
            prompt = deps.prompts.render("result_summary",
                user_query=prompt_json(query),
                column_meanings=prompt_json(column_meanings),
                plan_context=prompt_json(plan_context),
                row_count=len(safe_rows),
                total_row_count=len(rows),
                rows_truncated=str(len(rows) > len(safe_rows)).lower(),
                rows=prompt_json(safe_rows),
            )
            generated = deps.llm.complete_structured(prompt, ResultSummary, retries=1)
            summary = generated.model_copy(update={
                "status": fallback.status,
                "row_count": len(rows),
                "summarized_row_count": len(safe_rows),
                "truncated": len(rows) > len(safe_rows) or fallback.truncated,
                "caveats": list(dict.fromkeys([
                    *generated.caveats,
                    *fallback.caveats,
                ]))[:5],
            })
        except Exception:  # noqa: BLE001 - LLM 不可用时降级为确定性摘要
            summary = fallback
        return {
            "result_summary": summary,
            "final_answer": render_result_summary(summary),
        }

    return result_interpretation_node
