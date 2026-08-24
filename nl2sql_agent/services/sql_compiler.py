"""Deterministic QueryPlan compiler for plans with explicit output fields."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from nl2sql_agent.state import FilterSpec, JoinSpec, OutputFieldSpec, QueryPlan
from nl2sql_agent.services.field_labels import safe_sql_alias


class UnsupportedPlanError(ValueError):
    pass


class UnsafeMultiFactPlanError(RuntimeError):
    """The plan is expressible as SQL, but compiling it flat may change results."""

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
    if not field.alias:
        return expression
    alias = safe_sql_alias(field.alias, fallback=field.concept or field.column or "字段")
    return exp.alias_(expression, alias, quoted=True)


def _literal(value):
    return exp.convert(value)


def _predicate(item: FilterSpec) -> exp.Expression:
    column = _column_ref(item.column, item.table)
    if item.aggregation:
        if item.aggregation == "count_distinct":
            column = exp.Count(this=exp.Distinct(expressions=[column]))
        else:
            aggregates = {
                "count": exp.Count,
                "sum": exp.Sum,
                "avg": exp.Avg,
                "min": exp.Min,
                "max": exp.Max,
            }
            column = aggregates[item.aggregation](this=column)
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


def _join_path(start_table: str, target_table: str, joins: list[JoinSpec]) -> list[JoinSpec]:
    """Return the shortest declared relation path without inventing a join."""
    if start_table == target_table:
        return []
    queue: list[tuple[str, list[JoinSpec]]] = [(start_table, [])]
    visited = {start_table}
    while queue:
        table, path = queue.pop(0)
        for join in joins:
            if join.left_table == table:
                neighbor = join.right_table
            elif join.right_table == table:
                neighbor = join.left_table
            else:
                continue
            if neighbor in visited:
                continue
            next_path = [*path, join]
            if neighbor == target_table:
                return next_path
            visited.add(neighbor)
            queue.append((neighbor, next_path))
    raise UnsupportedPlanError(
        f"无法从分组表 {start_table} 找到事实表 {target_table} 的已声明关系路径"
    )


def _append_joins(query: exp.Select, start_table: str, path: list[JoinSpec]) -> tuple[exp.Select, set[str]]:
    joined = {start_table}
    for join in path:
        if join.left_table in joined:
            table = join.right_table
        elif join.right_table in joined:
            table = join.left_table
        else:
            raise UnsupportedPlanError("预聚合 Join 顺序无法连接到当前关系树")
        condition = exp.EQ(
            this=exp.column(join.left_column, table=join.left_table),
            expression=exp.column(join.right_column, table=join.right_table),
        )
        query = query.join(exp.to_table(table), on=condition, join_type=join.join_type)
        joined.add(table)
    return query, joined


def _compile_multi_fact(plan: QueryPlan, dialect: str) -> tuple[str, list[str]] | None:
    """Compile multi-fact metrics with one pre-aggregation subquery per fact.

    Directly joining two detail/fact tables and aggregating afterwards silently
    multiplies measures when either side has more than one row per join key.  This
    compiler branch aggregates every measure source to the requested output grain
    first, then joins those safe result sets.
    """
    aggregate_tables = {
        field.table for field in plan.output_fields
        if field.aggregation and field.table
    }
    if len(aggregate_tables) <= 1:
        return None
    if len(plan.group_by) != 1 or "." not in plan.group_by[0]:
        raise UnsafeMultiFactPlanError("多事实指标目前要求一个明确限定表名的共同分组字段")
    group_table, group_column = plan.group_by[0].rsplit(".", 1)
    dimensions = [field for field in plan.output_fields if not field.aggregation]
    if not dimensions or any(field.table != group_table for field in dimensions):
        raise UnsafeMultiFactPlanError("多事实预聚合的非指标输出必须来自共同分组表")

    base_projection = exp.alias_(
        exp.column(group_column, table=group_table), "__group_key", quoted=False,
    )
    base = exp.select(base_projection).distinct().from_(exp.to_table(group_table))
    base_filters = [item for item in plan.filters if item.table == group_table]
    if base_filters:
        condition = _predicate(base_filters[0])
        for item in base_filters[1:]:
            condition = exp.and_(condition, _predicate(item))
        base = base.where(condition)
    query = exp.select().from_(base.subquery("group_base"))

    final_projections: list[exp.Expression] = []
    for field in dimensions:
        if field.column != group_column:
            raise UnsafeMultiFactPlanError("多事实预聚合暂不支持共同粒度之外的维度属性")
        dimension = exp.column("__group_key", table="group_base")
        final_projections.append(
            exp.alias_(dimension, field.alias, quoted=False) if field.alias else dimension
        )

    metric_index = 0
    required_fact_aliases: list[str] = []
    assigned_filter_ids = {id(item) for item in base_filters}
    assigned_having_ids: set[int] = set()
    for fact_index, fact_table in enumerate(sorted(aggregate_tables), 1):
        fields = [
            field for field in plan.output_fields
            if field.aggregation and field.table == fact_table
        ]
        try:
            path = _join_path(group_table, fact_table, plan.join_logic)
        except UnsupportedPlanError as exc:
            raise UnsafeMultiFactPlanError(str(exc)) from exc
        subquery = exp.select(
            exp.alias_(
                exp.column(group_column, table=group_table), "__group_key", quoted=False,
            )
        ).from_(exp.to_table(group_table))
        try:
            subquery, path_tables = _append_joins(subquery, group_table, path)
        except UnsupportedPlanError as exc:
            raise UnsafeMultiFactPlanError(str(exc)) from exc

        scoped_filters = [
            item for item in plan.filters
            if item.table in path_tables and item.table != group_table
        ]
        assigned_filter_ids.update(id(item) for item in scoped_filters)
        if scoped_filters:
            condition = _predicate(scoped_filters[0])
            for item in scoped_filters[1:]:
                condition = exp.and_(condition, _predicate(item))
            subquery = subquery.where(condition)

        internal_fields: list[tuple[OutputFieldSpec, str]] = []
        for field in fields:
            metric_index += 1
            internal_alias = f"__metric_{metric_index}"
            internal_fields.append((field, internal_alias))
            subquery = subquery.select(_projection(
                field.model_copy(update={"alias": internal_alias}), dialect,
            ))
        subquery = subquery.group_by(exp.column(group_column, table=group_table))
        scoped_having = [item for item in plan.having if item.table == fact_table]
        assigned_having_ids.update(id(item) for item in scoped_having)
        if scoped_having:
            condition = _predicate(scoped_having[0])
            for item in scoped_having[1:]:
                condition = exp.and_(condition, _predicate(item))
            subquery = subquery.having(condition)

        fact_alias = f"fact_agg_{fact_index}"
        condition = exp.EQ(
            this=exp.column("__group_key", table=fact_alias),
            expression=exp.column("__group_key", table="group_base"),
        )
        query = query.join(subquery.subquery(fact_alias), on=condition, join_type="left")
        if any(item.scope in {"row", "cohort"} for item in scoped_filters) or scoped_having:
            required_fact_aliases.append(fact_alias)
        for field, internal_alias in internal_fields:
            metric = exp.column(internal_alias, table=fact_alias)
            final_projections.append(
                exp.alias_(metric, field.alias, quoted=False) if field.alias else metric
            )

    if len(assigned_filter_ids) != len(plan.filters):
        raise UnsafeMultiFactPlanError("多事实过滤条件无法分配到共同粒度或事实子计划")
    if len(assigned_having_ids) != len(plan.having):
        raise UnsafeMultiFactPlanError("多事实 HAVING 条件无法分配到对应事实子计划")

    query = query.select(*final_projections)
    if required_fact_aliases:
        condition = exp.Not(this=exp.Is(
            this=exp.column("__group_key", table=required_fact_aliases[0]),
            expression=exp.Null(),
        ))
        for alias in required_fact_aliases[1:]:
            condition = exp.and_(condition, exp.Not(this=exp.Is(
                this=exp.column("__group_key", table=alias),
                expression=exp.Null(),
            )))
        query = query.where(condition)
    if plan.order_by:
        output_by_id = {
            output_id: field
            for field in plan.output_fields
            for output_id in field.source_output_ids
        }
        orders = []
        for item in plan.order_by:
            output = output_by_id.get(item.source_output_id or "")
            if output is None or not output.alias:
                raise UnsupportedPlanError("多事实预聚合排序必须引用显式输出字段")
            orders.append(exp.Ordered(
                this=exp.column(output.alias), desc=item.direction == "desc",
            ))
        query = query.order_by(*orders)
    if plan.limit:
        query = query.limit(plan.limit)
    return query.sql(dialect=dialect), list(plan.target_tables)


def compile_query_plan(plan: QueryPlan, dialect: str) -> tuple[str, list[str]]:
    """Compile complete explicit plans; raise to let the caller use its LLM fallback."""
    if not plan.output_fields:
        raise UnsupportedPlanError("计划未显式声明 output_fields")
    multi_fact = _compile_multi_fact(plan, dialect)
    if multi_fact is not None:
        return multi_fact
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
    if plan.having:
        condition = _predicate(plan.having[0])
        for item in plan.having[1:]:
            condition = exp.and_(condition, _predicate(item))
        query = query.having(condition)
    if plan.order_by:
        output_by_id = {
            output_id: field
            for field in plan.output_fields
            for output_id in field.source_output_ids
        }
        orders = []
        for item in plan.order_by:
            output = output_by_id.get(item.source_output_id or "")
            if output is not None and output.alias:
                expression = exp.column(output.alias)
            elif item.expression:
                expression = sqlglot.parse_one(item.expression, read=dialect)
            elif item.column:
                expression = _column_ref(item.column, item.table)
            else:
                raise UnsupportedPlanError(f"排序 {item.concept!r} 缺少字段或表达式")
            orders.append(exp.Ordered(this=expression, desc=item.direction == "desc"))
        query = query.order_by(*orders)
    if plan.limit:
        query = query.limit(plan.limit)
    if plan.output_grain.level == "entity" and plan.join_logic:
        query = query.distinct()

    if joined_tables != set(plan.target_tables):
        raise UnsupportedPlanError("target_tables 中存在未连接到关系树的表")

    sql = query.sql(dialect=dialect)
    return sql, list(plan.target_tables)
