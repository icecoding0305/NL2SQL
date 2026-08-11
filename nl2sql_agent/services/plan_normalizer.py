"""Deterministic normalization for structural semantic atoms in QueryPlan."""

from __future__ import annotations

from nl2sql_agent.services.semantic_parser import iter_semantic_atoms
from nl2sql_agent.services.schema_planner import output_binding_fields
from nl2sql_agent.state import QueryPlan, SemanticPredicate


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
    plan: QueryPlan, semantic_graph, output_bindings: dict[str, dict] | None = None
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
    normalized.covered_output_ids = sorted(_implemented_output_ids(normalized))
    return normalized, changes
