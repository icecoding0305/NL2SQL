"""Query-scoped schema projection and QueryPlan -> LogicalPlan compatibility bridge."""

from __future__ import annotations

import json
from typing import Any, Iterable

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


_DEFAULT_SCHEMA_POLICY = {
    "precision": {"max_tables": 4, "max_total_columns": 32, "max_optional_columns": 8},
    "recall": {"max_tables": 6, "max_total_columns": 56, "max_optional_columns": 20},
    "execution": {"max_tables": 8, "max_total_columns": 64, "max_optional_columns": 0},
}


def query_mschema_runtime_kwargs(state: NL2SQLState, deps) -> dict:
    """Collect complete internal facts for bounded Query M-Schema projection."""
    from nl2sql_agent.services.schema_ingest.text_builder import load_mschema_vector_source

    source = load_mschema_vector_source(getattr(deps.catalog, "metadata", {}))
    relations = list(source[0].get("relations", [])) if source is not None else []
    relations.extend(getattr(deps.catalog, "relation_overrides", []))
    return {
        "policy": deps.config.clarification_rules.get("query_mschema", {}),
        "available_tables": list(deps.catalog.tables_for_scope(state.data_scope)),
        "available_relations": relations,
    }


def _policy_for(profile: str, policy: dict | None) -> dict[str, int]:
    configured = (policy or {}).get(profile, {})
    return {
        key: int(configured.get(key, value))
        for key, value in _DEFAULT_SCHEMA_POLICY[profile].items()
    }


def _table_name(table: Any) -> str:
    return str(getattr(table, "table_name", None) or getattr(table, "name", ""))


def _table_columns(table: Any) -> list[dict]:
    return list(getattr(table, "columns", []) or [])


def _trusted_relation(relation: dict) -> bool:
    relation_type = str(relation.get("relation_type") or "foreign_key")
    status = str(relation.get("status") or "")
    enabled = relation.get("enabled", True)
    return bool(enabled) and (
        relation_type == "foreign_key" or status in {"verified", "confirmed", "approved"}
    )


def _merge_relations(*groups: Iterable[dict]) -> list[dict]:
    merged: dict[tuple, dict] = {}
    for relation in (item for group in groups for item in group):
        if not _trusted_relation(relation):
            continue
        key = (
            relation.get("source_table"), tuple(relation.get("source_columns") or []),
            relation.get("target_table"), tuple(relation.get("target_columns") or []),
        )
        if key[0] and key[2]:
            merged[key] = dict(relation)
    return list(merged.values())


def _recall_expansion_reasons(state: NL2SQLState) -> list[str]:
    evidence = [
        *(state.plan_validation_errors or []),
        *(state.schema_plan.unresolved_slots if state.schema_plan else []),
    ]
    reasons: list[str] = []
    for text in map(str, evidence):
        if any(marker in text for marker in ("关联", "JOIN", "Join", "join", "路径")):
            reasons.append("relation_path")
        if any(marker in text for marker in (
            "字段", "输出", "分组", "排序", "不存在", "外部表", "target_tables",
        )):
            reasons.append("schema_coverage")
    return list(dict.fromkeys(reasons))


def _plan_column_refs(plan: QueryPlan | None) -> dict[str, set[str]]:
    refs: dict[str, set[str]] = {}
    if plan is None:
        return refs

    def add(table: str | None, column: str | None) -> None:
        if table and column:
            refs.setdefault(table, set()).add(column)

    for field in plan.output_fields:
        add(field.table, field.column)
    for item in [*plan.filters, *plan.having]:
        add(item.table, item.column)
    for item in plan.order_by:
        add(item.table, item.column)
    for join in plan.join_logic:
        add(join.left_table, join.left_column)
        add(join.right_table, join.right_column)
    for ref in [*plan.group_by, *plan.output_grain.keys]:
        if "." in ref:
            table, column = ref.split(".", 1)
            add(table, column)
    if plan.metric_logic:
        for ref in plan.metric_logic.columns:
            if "." in ref:
                table, column = ref.split(".", 1)
                add(table, column)
    return refs


