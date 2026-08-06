"""模块 9:敏感判定(规则来自 config/sensitive_rules.yaml)。

风险统一输出三态决策:pass / approval_required / hard_block。
可审批风险进入 human_review；硬阻断风险直接结束，不制造“批准后仍被拒绝”的假审批:
- 命中敏感字段列表(身份证、手机号等)
- EXPLAIN 预估扫描/返回行数超过阈值
- 涉及金额类字段的汇总或导出

判定标准全部是配置项,风控同事可自行调整,不写死在 prompt 或代码里。
"""

from __future__ import annotations

from typing import Any

from nl2sql_agent.state import NL2SQLState


def make_sensitive_check_node(deps):
    def sensitive_check_node(state: NL2SQLState) -> NL2SQLState | dict:
        rules = deps.config.sensitive_rules
        reasons: list[str] = []
        hard_block_reasons: list[str] = []
        sql = state.generated_sql or ""
        scope = state.data_scope

        try:
            expr = deps.sql.parse(sql, deps.config.dialect)
        except Exception:  # noqa: BLE001 - 语法问题已由模块 8 拦截,这里兜底
            expr = None

        # 规则 1:命中敏感字段列表(身份证/手机号等)
        sensitive_fields = {
            f["name"]
            for f in rules.get("sensitive_fields", [])
        }
        if expr is not None:
            for tbl, col in deps.sql.extract_columns(expr):
                if col in sensitive_fields:
                    reasons.append(f"引用敏感字段 {col}")
        # 查询文本提到敏感字段关键词也触发(未检出列引用时的兜底)
        if not reasons:
            kw_hits = [
                kw
                for f in rules.get("sensitive_fields", [])
                for kw in f.get("keywords", [])
                if kw in (state.clarified_query or state.user_query)
            ]
            if kw_hits:
                reasons.append(f"查询涉及敏感信息: {', '.join(sorted(set(kw_hits)))}")

        # 规则 2:EXPLAIN 预估行数超过阈值
        # EXPLAIN 失败属基础设施问题(如无执行环境),不是查询风险,静默跳过,由模块 10 守门
        scan_rule = rules.get("explain_scan", {})
        threshold = int(scan_rule.get("threshold", rules.get("explain_row_threshold", 1_000_000)))
        scan_action = scan_rule.get("action", "hard_block")
        if expr is not None:
            try:
                est = deps.executor.explain(sql)
                if est.estimated_rows > threshold:
                    reason = f"EXPLAIN 预估行数 {est.estimated_rows} 超过阈值 {threshold}"
                    if scan_action == "approval_required":
                        reasons.append(reason)
                    else:
                        hard_block_reasons.append(reason)
            except Exception:  # noqa: BLE001
                pass

        # 规则 3:涉及金额类字段的汇总或导出
        amount_keywords = rules.get("amount_field_keywords", [])
        amount_cols = {
            c["name"]
            for h in state.retrieved_schema
            for c in h.columns
            if any(k in str(c.get("comment", "")) or k in c["name"] for k in amount_keywords)
        }
        if expr is not None and amount_cols:
            for col in amount_cols:
                if rules.get("aggregation_trigger", {}).get("enabled", True) and deps.sql.is_column_in_aggregate(expr, col):
                    reasons.append(f"金额字段 {col} 参与聚合")
                if rules.get("export_trigger", {}).get("enabled", True) and deps.sql.is_select_column(expr, None, col):
                    reasons.append(f"导出金额字段 {col}")

        # 低置信度查询:即便其它规则都没命中,也强制进人工确认
        if state.low_confidence_flag:
            reasons.append("低置信度查询,需人工确认")

        reasons = list(dict.fromkeys([*hard_block_reasons, *reasons]))  # 去重保序
        if hard_block_reasons:
            decision = "hard_block"
        elif reasons:
            decision = "approval_required"
        else:
            decision = "pass"
        out = {
            "risk_decision": decision,
            "is_sensitive": decision != "pass",  # 兼容现有 API/前端
            "sensitive_reasons": reasons,
        }
        if decision == "hard_block":
            out["blocked_reason"] = "; ".join(hard_block_reasons)
            out["final_answer"] = "查询因安全策略被阻断：" + "；".join(hard_block_reasons)
        return out

    return sensitive_check_node
