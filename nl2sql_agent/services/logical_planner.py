"""Query-scoped schema projection and QueryPlan -> LogicalPlan compatibility bridge."""

from __future__ import annotations

from nl2sql_agent.services.semantic_parser import semantic_atom_map
from nl2sql_agent.services.schema_planner import output_binding_fields
from nl2sql_agent.state import (
    LogicalOperation,
    LogicalPlan,
    NL2SQLState,
    OutputFieldSpec,
    QueryMSchema,
    QueryPlan,
    QuerySchemaColumn,
    QuerySchemaRelation,
    QuerySchemaTable,
)


def build_query_mschema(state: NL2SQLState) -> QueryMSchema:
    """Build the smallest useful schema view from retrieval and grounding facts."""
    roles: dict[str, str] = {}
    selected: dict[str, set[str]] = {}
    if state.schema_plan:
        for item in state.schema_plan.anchor_tables:
            roles[item.table_name] = item.role
            selected.setdefault(item.table_name, set()).update(item.selected_columns)
        for item in state.schema_plan.dimension_tables:
            roles[item.table_name] = item.role
            selected.setdefault(item.table_name, set()).update(item.selected_columns)
        for item in state.schema_plan.bridge_tables:
            roles[item.table_name] = item.role
            selected.setdefault(item.table_name, set()).update(item.selected_columns)

    relations = state.schema_plan.relations if state.schema_plan else []
    for relation in relations:
        selected.setdefault(relation.get("source_table", ""), set()).update(
            relation.get("source_columns", [])
        )
        selected.setdefault(relation.get("target_table", ""), set()).update(
            relation.get("target_columns", [])
        )
    for binding in state.semantic_bindings.values():
        table = binding.get("table_name")
        column = binding.get("column_name")
        if table and column:
            selected.setdefault(table, set()).add(column)
    for binding in state.output_bindings.values():
        for field in output_binding_fields(binding):
            table = field.get("table_name")
            column = field.get("column_name")
            if table and column:
                selected.setdefault(table, set()).add(column)

    planned_tables = set(roles)
    tables: list[QuerySchemaTable] = []
    for hit in state.retrieved_schema:
        if planned_tables and hit.table_name not in planned_tables:
            continue
        wanted = selected.get(hit.table_name, set())
        # Never copy a complete retrieved table into a prompt. Keep grounded/planned
        # columns, relation keys and a bounded set of key/fallback columns only.
        key_columns = [
            str(column.get("name")) for column in hit.columns
            if column.get("primary_key") or column.get("unique")
        ]
        wanted.update(key_columns[:4])
        if not wanted:
            wanted.update(
                str(column.get("name")) for column in hit.columns[:8] if column.get("name")
            )
        source_columns = [c for c in hit.columns if c.get("name") in wanted][:16]
        columns = [
            QuerySchemaColumn(
                name=column.get("name", ""),
                type=column.get("type", ""),
                comment=column.get("comment", ""),
                semantic_role=column.get("semantic_role", column.get("dim_or_meas", "")),
                primary_key=bool(column.get("primary_key", False)),
                unique=bool(column.get("unique", False)),
                nullable=bool(column.get("nullable", True)),
            )
            for column in source_columns
            if column.get("name")
        ]
        catalog_table = next((
            table for table in state.retrieved_schema if table.table_name == hit.table_name
        ), None)
        tables.append(QuerySchemaTable(
            name=hit.table_name,
            comment=str(getattr(catalog_table, "comment", "") or ""),
            role=roles.get(hit.table_name, "unknown"),
            columns=columns,
            primary_keys=[column.name for column in columns if column.primary_key],
        ))

    query_relations = [
        QuerySchemaRelation(
            source_table=relation.get("source_table", ""),
            source_columns=relation.get("source_columns", []),
            target_table=relation.get("target_table", ""),
            target_columns=relation.get("target_columns", []),
            cardinality=relation.get("cardinality"),
            status=relation.get("status", ""),
        )
        for relation in relations
        if relation.get("source_table") and relation.get("target_table")
    ]
    return QueryMSchema(
        tables=tables,
        relations=query_relations,
        semantic_bindings=state.semantic_bindings,
        output_bindings=state.output_bindings,
    )


def _join_kind(source_atom_ids: list[str], state: NL2SQLState) -> str:
    atoms = semantic_atom_map(state.semantic_graph)
    types = {atoms[atom_id].predicate_type for atom_id in source_atom_ids if atom_id in atoms}
    if "not_exists" in types:
        return "anti_join"
    if "exists" in types:
        return "semi_join"
    return "join"


