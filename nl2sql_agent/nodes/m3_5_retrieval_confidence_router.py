"""模块 3.5:检索后置信度与自动改写路由。

把"是否需要澄清"的判断依据从"术语库有没有收录"改为"Schema 检索完成后的置信度分布":
- 多个物理表候选由系统按角色、粒度和关系自动规划，不要求业务用户选表
- 同一业务槽位存在不同字段口径 → 只展示业务语言选项
- 检索置信度低于阈值 → 自动业务语义改写并重新召回一次
- 第二次仍低置信 → 带风险标记进入严格计划校验，不询问物理表
- 高置信单一候选 → 直接放行到模块 4

阈值从 config/clarification_rules.yaml 的 retrieval_confidence 读取,不硬编码。
"""

from __future__ import annotations

from langgraph.types import interrupt

from nl2sql_agent.state import NL2SQLState


def _thresholds(deps) -> tuple[float, float]:
    rc = deps.config.clarification_rules.get("retrieval_confidence", {})
    return (
        float(rc.get("confidence_threshold", 0.7)),
        float(rc.get("candidate_gap_threshold", 0.1)),
    )


def make_route_after_retrieval(deps):
    """模块 3 之后的路由:多候选 / 低置信 / 放行。"""

    def route_after_retrieval(state: NL2SQLState) -> str:
        if state.unsupported_outputs:
            return "unsupported_output"
        if state.field_ambiguities:
            return "clarify_business"
        confidence_threshold, _ = _thresholds(deps)
        if state.retrieval_confidence < confidence_threshold:
            max_rewrites = int(
                deps.config.clarification_rules.get("retrieval_confidence", {})
                .get("max_automatic_rewrites", 1)
            )
            if state.retrieval_rewrite_count < max_rewrites:
                return "rewrite_retrieval"
            return "plan_generation"
        return "plan_generation"

    return route_after_retrieval


def _semantic_rewrite(state: NL2SQLState) -> str:
    """Build a business-language retrieval query without exposing Schema names."""
    graph = state.semantic_graph
    parts = [
        state.resolved_query.rewritten_query
        if state.resolved_query is not None else state.user_query
    ]
    if graph is not None:
        for output in graph.outputs:
            parts.extend([output.concept, output.grounding_concept or ""])
        parts.extend(graph.group_by)
        for order in graph.order_by:
            parts.extend([order.concept, order.grounding_concept or ""])
        if graph.group_by:
            parts.append("按维度分组统计")
        if graph.order_by:
            parts.append("排序指标")
        if graph.limit:
            parts.append(f"前{graph.limit}条")
    cleaned = [str(part).strip() for part in parts if str(part or "").strip()]
    return " ".join(dict.fromkeys(cleaned))


def make_rewrite_retrieval_node(deps):  # noqa: ARG001
    """Automatically rewrite a low-confidence query and run Schema retrieval again."""

    def rewrite_retrieval_node(state: NL2SQLState) -> dict:
        rewritten = _semantic_rewrite(state)
        return {
            "clarified_query": rewritten,
            "retrieval_rewrite_count": state.retrieval_rewrite_count + 1,
            "retrieval_rewrites": [*state.retrieval_rewrites, rewritten],
            "retrieval_resolved": False,
            "retrieved_schema": [],
            "retrieval_candidates": [],
            "field_candidates": [],
            "field_ambiguities": {},
            "schema_plan": None,
            "low_confidence_flag": True,
            "clarification_reason": None,
        }

    return rewrite_retrieval_node


def make_clarify_business_node(deps):  # noqa: ARG001 - 保持节点工厂签名一致
    """仅澄清业务含义，物理字段绑定始终留在服务端。"""

    def clarify_business_node(state: NL2SQLState) -> NL2SQLState | dict:
        clarification = state.business_clarification
        if clarification is None or not clarification.options:
            return {
                "need_clarification": True,
                "clarification_questions": ["请补充需要确认的业务口径"],
                "clarification_reason": "business_ambiguity",
                "final_answer": "当前业务口径不明确，请补充后重新提问。",
            }
        payload = {
            "type": "clarify_business",
            "question": clarification.question,
            "slot": clarification.slot,
            "options": [option.model_dump() for option in clarification.options],
            "query": state.user_query,
        }
        choice = interrupt(payload)
        option_id = choice.get("option_id") if isinstance(choice, dict) else choice
        selected_field = state.business_option_bindings.get(str(option_id))
        if not selected_field:
            return {
                "need_clarification": True,
                "clarification_questions": ["业务口径选择无效，请重新确认"],
                "clarification_reason": "business_ambiguity",
                "final_answer": "业务口径选择无效，请重新提问。",
            }
        return {
            "selected_field_overrides": {
                **state.selected_field_overrides,
                clarification.slot: selected_field,
            },
            "field_ambiguities": {},
            "business_clarification": None,
            "business_option_bindings": {},
            "retrieval_candidates": [],
            "retrieval_resolved": False,
            "clarification_reason": None,
        }

    return clarify_business_node