def validate_query_mschema_contract(
    state: NL2SQLState,
    schema: QueryMSchema,
    query_plan: QueryPlan | None = None,
) -> list[str]:
    """Verify that prompt Schema covers every already-grounded contract fact."""
    available = {
        table.name: {column.name for column in table.columns}
        for table in schema.tables
    }
    errors: list[str] = []

    def require(table: str | None, column: str | None, label: str) -> None:
        if table and column and column not in available.get(table, set()):
            errors.append(f"{label}缺少字段:{table}.{column}")

    for atom_id, binding in state.semantic_bindings.items():
        require(binding.get("table_name"), binding.get("column_name"), f"语义绑定{atom_id}")
    for output_id, binding in state.output_bindings.items():
        for field in output_binding_fields(binding):
            require(field.get("table_name"), field.get("column_name"), f"输出绑定{output_id}")
    if state.semantic_graph is not None:
        for output in state.semantic_graph.outputs:
            if output.required and output.id not in state.output_bindings:
                errors.append(f"必需输出未绑定:{output.id}:{output.concept}")
    for relation in schema.relations:
        for column in relation.source_columns:
            require(relation.source_table, column, "关系")
        for column in relation.target_columns:
            require(relation.target_table, column, "关系")
    for table, columns in _plan_column_refs(query_plan).items():
        for column in columns:
            require(table, column, "QueryPlan")
    if query_plan:
        missing_tables = set(query_plan.target_tables) - set(available)
        if missing_tables:
            errors.append(f"QueryPlan缺少表:{','.join(sorted(missing_tables))}")
    table_names = set(available)
    if len(table_names) > 1:
        adjacency: dict[str, set[str]] = {table: set() for table in table_names}
        for relation in schema.relations:
            if relation.source_table in table_names and relation.target_table in table_names:
                adjacency[relation.source_table].add(relation.target_table)
                adjacency[relation.target_table].add(relation.source_table)
        seen: set[str] = set()
        stack = [next(iter(table_names))]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(adjacency.get(current, set()) - seen)
        if seen != table_names:
            errors.append(f"Query M-Schema表不连通:{','.join(sorted(table_names - seen))}")
    return list(dict.fromkeys(errors))


