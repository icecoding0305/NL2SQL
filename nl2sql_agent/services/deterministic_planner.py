"""Build QueryPlan without an LLM when grounded semantics are unambiguous."""

from __future__ import annotations

import re

from nl2sql_agent.services.schema_planner import output_binding_fields
from nl2sql_agent.services.semantic_parser import iter_semantic_atoms
from nl2sql_agent.state import (
    FilterSpec, JoinSpec, NL2SQLState, OrderSpec, OutputFieldSpec, OutputGrain, QueryPlan,
)


def _concept(value: str | None) -> str:
    return re.sub(r"[\s._-]+", "", str(value or "")).lower().replace("维度", "")


def _simple_predicate_supported(state: NL2SQLState) -> bool:
    graph = state.semantic_graph
    if graph is None or graph.predicate is None:
        return True

    def supported(predicate) -> bool:
        if predicate.predicate_type == "and":
            return bool(predicate.children) and all(supported(child) for child in predicate.children)
        return predicate.predicate_type in {
            "comparison", "status", "membership", "text_match", "aggregate_comparison",
        }

    return supported(graph.predicate)


def _trusted_join(state: NL2SQLState, target_tables: list[str]) -> list[JoinSpec] | None:
    if len(target_tables) == 1:
        return []
    if len(target_tables) != 2 or state.schema_plan is None:
        return None
    left, right = target_tables
    matches: list[JoinSpec] = []
    for relation in state.schema_plan.relations:
        source = relation.get("source_table")
        target = relation.get("target_table")
        source_columns = relation.get("source_columns") or []
        target_columns = relation.get("target_columns") or []
        if {source, target} != {left, right} or len(source_columns) != 1 or len(target_columns) != 1:
            continue
        matches.append(JoinSpec(
            left_table=source,
            right_table=target,
            left_column=source_columns[0],
            right_column=target_columns[0],
            join_type="inner",
        ))
    return matches if len(matches) == 1 else None


def build_deterministic_query_plan(
    state: NL2SQLState,
    *,
    min_confidence: float = 0.78,
) -> QueryPlan | None:
    """Return a safe simple plan, or ``None`` to use the plan model."""
    graph = state.semantic_graph
    schema_plan = state.schema_plan
    if graph is None or schema_plan is None or schema_plan.unresolved_slots:
        return None
    if min(state.retrieval_confidence, schema_plan.confidence) < min_confidence:
        return None
    if not _simple_predicate_supported(state):
        return None
    if any(capability in graph.capabilities for capability in {
        "existence", "temporal", "window", "comparison_period", "multi_fact",
    }):
        return None

    required_outputs = [item for item in graph.outputs if item.required]
    if not required_outputs:
        return None
    output_fields: list[OutputFieldSpec] = []
    output_by_id: dict[str, OutputFieldSpec] = {}
    tables: list[str] = []
    binding_confidences: list[float] = []
    for output in required_outputs:
        binding = state.output_bindings.get(output.id)
        fields = output_binding_fields(binding or {})
        if not fields or (binding or {}).get("binding_mode") == "expanded" or len(fields) != 1:
            return None
        physical = fields[0]
        table = physical.get("table_name")
        column = physical.get("column_name")
        confidence = float(physical.get("confidence", (binding or {}).get("confidence", 0.0)))
        if not table or not column or confidence < min_confidence:
            return None
        field = OutputFieldSpec(
            concept=output.concept,
            table=table,
            column=column,
            alias=physical.get("label") or output.concept,
            aggregation=output.aggregation,
            source_output_ids=[output.id],
        )
        output_fields.append(field)
        output_by_id[output.id] = field
        tables.append(table)
        binding_confidences.append(confidence)

    filters: list[FilterSpec] = []
    having: list[FilterSpec] = []
    atoms = list(iter_semantic_atoms(graph.predicate))
    for atom in atoms:
        binding = state.semantic_bindings.get(atom.atom_id)
        if not binding:
            return None
        confidence = float(binding.get("confidence", 0.0))
        table = binding.get("table_name")
        column = binding.get("column_name")
        operator = binding.get("operator")
        if not table or not column or not operator or confidence < min_confidence:
            return None
        spec = FilterSpec(
            table=table,
            column=column,
            operator=operator,
            value=binding.get("value"),
            source_atom_ids=[atom.atom_id],
            scope="measure" if atom.predicate_type == "aggregate_comparison" else "row",
            aggregation=(
                binding.get("aggregation") if atom.predicate_type == "aggregate_comparison"
                else None
            ),
        )
        (having if atom.predicate_type == "aggregate_comparison" else filters).append(spec)
        tables.append(table)
        binding_confidences.append(confidence)

    aggregate_outputs = [field for field in output_fields if field.aggregation]
    detail_outputs = [field for field in output_fields if not field.aggregation]
    group_by = list(dict.fromkeys(
        f"{field.table}.{field.column}" for field in detail_outputs
    )) if aggregate_outputs and detail_outputs else []
    if graph.group_by:
        for group in graph.group_by:
            matching = next((
                (output, output_by_id[output.id]) for output in required_outputs
                if output.id in output_by_id and _concept(group) in {
                    _concept(output.concept), _concept(output.grounding_concept),
                }
            ), None)
            if matching is None:
                return None
            field = matching[1]
            ref = f"{field.table}.{field.column}"
            if ref not in group_by:
                group_by.append(ref)

    order_by: list[OrderSpec] = []
    for order in graph.order_by:
        order_keys = {_concept(order.concept), _concept(order.grounding_concept)} - {""}
        matching = next((
            output for output in required_outputs
            if order_keys & ({
                _concept(output.concept), _concept(output.grounding_concept),
            } - {""})
        ), None)
        if matching is None:
            return None
        field = output_by_id[matching.id]
        order_by.append(OrderSpec(
            concept=matching.concept,
            table=field.table,
            column=field.column,
            direction=order.direction,
            source_output_id=matching.id,
            aggregation=matching.aggregation,
        ))

    planned_tables = [
        item.table_name for item in [
            *schema_plan.anchor_tables, *schema_plan.bridge_tables, *schema_plan.dimension_tables,
        ]
    ]
    target_tables = list(dict.fromkeys([*planned_tables, *tables]))
    if not target_tables or len(target_tables) > 2:
        return None
    joins = _trusted_join(state, target_tables)
    if joins is None:
        return None

    if aggregate_outputs:
        grain = OutputGrain(
            level="aggregate" if group_by else "global",
            entity=graph.group_by[0] if graph.group_by else None,
            keys=list(group_by),
            description=(f"按{'、'.join(graph.group_by)}分组统计" if graph.group_by else "全局汇总"),
        )
    else:
        grain = OutputGrain(level="record", description="业务明细记录")

    confidence = min([
        state.retrieval_confidence, schema_plan.confidence, *binding_confidences,
    ])
    return QueryPlan(
        target_tables=target_tables,
        join_logic=joins,
        filters=filters,
        having=having,
        group_by=group_by,
        order_by=order_by,
        limit=graph.limit,
        output_fields=output_fields,
        output_grain=grain,
        covered_atom_ids=sorted(atom.atom_id for atom in atoms),
        covered_output_ids=sorted(output.id for output in required_outputs),
        confidence=confidence,
    )
