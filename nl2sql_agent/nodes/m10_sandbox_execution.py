"""模块 10:沙箱执行。

- 使用只读账号连接(PostgresExecutor:READ ONLY 事务 + statement_timeout)
- 执行前跑 EXPLAIN,预估扫描行数超过阈值直接拒绝,不执行
- 未聚合查询强制加 LIMIT(值可配置)
- 设置查询超时,超时视为 execution_error

执行报错时,带着具体报错文本退回模块 7(SQL 生成)重试；空结果是合法业务结果，
直接进入结果解释节点，不得通过改写 SQL 放宽查询条件。
不退回模块 5b(计划)。重试次数打满后降级为"生成失败,请人工介入",避免死循环。
"""

from __future__ import annotations

from typing import Any

from nl2sql_agent.state import NL2SQLState


def _mark_candidate(state: NL2SQLState, status: str, error: str | None = None) -> list:
    return [
        candidate.model_copy(update={"status": status, "execution_error": error})
        if candidate.stage == "sql" and candidate.selected else candidate
        for candidate in state.query_candidates
    ]


def _exec_fail(state: NL2SQLState, msg: str) -> dict[str, Any]:
    new_count = state.retry_count + 1
    out: dict[str, Any] = {
        "execution_error": msg,
        "retry_count": new_count,
        **({"query_candidates": _mark_candidate(state, "execution_error", msg)}
           if state.query_candidates else {}),
    }
    if new_count >= state.max_retries:
        out["final_answer"] = f"执行多次失败({msg}),请人工介入"
    return out


def make_sandbox_execution_node(deps):
    def sandbox_execution_node(state: NL2SQLState) -> NL2SQLState | dict:
        sql = state.generated_sql or ""
        if not sql:
            return _exec_fail(state, "没有可执行的 SQL")

        # 1. 执行前 EXPLAIN 守门
        try:
            est = deps.executor.explain(sql)
        except Exception as e:  # noqa: BLE001
            return _exec_fail(state, f"EXPLAIN 失败: {e}")
        threshold = deps.config.explain_row_threshold
        if est.estimated_rows > threshold:
            return _exec_fail(
                state,
                f"EXPLAIN 预估扫描 {est.estimated_rows} 行,超过阈值 {threshold},拒绝执行",
            )

        # 2. 未聚合查询强制 LIMIT
        expr = deps.sql.parse(sql, deps.config.dialect)
        if not deps.sql.has_aggregate_or_limit(expr):
            sql = deps.sql.enforce_limit(expr, deps.config.execution_limit, deps.config.dialect)

        # 3. 执行(带超时)
        try:
            rows = deps.executor.execute(
                sql, timeout_seconds=deps.config.execution_timeout_seconds
            )
        except Exception as e:  # noqa: BLE001
            return _exec_fail(state, f"执行报错: {e}")

        return {
            "execution_result": rows,
            "execution_error": None,
            **({"query_candidates": _mark_candidate(state, "executed")}
               if state.query_candidates else {}),
        }

    return sandbox_execution_node
