"""模块 4:复杂度判断(纯路由节点,不调 LLM)。

用确定性规则(config/complexity_rules.yaml)决定走简单路径还是计划路径:
- 涉及表数量 >= 阈值
- 命中术语映射表里 composite_metric: true 的指标
- 问题包含多步聚合语义关键词(词表可配置)

规则偏保守:宁可让本可简单的查询多走一次计划,也不要漏判真正复杂的查询。
"""

from __future__ import annotations

from nl2sql_agent.state import NL2SQLState
from nl2sql_agent.services.term_mapping import TermResolutionStatus


def make_complexity_check_node(deps):
    def complexity_check_node(state: NL2SQLState) -> NL2SQLState | dict:
        # 低置信度查询:强制走计划路径,跳过规则判断(置信度低更需要人工审视理解过程)
        if state.low_confidence_flag:
            return {"is_complex": True, "complex_reasons": ["低置信度查询,强制走计划路径"]}

        rules = deps.config.complexity_rules
        query = state.clarified_query or state.user_query
        scope = state.data_scope
        reasons: list[str] = []

        # 1. 表数量:新链路使用锚点+实体表数，旧链路沿用术语主表数；都不包含桥接补表。
        threshold = int(rules.get("multi_table_threshold", 2))
        main_tables = state.main_table_count or len(state.retrieved_schema)
        if main_tables >= threshold:
            reasons.append(f"涉及表数量 {main_tables} >= {threshold}")

        # 2. 复合口径指标
        if rules.get("composite_metric_trigger", True):
            for term in deps.term_mapping.extract_terms(query, scope):
                res = deps.term_mapping.resolve(term, scope)
                if res.status == TermResolutionStatus.FOUND and res.entries[0].composite_metric:
                    reasons.append(f"命中复合口径指标: {term}")
                    break

        # 3. 多步聚合语义关键词
        if rules.get("keyword_trigger", True):
            for kw in rules.get("multi_step_keywords", []):
                if kw in query:
                    reasons.append(f"命中多步聚合关键词: {kw}")

        # conservative: 任一信号即判复杂
        is_complex = bool(reasons) if rules.get("conservative", True) else len(reasons) >= 2
        return {"is_complex": is_complex, "complex_reasons": reasons}

    return complexity_check_node
