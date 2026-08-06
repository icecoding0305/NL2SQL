"""Deterministic QueryPlan compiler for plans with explicit output fields."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from nl2sql_agent.state import FilterSpec, OutputFieldSpec, QueryPlan


class UnsupportedPlanError(ValueError):
    pass


def _column_ref(value: str, default_table: str | None = None) -> exp.Column:
    if "." in value:
        table, column = value.rsplit(".", 1)
        return exp.column(column, table=table)
    return exp.column(value, table=default_table)


def _projection(field: OutputFieldSpec, dialect: str) -> exp.Expression:
    if field.expression:
        expression = sqlglot.parse_one(field.expression, read=dialect)
    elif field.column:
        expression = _column_ref(field.column, field.table)
        if field.aggregation:
            if field.aggregation == "count_distinct":
                expression = exp.Count(this=exp.Distinct(expressions=[expression]))
            else:
                aggregates = {
                    "count": exp.Count,
                    "sum": exp.Sum,
                    "avg": exp.Avg,
                    "min": exp.Min,
                    "max": exp.Max,
                }
                expression = aggregates[field.aggregation](this=expression)
    else:
        raise UnsupportedPlanError(f"输出字段 {field.concept!r} 缺少 column/expression")
    return exp.alias_(expression, field.alias, quoted=False) if field.alias else expression


def _literal(value):
    return exp.convert(value)


def _predicate(item: FilterSpec) -> exp.Expression:
    column = _column_ref(item.column, item.table)
    operator = item.operator
    if operator == "=":
        return exp.EQ(this=column, expression=_literal(item.value))
    if operator in {"!=", "<>"}:
        return exp.NEQ(this=column, expression=_literal(item.value))
    if operator == ">":
        return exp.GT(this=column, expression=_literal(item.value))
    if operator == ">=":
        return exp.GTE(this=column, expression=_literal(item.value))
    if operator == "<":
        return exp.LT(this=column, expression=_literal(item.value))
    if operator == "<=":
        return exp.LTE(this=column, expression=_literal(item.value))
    if operator in {"in", "not in"}:
        values = item.value if isinstance(item.value, (list, tuple, set)) else [item.value]
        predicate = column.isin(*[_literal(value) for value in values])
        return exp.Not(this=predicate) if operator == "not in" else predicate
    if operator == "between":
        if not isinstance(item.value, (list, tuple)) or len(item.value) != 2:
            raise UnsupportedPlanError("between 必须提供两个边界值")
        return exp.Between(this=column, low=_literal(item.value[0]), high=_literal(item.value[1]))
    if operator in {"like", "not like"}:
        predicate = exp.Like(this=column, expression=_literal(item.value))
        return exp.Not(this=predicate) if operator == "not like" else predicate
    if operator in {"is", "is not"}:
        value = exp.Null() if item.value is None or str(item.value).lower() == "null" else _literal(item.value)
        predicate = exp.Is(this=column, expression=value)
        return exp.Not(this=predicate) if operator == "is not" else predicate
    raise UnsupportedPlanError(f"不支持的过滤运算符: {operator}")


def compile_query_plan(plan: QueryPlan, dialect: str) -> tuple[str, list[str]]:
    """Compile complete explicit plans; raise to let the caller use its LLM fallback."""
    if not plan.output_fields:
        raise UnsupportedPlanError("计划未显式声明 output_fields")
    projections = [_projection(field, dialect) for field in plan.output_fields]
    query = exp.select(*projections).from_(exp.to_table(plan.target_tables[0]))

    joined_tables = {plan.target_tables[0]}
    for join in plan.join_logic:
        if join.left_table in joined_tables:
            table = join.right_table
        elif join.right_table in joined_tables:
            table = join.left_table
        else:
            raise UnsupportedPlanError("Join 顺序无法连接到当前关系树")
        condition = exp.EQ(
            this=exp.column(join.left_column, table=join.left_table),
            expression=exp.column(join.right_column, table=join.right_table),
        )
        query = query.join(exp.to_table(table), on=condition, join_type=join.join_type)
        joined_tables.add(table)

    if plan.filters:
        condition = _predicate(plan.filters[0])
        for item in plan.filters[1:]:
            condition = exp.and_(condition, _predicate(item))
        query = query.where(condition)
    if plan.group_by:
        query = query.group_by(*[_column_ref(field) for field in plan.group_by])
    if plan.output_grain.level == "entity" and plan.join_logic:
        query = query.distinct()

    if joined_tables != set(plan.target_tables):
        raise UnsupportedPlanError("target_tables 中存在未连接到关系树的表")

    sql = query.sql(dialect=dialect)
    return sql, list(plan.target_tables)
