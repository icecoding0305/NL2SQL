"""模块 2:意图澄清——只做时间范围检查(精简版)。

原先"术语查不到唯一映射""指标聚合口径有歧义"两条判断已移除,
职责移交模块 3.5(检索后按置信度判定,依据不同)。本节点不再引用术语映射表。

只保留:缺时间范围的模式匹配检查(规则读 config/clarification_rules.yaml)。
"""

from __future__ import annotations

import re

from nl2sql_agent.state import NL2SQLState


def _history_text(state: NL2SQLState) -> str:
    parts = []
    for msg in state.conversation_history or []:
        parts.append(msg.get("content", "") if isinstance(msg, dict) else str(msg))
    return "\n".join(parts)


def _check_time_range(query: str, rule: dict) -> str | None:
    """问题表现出时间意图但缺少明确范围 → 返回提示,否则 None。"""
    intent = [k for k in rule.get("time_intent_keywords", []) if k in query]
    if not intent:
        return None
    range_present = any(re.search(p, query) for p in rule.get("range_present_patterns", []))
    if range_present:
        return None
    return rule.get("message", "请补充查询的时间范围(起止时间)")


def make_clarify_time_range_node(deps):
    def clarify_time_range_node(state: NL2SQLState) -> NL2SQLState | dict:
        rules = deps.config.clarification_rules
        if not rules.get("enabled", True):
            return {"need_clarification": False, "clarification_questions": [], "clarification_reason": None}

        query = state.clarified_query or state.user_query
        if state.conversation_history:
            query = query + "\n" + _history_text(state)  # 历史中已有范围时不再追问

        tr = rules.get("time_range_missing", {})
        msg = _check_time_range(query, tr) if tr.get("enabled", True) else None
        if msg is None:
            return {"need_clarification": False, "clarification_questions": [], "clarification_reason": None}

        # 敏感度阈值(与其它规则一致):置信 = 触发 × reliability,>= sensitivity 才触发
        sensitivity = float(rules.get("sensitivity", 0.5))
        if 1.0 * tr.get("reliability", 1.0) < sensitivity:
            return {"need_clarification": False, "clarification_questions": [], "clarification_reason": None}

        return {
            "need_clarification": True,
            "clarification_questions": [msg],
            "clarification_reason": "missing_time_range",
            "final_answer": "需要补充信息: " + msg,
        }

    return clarify_time_range_node
