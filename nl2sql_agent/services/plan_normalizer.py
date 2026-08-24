"""Deterministic normalization for structural semantic atoms in QueryPlan."""

from __future__ import annotations

from nl2sql_agent.services.semantic_parser import iter_semantic_atoms
from nl2sql_agent.services.schema_planner import output_binding_fields
from nl2sql_agent.state import FilterSpec, OrderSpec, OutputFieldSpec, QueryPlan, SemanticPredicate


def _descendant_atom_ids(predicate: SemanticPredicate) -> set[str]:
    ids: set[str] = set()
    for child in predicate.children:
        if child.materiality == "high" and child.predicate_type not in {"and", "or", "not"}:
            ids.add(child.atom_id)
        ids.update(_descendant_atom_ids(child))
    return ids


def _implemented_atom_ids(plan: QueryPlan) -> set[str]:
    atom_ids: set[str] = set()
    for item in plan.filters:
        atom_ids.update(item.source_atom_ids)
    for item in plan.having:
        atom_ids.update(item.source_atom_ids)
    for item in plan.join_logic:
        atom_ids.update(item.source_atom_ids)
    if plan.metric_logic:
        atom_ids.update(plan.metric_logic.source_atom_ids)
    return atom_ids


def _implemented_output_ids(plan: QueryPlan) -> set[str]:
    return {
        output_id
        for field in plan.output_fields
        for output_id in field.source_output_ids
    }