def build_query_mschema(
    state: NL2SQLState,
    profile: str = "precision",
    *,
    policy: dict | None = None,
    available_tables: Iterable[Any] | None = None,
    available_relations: Iterable[dict] | None = None,
) -> QueryMSchema:
    """Build a validated query-scoped Schema under a global context budget."""
    if profile not in {"precision", "recall", "execution"}:
        raise ValueError(f"unsupported Query M-Schema profile: {profile}")
    limits = _policy_for(profile, policy)
    roles: dict[str, str] = {}
    required: dict[str, set[str]] = {}
    optional: dict[str, dict[str, float]] = {}
    if state.schema_plan:
        for item in state.schema_plan.anchor_tables:
            roles[item.table_name] = item.role
            required.setdefault(item.table_name, set()).update(item.selected_columns)
        for item in state.schema_plan.dimension_tables:
            roles[item.table_name] = item.role
            required.setdefault(item.table_name, set()).update(item.selected_columns)
        for item in state.schema_plan.bridge_tables:
            roles[item.table_name] = item.role
            required.setdefault(item.table_name, set()).update(item.selected_columns)
    initial_role_count = len(roles)

    relations = _merge_relations(
        state.schema_plan.relations if state.schema_plan else [],
        available_relations or [],
    )
    if profile == "execution" and state.query_plan:
        exact_refs = _plan_column_refs(state.query_plan)
        roles = {
            table: roles.get(table, "unknown") for table in state.query_plan.target_tables
        }
        required = {table: set(columns) for table, columns in exact_refs.items()}
        for table in state.query_plan.target_tables:
            required.setdefault(table, set())
        joined_pairs = [
            {join.left_table, join.right_table} for join in state.query_plan.join_logic
        ]
        relations = [
            relation for relation in relations
            if {relation.get("source_table"), relation.get("target_table")} in joined_pairs
        ]

    for relation in relations:
        if relation.get("source_table") not in roles or relation.get("target_table") not in roles:
            continue
        required.setdefault(relation.get("source_table", ""), set()).update(
            relation.get("source_columns", [])
        )
        required.setdefault(relation.get("target_table", ""), set()).update(
            relation.get("target_columns", [])
        )
    for binding in state.semantic_bindings.values():
        table = binding.get("table_name")
        column = binding.get("column_name")
        if table and column:
            required.setdefault(table, set()).add(column)
    for binding in state.output_bindings.values():
        for field in output_binding_fields(binding):
            table = field.get("table_name")
            column = field.get("column_name")
            if table and column:
                required.setdefault(table, set()).add(column)

    if profile == "recall":
        by_slot: dict[str, list] = {}
        for candidate in state.field_candidates:
            by_slot.setdefault(candidate.query_slot, []).append(candidate)
        for candidates in by_slot.values():
            candidates.sort(key=lambda item: item.final_score, reverse=True)
            top_score = candidates[0].final_score if candidates else 0.0
            threshold = max(0.42, top_score * 0.75)
            for candidate in candidates:
                if candidate.table_name in roles and candidate.final_score >= threshold:
                    optional.setdefault(candidate.table_name, {})[candidate.column_name] = candidate.final_score

        # Only a failed/incomplete plan may add a trusted one-hop table.  This
        # recovers omitted dimensions without ever exposing a disconnected table.
        expansion_reasons = _recall_expansion_reasons(state)
        expansion_needed = bool(expansion_reasons)
        if expansion_needed:
            planned = set(roles)
            for candidate in sorted(state.field_candidates, key=lambda item: -item.final_score):
                if candidate.table_name in roles or candidate.final_score < 0.65:
                    continue
                connecting = next((
                    relation for relation in relations
                    if candidate.table_name in {
                        relation.get("source_table"), relation.get("target_table")
                    }
                    and bool(planned & {
                        relation.get("source_table"), relation.get("target_table")
                    })
                ), None)
                if connecting is None or len(roles) >= limits["max_tables"]:
                    continue
                roles[candidate.table_name] = (
                    "secondary_fact" if candidate.semantic_role == "measure" else "dimension"
                )
                optional.setdefault(candidate.table_name, {})[candidate.column_name] = candidate.final_score
                planned.add(candidate.table_name)
                for table_key, column_key in (
                    ("source_table", "source_columns"), ("target_table", "target_columns")
                ):
                    required.setdefault(connecting.get(table_key, ""), set()).update(
                        connecting.get(column_key, [])
                    )

    source_tables = list(available_tables or state.retrieved_schema)
    source_by_name = {_table_name(table): table for table in source_tables}
    for hit in state.retrieved_schema:
        source_by_name.setdefault(hit.table_name, hit)

    # Add at most one useful identity key per planned table. Required contract
    # fields are never removed; optional alternatives consume the remaining budget.
    for table_name in roles:
        source = source_by_name.get(table_name)
        if source is None:
            continue
        identity = next((
            str(column.get("name")) for column in _table_columns(source)
            if column.get("primary_key") or column.get("unique")
        ), None)
        if identity:
            required.setdefault(table_name, set()).add(identity)

    required_count = sum(len(columns) for columns in required.values())
    optional_budget = min(
        limits["max_optional_columns"],
        max(0, limits["max_total_columns"] - required_count),
    )
    optional_ranked = sorted(
        (
            (score, table, column)
            for table, columns in optional.items()
            for column, score in columns.items()
            if column not in required.get(table, set())
        ),
        reverse=True,
    )
    selected = {table: set(columns) for table, columns in required.items()}
    for _, table, column in optional_ranked[:optional_budget]:
        selected.setdefault(table, set()).add(column)

    tables: list[QuerySchemaTable] = []
    for table_name in roles:
        hit = source_by_name.get(table_name)
        if hit is None:
            continue
        wanted = selected.get(table_name, set())
        source_columns = [c for c in _table_columns(hit) if c.get("name") in wanted]
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
        tables.append(QuerySchemaTable(
            name=table_name,
            comment=str(getattr(hit, "comment", "") or ""),
            role=roles.get(table_name, "unknown"),
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
        if relation.get("source_table") in roles and relation.get("target_table") in roles
    ]
    schema = QueryMSchema(
        profile=profile,
        tables=tables,
        relations=query_relations,
        semantic_bindings=state.semantic_bindings,
        output_bindings=state.output_bindings,
        metrics={
            "table_count": len(tables),
            "column_count": sum(len(table.columns) for table in tables),
            "required_column_count": required_count,
            "optional_column_count": min(optional_budget, len(optional_ranked)),
            "max_tables": limits["max_tables"],
            "max_total_columns": limits["max_total_columns"],
            "budget_exceeded": len(roles) > limits["max_tables"] or required_count > limits["max_total_columns"],
            "expansion_reasons": (
                _recall_expansion_reasons(state) if profile == "recall" else []
            ),
            "expanded_table_count": (
                max(0, len(roles) - initial_role_count) if profile == "recall" else 0
            ),
        },
    )
    schema.warnings = validate_query_mschema_contract(
        state, schema, state.query_plan if profile == "execution" else None
    )
    if profile == "execution" and state.query_plan is not None:
        grain_risks = validate_logical_plan(
            build_logical_plan(state.query_plan, state), schema
        )
        schema.warnings = list(dict.fromkeys([*schema.warnings, *grain_risks]))
        schema.metrics["grain_risk_count"] = len(grain_risks)
    schema.metrics["coverage_error_count"] = len(schema.warnings)
    schema.metrics["contract_complete"] = not schema.warnings
    prompt_payload = schema.model_dump(exclude={"metrics", "warnings"})
    serialized_chars = len(json.dumps(prompt_payload, ensure_ascii=False, default=str))
    schema.metrics["serialized_char_count"] = serialized_chars
    schema.metrics["estimated_token_count"] = max(1, (serialized_chars + 2) // 3)
    return schema


def build_query_mschema_bundle(
    state: NL2SQLState, **kwargs
) -> tuple[QueryMSchema, QueryMSchema]:
    """Return both prompt-safe views from the same grounding facts."""
    return (
        build_query_mschema(state, "precision", **kwargs),
        build_query_mschema(state, "recall", **kwargs),
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

    if plan.metric_logic or plan.group_by or any(field.aggregation for field in plan.output_fields):
        operations.append(LogicalOperation(
            id="aggregate_1",
            kind="aggregate",
            inputs=[root],
            group_by=plan.group_by,
            metric=plan.metric_logic,
            source_atom_ids=plan.metric_logic.source_atom_ids if plan.metric_logic else [],
        ))
        root = "aggregate_1"

    if plan.having:
        operations.append(LogicalOperation(
            id="having_1",
            kind="having",
            inputs=[root],
            predicates=plan.having,
            source_atom_ids=list(dict.fromkeys(
                atom_id for item in plan.having for atom_id in item.source_atom_ids
            )),
        ))
        root = "having_1"

    operations.append(LogicalOperation(
        id="project_1",
        kind="project",
        inputs=[root],
        fields=plan.output_fields,
    ))
    root = "project_1"
    if plan.order_by:
        operations.append(LogicalOperation(
            id="sort_1",
            kind="sort",
            inputs=[root],
            sort_by=[
                f"{item.concept} {item.direction.upper()}" for item in plan.order_by
            ],
        ))
        root = "sort_1"
    if plan.limit:
        operations.append(LogicalOperation(
            id="limit_1",
            kind="limit",
            inputs=[root],
            limit=plan.limit,
        ))
        root = "limit_1"
    return LogicalPlan(
        operations=operations,
        root_operation_id=root,
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
        if plan.output_grain.level in {"aggregate", "global"} and many_to_many:
            errors.append(
                f"聚合查询使用 {cardinality} 普通 JOIN 可能重复累计指标；"
                "应先按共同粒度预聚合或配置更准确的关系基数"
            )
    return errors
