"""模块 3.5:检索后置信度路由(插在模块 3 与模块 4 之间)。

把"是否需要澄清"的判断依据从"术语库有没有收录"改为"Schema 检索完成后的置信度分布":
- 多个物理表候选由系统按角色、粒度和关系自动规划，不要求业务用户选表
- 同一业务槽位存在不同字段口径 → 只展示业务语言选项
- 检索置信度低于阈值 → 低置信澄清(clarify_low_confidence),问用户是否继续
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
        if state.field_ambiguities:
            return "clarify_business"
        confidence_threshold, _ = _thresholds(deps)
        if state.retrieval_confidence < confidence_threshold:
            return "clarify_low_confidence"
        return "plan_generation"

    return route_after_retrieval


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


def make_clarify_low_confidence_node(deps):
    """低置信澄清:提示指标不在已知范围,问用户是否继续。"""

    def clarify_low_confidence_node(state: NL2SQLState) -> NL2SQLState | dict:
        unresolved = state.schema_plan.unresolved_slots if state.schema_plan else []
        detail = f" 未确定内容：{'、'.join(unresolved)}。" if unresolved else ""
        payload = {
            "type": "clarify_low_confidence",
            "question": f"Schema 规划证据不足。{detail}是否继续尝试?",
            "query": state.user_query,
        }
        decision = interrupt(payload)
        if isinstance(decision, dict):
            cont = bool(decision.get("continue", decision.get("approved", False)))
        else:
            cont = bool(decision)
        if cont:
            return {"low_confidence_flag": True, "clarification_reason": None}
        return {
            "need_clarification": True,
            "clarification_questions": ["Schema 规划证据不足，请换一种问法或补充字段口径"],
            "clarification_reason": "low_confidence",
            "final_answer": "Schema 规划证据不足，请换一种问法或补充字段口径。",
        }

    return clarify_low_confidence_node


def route_after_low_confidence(state: NL2SQLState) -> str:
    """用户选择不继续 → 结束;继续 → 带着 low_confidence_flag 进模块 4。"""
    return "need_info" if state.need_clarification else "proceed"