def normalize_structural_coverage(
    plan: QueryPlan,
    semantic_graph,
    output_bindings: dict[str, dict] | None = None,
    semantic_bindings: dict[str, dict] | None = None,
) -> tuple[QueryPlan, list[str]]:
    """Attach provably implemented positive ``exists`` atoms to physical operators.

    The model often implements every child predicate and the required relation but
    forgets to repeat the parent exists atom id. This function repairs only that
    bookkeeping gap. Negative existence is intentionally excluded because it needs
    explicit anti-join/NOT EXISTS semantics and cannot be inferred safely.
    """
    if semantic_graph is None:
        return plan, []

    normalized = plan.model_copy(deep=True)
    changes: list[str] = []
    # 字段检索与值画像已经形成可审计的物理绑定后，LLM 只负责计划结构。
    # 对带 atom id 的过滤条件强制采用绑定事实，避免模型把“上海市”改回“上海”。
    for atom_id, binding in (semantic_bindings or {}).items():
        atom = next((
            item for item in iter_semantic_atoms(semantic_graph.predicate)
            if item.atom_id == atom_id
        ), None)
        target_items = (
            normalized.having
            if atom is not None and atom.predicate_type == "aggregate_comparison"
            else normalized.filters
        )
        target_index = next((
            index for index, item in enumerate(target_items)
            if atom_id in item.source_atom_ids
        ), None)
        if target_index is None:
            if atom is None or atom.predicate_type != "aggregate_comparison":
                continue
            normalized.having.append(FilterSpec(
                table=binding.get("table_name"),
                column=binding.get("column_name"),
                operator=binding.get("operator") or atom.operator or "=",
                value=binding.get("value"),
                source_atom_ids=[atom_id],
                scope="cohort",
                aggregation=binding.get("aggregation") or "sum",
            ))
            changes.append(f"{atom_id} 已按聚合比较语义补充 HAVING")
            continue
        target = target_items[target_index]
        updates = {
            "table": binding.get("table_name") or target.table,
            "column": binding.get("column_name") or target.column,
            "operator": binding.get("operator") or target.operator,
            "value": binding.get("value"),
            "scope": binding.get("scope") if binding.get("scope") in {"row", "cohort", "measure"} else target.scope,
            "aggregation": binding.get("aggregation") or target.aggregation,
        }
        if any(getattr(target, key) != value for key, value in updates.items()):
            target_items[target_index] = target.model_copy(update=updates)
            changes.append(f"{atom_id} 已采用确认的 Schema 字段和值绑定")

    for atom in iter_semantic_atoms(semantic_graph.predicate):
        if atom.predicate_type != "exists":
            continue
        implemented = _implemented_atom_ids(normalized)
        if atom.atom_id in implemented:
            continue
        descendants = _descendant_atom_ids(atom)
        if not descendants or not descendants <= implemented:
            continue

        descendant_tables = {
            item.table
            for item in normalized.filters
            if item.table and descendants & set(item.source_atom_ids)
        }
        target_join = next((
            join for join in normalized.join_logic
            if descendant_tables & {join.left_table, join.right_table}
        ), None)
        if target_join is not None:
            target_join.source_atom_ids = list(dict.fromkeys([
                *target_join.source_atom_ids, atom.atom_id,
            ]))
            changes.append(f"{atom.atom_id} 自动绑定到关联操作")
            continue

        # A single-table record filter also proves positive existence: returned
        # records necessarily satisfy every descendant condition.
        target_filter = next((
            item for item in normalized.filters
            if descendants & set(item.source_atom_ids)
        ), None)
        if target_filter is not None and len(normalized.target_tables) == 1:
            target_filter.source_atom_ids = list(dict.fromkeys([
                *target_filter.source_atom_ids, atom.atom_id,
            ]))
            changes.append(f"{atom.atom_id} 自动绑定到同表记录过滤")

    normalized.covered_atom_ids = sorted(_implemented_atom_ids(normalized))

    output_bindings = output_bindings or {}
    for output in semantic_graph.outputs:
        if not output.required:
            continue
        binding = output_bindings.get(output.id)
        if not binding:
            continue
        for expected in output_binding_fields(binding):
            target = next((
                field for field in normalized.output_fields
                if field.table == expected.get("table_name")
                and field.column == expected.get("column_name")
            ), None)
            if target is None or output.id in target.source_output_ids:
                continue
            target.source_output_ids = list(dict.fromkeys([
                *target.source_output_ids, output.id,
            ]))
            if binding.get("binding_mode") == "expanded" and not target.alias:
                target.alias = str(expected.get("label") or "") or None
            changes.append(
                f"{output.id} 自动绑定到返回字段 "
                f"{expected.get('table_name')}.{expected.get('column_name')}"
            )

    # The semantic output contract is authoritative. Build final projections
    # from grounded fields so one physical column may safely implement several
    # different expressions (for example SUM(LOAN_AMT) and AVG(LOAN_AMT)).
    required_outputs = [output for output in semantic_graph.outputs if output.required]
    if required_outputs and all(output.id in output_bindings for output in required_outputs):
        # Keep structural identifiers that make each result row intelligible.
        # The semantic output contract controls business projections, but must
        # not erase record/entity keys declared by the plan.
        structural_refs = set(normalized.output_grain.keys) | set(normalized.group_by)
        for join in normalized.join_logic:
            structural_refs.add(f"{join.left_table}.{join.left_column}")
            structural_refs.add(f"{join.right_table}.{join.right_column}")
        structural_fields = [
            field.model_copy(deep=True)
            for field in normalized.output_fields
            if field.table and field.column and not field.aggregation
            and (
                f"{field.table}.{field.column}" in structural_refs
                or field.column in structural_refs
            )
        ]
        contract_fields: list[OutputFieldSpec] = []
        for output in required_outputs:
            binding = output_bindings[output.id]
            expected_fields = output_binding_fields(binding)
            for expected in expected_fields:
                alias = (
                    str(expected.get("label") or output.concept)
                    if binding.get("binding_mode") == "expanded"
                    else output.concept
                )
                contract_fields.append(OutputFieldSpec(
                    concept=output.concept,
                    table=expected.get("table_name"),
                    column=expected.get("column_name"),
                    alias=alias,
                    aggregation=output.aggregation or binding.get("aggregation"),
                    source_output_ids=[output.id],
                ))
        existing_refs = {
            (field.table, field.column, field.aggregation)
            for field in contract_fields
        }
        contract_fields.extend(
            field for field in structural_fields
            if (field.table, field.column, field.aggregation) not in existing_refs
        )
        if [item.model_dump() for item in normalized.output_fields] != [
            item.model_dump() for item in contract_fields
        ]:
            normalized.output_fields = contract_fields
            changes.append("按用户语义输出契约重建返回字段")

        def normalized_concept(value: str | None) -> str:
            return "".join(str(value or "").split()).replace("维度", "")

        group_fields: list[str] = []
        for concept in semantic_graph.group_by:
            output = next((
                item for item in required_outputs
                if normalized_concept(item.grounding_concept or item.concept)
                == normalized_concept(concept)
            ), None)
            if output is None:
                continue
            for expected in output_binding_fields(output_bindings[output.id]):
                group_fields.append(f"{expected.get('table_name')}.{expected.get('column_name')}")
        if group_fields:
            normalized.group_by = list(dict.fromkeys(group_fields))
            normalized.output_grain.level = "aggregate"
            normalized.output_grain.entity = semantic_graph.group_by[0]
            normalized.output_grain.keys = list(normalized.group_by)
            normalized.output_grain.description = "按" + "、".join(semantic_graph.group_by) + "分组统计"

        order_specs: list[OrderSpec] = []
        for order in semantic_graph.order_by:
            output = next((
                item for item in required_outputs
                if {
                    normalized_concept(item.concept),
                    normalized_concept(item.grounding_concept),
                } - {""}
                & ({
                    normalized_concept(order.concept),
                    normalized_concept(order.grounding_concept),
                } - {""})
            ), None)
            if output is None:
                continue
            expected = output_binding_fields(output_bindings[output.id])[0]
            order_specs.append(OrderSpec(
                concept=output.concept,
                table=expected.get("table_name"),
                column=expected.get("column_name"),
                direction=order.direction,
                source_output_id=output.id,
                aggregation=output.aggregation,
            ))
        normalized.order_by = order_specs
        normalized.limit = semantic_graph.limit
    normalized.covered_output_ids = sorted(_implemented_output_ids(normalized))
    return normalized, changes