def build_logical_plan(plan: QueryPlan, state: NL2SQLState) -> LogicalPlan:
    """Translate the legacy flat plan into an auditable relational operator DAG."""
    operations: list[LogicalOperation] = []
    scan_ids: dict[str, str] = {}
    for index, table in enumerate(plan.target_tables, 1):
        scan_id = f"scan_{index}"
        scan_ids[table] = scan_id
        operations.append(LogicalOperation(id=scan_id, kind="scan", table=table))

    root = scan_ids[plan.target_tables[0]]
    for index, join in enumerate(plan.join_logic, 1):
        right_input = scan_ids.get(join.right_table, scan_ids.get(join.left_table, root))
        op_id = f"join_{index}"
        kind = _join_kind(join.source_atom_ids, state)
        operations.append(LogicalOperation(
            id=op_id,
            kind=kind,
            inputs=list(dict.fromkeys([root, right_input])),
            join=join,
            source_atom_ids=join.source_atom_ids,
        ))
        root = op_id

    if plan.filters:
        operations.append(LogicalOperation(
            id="filter_1",
            kind="filter",
            inputs=[root],
            predicates=plan.filters,
            source_atom_ids=list(dict.fromkeys(
                atom_id for item in plan.filters for atom_id in item.source_atom_ids
            )),
        ))
        root = "filter_1"

    if plan.metric_logic or plan.group_by:
        operations.append(LogicalOperation(
            id="aggregate_1",
            kind="aggregate",
            inputs=[root],
            group_by=plan.group_by,
            metric=plan.metric_logic,
            source_atom_ids=plan.metric_logic.source_atom_ids if plan.metric_logic else [],
        ))
        root = "aggregate_1"

    operations.append(LogicalOperation(
        id="project_1",
        kind="project",
        inputs=[root],
        fields=plan.output_fields,
    ))
    return LogicalPlan(
        operations=operations,
        root_operation_id="project_1",
        output_fields=plan.output_fields,
        output_grain=plan.output_grain,
        covered_atom_ids=plan.covered_atom_ids,
        confidence=plan.confidence,
    )


def validate_logical_plan(plan: LogicalPlan, query_mschema: QueryMSchema) -> list[str]:
    errors: list[str] = []
    operation_ids = {operation.id for operation in plan.operations}
    if len(operation_ids) != len(plan.operations):
        errors.append("LogicalPlan operation id 重复")
    if plan.root_operation_id not in operation_ids:
        errors.append("LogicalPlan root_operation_id 不存在")
    for operation in plan.operations:
        missing = set(operation.inputs) - operation_ids
        if missing:
            errors.append(f"LogicalPlan 操作 {operation.id} 引用了不存在的输入 {sorted(missing)}")

    if plan.output_grain.level == "aggregate":
        aggregate = next((op for op in plan.operations if op.kind == "aggregate"), None)
        if aggregate is None:
            errors.append("输出粒度为 aggregate，但 LogicalPlan 缺少 aggregate 操作")
        elif plan.output_grain.keys and set(plan.output_grain.keys) != set(aggregate.group_by):
            errors.append("输出粒度 keys 与 aggregate.group_by 不一致")
    if plan.output_grain.level == "global" and plan.output_grain.keys:
        errors.append("global 输出粒度不能包含分组 keys")

    for operation in plan.operations:
        if operation.kind != "join" or operation.join is None:
            continue
        join = operation.join
        relation = next((r for r in query_mschema.relations if {
            r.source_table, r.target_table
        } == {join.left_table, join.right_table}), None)
        cardinality = (relation.cardinality or "").lower() if relation else ""
        one_side = None
        if relation and cardinality in {"one_to_many", "1:n", "one-to-many"}:
            one_side = relation.source_table
        elif relation and cardinality in {"many_to_one", "n:1", "many-to-one"}:
            one_side = relation.target_table
        many_to_many = cardinality in {"many_to_many", "n:m", "many-to-many"}
        entity_table = None
        if plan.output_grain.entity:
            entity_table = next((
                table.name for table in query_mschema.tables
                if table.role == "entity" and plan.output_grain.entity
            ), None)
        if plan.output_grain.level == "entity" and (
            many_to_many or (one_side is not None and entity_table == one_side)
        ):
            errors.append(
                f"实体粒度查询使用 {cardinality} 普通 JOIN 可能放大行数；应使用 semi_join、先聚合或去重"
            )
    return errors
