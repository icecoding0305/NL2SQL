"""模块 8:静态校验(基于 sqlglot AST,不用正则)。

检查三件事:
1. 语法与方言是否合法(方言从配置读取)
2. 引用的表字段是否都在 retrieved_schema 内,且与 used_tables 一致(防字段幻觉)
3. 是否命中 DROP/DELETE/UPDATE/TRUNCATE 等危险操作——命中直接判失败,不进入重试

同时用代码逻辑(不交给 LLM)读取独立的 row_level_filters 并注入 WHERE；
data_scope 只负责系统/表权限，不能作为表内字段过滤值。
校验不过(非危险类错误)带着具体错误退回模块 7 重试。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlglot import exp

from nl2sql_agent.state import NL2SQLState


def _candidate_update(
    state: NL2SQLState, status: str, errors: list[str] | None = None, sql: str | None = None
) -> list:
    """Update only the selected SQL candidate; retain rejected attempts for audit."""
    updated = []
    for candidate in state.query_candidates:
        if candidate.stage == "sql" and candidate.selected:
            updated.append(candidate.model_copy(update={
                "status": status,
                "validation_errors": list(errors or []),
                "sql": sql if sql is not None else candidate.sql,
            }))
        else:
            updated.append(candidate)
    return updated


def _fail(state: NL2SQLState, errors: list[str]) -> dict[str, Any]:
    sql_hash = hashlib.sha256((state.generated_sql or "").encode("utf-8")).hexdigest()
    failed_hashes = list(state.failed_sql_hashes or [])
    repeated = bool(state.generated_sql) and sql_hash in failed_hashes
    if state.generated_sql and not repeated:
        failed_hashes.append(sql_hash)
    non_retryable = state.sql_generation_source == "deterministic" or repeated
    new_count = state.max_retries if non_retryable else state.retry_count + 1
    out: dict[str, Any] = {
        "validation_errors": [*state.validation_errors[-5:], *errors],
        "retry_count": new_count,
        "failed_sql_hashes": failed_hashes,
        **({"query_candidates": _candidate_update(state, "rejected", errors)}
           if state.query_candidates else {}),
    }
    if new_count >= state.max_retries:
        out["terminal_status"] = "error"
        reason = "确定性 SQL 编译结果未通过校验" if non_retryable else "SQL 生成多次校验失败"
        out["final_answer"] = reason + ",请人工介入\n" + "；".join(errors[:5])
    return out


def _plan_shape_errors(expr, state: NL2SQLState) -> list[str]:
    """Verify material QueryPlan operators survived SQL translation."""
    plan = state.query_plan
    if plan is None:
        return []
    selects = list(expr.find_all(exp.Select))
    root_select = expr if isinstance(expr, exp.Select) else (selects[0] if selects else None)
    if root_select is None:
        return ["SQL 缺少可验证的 SELECT 结构"]

    has_where = any(select.args.get("where") is not None for select in selects)
    has_having = any(select.args.get("having") is not None for select in selects)
    has_group = any(select.args.get("group") is not None for select in selects)
    root_order = root_select.args.get("order")
    root_limit = root_select.args.get("limit")
    errors: list[str] = []

    if bool(plan.filters) != has_where:
        errors.append("SQL WHERE 与已验证 QueryPlan 的过滤要求不一致")
    if bool(plan.having) != has_having:
        errors.append("SQL HAVING 与已验证 QueryPlan 的聚合过滤要求不一致")
    if bool(plan.group_by) != has_group:
        errors.append("SQL GROUP BY 与已验证 QueryPlan 的分组要求不一致")

    actual_orders = list(root_order.expressions) if root_order is not None else []
    if len(actual_orders) != len(plan.order_by):
        errors.append("SQL ORDER BY 数量与已验证 QueryPlan 不一致")
    else:
        for expected, actual in zip(plan.order_by, actual_orders):
            actual_direction = "desc" if bool(actual.args.get("desc")) else "asc"
            if actual_direction != expected.direction:
                errors.append(
                    f"SQL 排序方向不一致：{expected.concept} 应为 {expected.direction}"
                )

    actual_limit = None
    if root_limit is not None and root_limit.expression is not None:
        try:
            actual_limit = int(root_limit.expression.this)
        except (TypeError, ValueError):
            actual_limit = None
    if actual_limit != plan.limit:
        errors.append(
            f"SQL LIMIT 与已验证 QueryPlan 不一致：期望={plan.limit!r}，实际={actual_limit!r}"
        )
    return errors


def make_static_validation_node(deps):
    def static_validation_node(state: NL2SQLState) -> NL2SQLState | dict:
        sql = state.generated_sql
        if not sql:
            return _fail(state, ["SQL 为空"])

        dialect = deps.config.dialect
        sqlsvc = deps.sql

        # 1. 语法 + 方言合法性
        try:
            expr = sqlsvc.parse(sql, dialect)
        except Exception as e:  # noqa: BLE001
            return _fail(state, [f"SQL 语法错误({dialect}): {e}"])

        # 2. 危险操作:AST 结构判定,命中即硬失败,不进入重试
        danger = sqlsvc.is_dangerous(expr)
        if danger:
            return {
                "validation_errors": [f"命中危险操作: {danger},禁止执行"],
                "blocked_reason": danger,
                "final_answer": f"SQL 被拦截: 检测到危险操作 {danger}",
                **({"query_candidates": _candidate_update(
                    state, "rejected", [f"命中危险操作: {danger}"]
                )} if state.query_candidates else {}),
            }

        plan_shape_errors = _plan_shape_errors(expr, state)
        if plan_shape_errors:
            return _fail(state, plan_shape_errors)

        # data_scope 是系统命名空间，不是任何表字段的业务值。即使模型或旧 few-shot
        # 误把 risk_mart/dw/core 写进 WHERE，也在确定性校验层拦截并要求重新生成。
        scope_values = set(state.data_scope or [])
        leaked_scope_values = {
            str(literal.this)
            for literal in expr.find_all(exp.Literal)
            if literal.is_string and str(literal.this) in scope_values
        }
        if leaked_scope_values:
            return _fail(
                state,
                [
                    "SQL 把系统命名空间误作表字段值: "
                    f"{sorted(leaked_scope_values)}；data_scope 只能用于表权限"
                ],
            )

        # 3. 表引用一致性:AST 表 ⊆ used_tables,且 ⊆ retrieved_schema
        ref_tables = sqlsvc.extract_tables(expr)
        known_tables = {h.table_name for h in state.retrieved_schema}
        used_tables = set(state.used_tables or [])
        if not ref_tables:
            return _fail(state, ["SQL 未引用任何表"])
        inconsistent = (set(ref_tables) - used_tables) | (set(ref_tables) - known_tables)
        if inconsistent:
            return _fail(
                state,
                [f"引用了未检索到/未声明的表: {sorted(inconsistent)}"
                 f"(AST表={ref_tables}, used_tables={sorted(used_tables)})"],
            )

        # 4. 字段幻觉:引用字段必须存在于某张检索到的表
        # 先收集 SELECT 定义的别名(ORDER BY / GROUP BY 引用别名是合法 SQL,
        # 如 "SELECT SUM(x) AS t ... ORDER BY t",不能误判为字段幻觉)
        select_aliases = {
            e.alias_or_name
            for sel in expr.find_all(exp.Select)
            for e in sel.expressions
            if isinstance(e, exp.Alias)
        }
        # 解析表别名:FROM dwd_ar_loan_info t1 → {"t1": "dwd_ar_loan_info"},
        # 字段校验按真实表名进行,避免把别名 t1/t2 误判为不存在的表
        alias_map: dict[str, str] = {}
        for t in expr.find_all(exp.Table):
            if t.alias and t.alias != t.name:
                alias_map[t.alias] = t.name
        derived_aliases = {
            subquery.alias
            for subquery in expr.find_all(exp.Subquery)
            if subquery.alias
        }

        table_cols = {h.table_name: {c["name"] for c in h.columns} for h in state.retrieved_schema}
        errors: list[str] = []

        # 返回字段完整性：即使 SQL 来自 LLM 降级路径，也必须实现已校验计划中的
        # 每一个显式物理输出，不能生成可执行但缺列的 SQL。
        root_select = next(expr.find_all(exp.Select), None)
        projected_columns: set[tuple[str | None, str]] = set()
        projected_aliases: set[str] = set()
        if root_select is not None:
            for projection in root_select.expressions:
                if projection.alias_or_name:
                    projected_aliases.add(projection.alias_or_name)
                for column_expr in projection.find_all(exp.Column):
                    qualifier = column_expr.table or None
                    projected_columns.add((alias_map.get(qualifier, qualifier), column_expr.name))
        for output in state.query_plan.output_fields if state.query_plan else []:
            if not output.table or not output.column:
                continue
            qualified = (output.table, output.column) in projected_columns
            unqualified = (None, output.column) in projected_columns
            aliased = bool(output.alias and output.alias in projected_aliases)
            owners = [
                table for table in ref_tables
                if output.column in table_cols.get(table, set())
            ]
            if not aliased and not qualified and not (unqualified and owners == [output.table]):
                errors.append(
                    f"SQL SELECT 遗漏计划返回字段 {output.table}.{output.column}"
                )

        for tbl, col in sqlsvc.extract_columns(expr):
            if tbl is not None:
                if tbl in derived_aliases:
                    continue
                real = alias_map.get(tbl, tbl)
                if real not in table_cols or col not in table_cols[real]:
                    errors.append(f"表 {real} 不存在字段 {col}")
            else:
                if col in select_aliases:
                    continue  # SELECT 别名的合法引用,跳过字段检查
                if not any(col in cols for cols in table_cols.values()):
                    errors.append(f"字段 {col} 不在检索到的 schema 内(疑似字段幻觉)")
        if errors:
            return _fail(state, errors)

        # 5. 行级权限注入。系统权限 data_scope 与表内维度没有取值关系，严禁复用；
        # 这里只接受服务端鉴权层注入的独立 row_level_filters。
        filter_cfg = deps.config.row_level_filter
        if filter_cfg.get("enabled", True):
            column = filter_cfg.get("column", "business_line")
            # 只处理 SQL 实际引用且确实包含权限列的表；JOIN 中的相关表分别按别名注入。
            qualifiers: list[str] = []
            for table in expr.find_all(exp.Table):
                real = table.name
                if column not in table_cols.get(real, set()):
                    continue
                qualifier = table.alias if table.alias and table.alias != real else real
                if qualifier not in qualifiers:
                    qualifiers.append(qualifier)
            if qualifiers:
                values = state.row_level_filters.get(column, [])
                if not values:
                    reason = f"已启用行级过滤 {column},但服务端未提供可信权限值"
                    return {
                        "validation_errors": [reason],
                        "blocked_reason": reason,
                        "final_answer": "查询因行级权限配置不完整而被阻断。",
                        **({"query_candidates": _candidate_update(state, "rejected", [reason])}
                           if state.query_candidates else {}),
                    }
                injected = sqlsvc.to_sql(expr, dialect)
                for qualifier in qualifiers:
                    injected_expr = sqlsvc.parse(injected, dialect)
                    injected = sqlsvc.inject_row_level_filter(
                        injected_expr, dialect, column, qualifier, values
                    )
                return {
                    "generated_sql": injected,
                    "validation_errors": [],
                    **({"query_candidates": _candidate_update(state, "validated", sql=injected)}
                       if state.query_candidates else {}),
                }

        return {
            "validation_errors": [],
            **({"query_candidates": _candidate_update(state, "validated")}
               if state.query_candidates else {}),
        }

    return static_validation_node
