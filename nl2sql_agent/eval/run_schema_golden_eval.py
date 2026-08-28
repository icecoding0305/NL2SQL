"""Run the production Schema-retrieval node against a deterministic golden set.

This deliberately stops before SQL generation and never connects to a business
database. Golden cases may provide a frozen ``query_intent`` so changes in the
LLM query-understanding step do not hide Schema-grounding regressions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from nl2sql_agent.eval.schema_metrics import evaluate_schema_cases
from nl2sql_agent.nodes.m3_schema_retrieval import make_schema_retrieval_node
from nl2sql_agent.services.deps import build_deps
from nl2sql_agent.services.executor import InMemoryExecutor
from nl2sql_agent.services.schema_planner import parse_query_intent, plan_table_names
from nl2sql_agent.state import NL2SQLState, QueryIntent


DEFAULT_CASES = Path(__file__).with_name("schema_golden_set.yaml")


def _norm(value: str) -> str:
    return str(value or "").strip().casefold()


def _columns(output: dict[str, Any]) -> list[str]:
    values: set[str] = set()
    for candidate in output.get("field_candidates") or []:
        values.add(f"{candidate.table_name}.{candidate.column_name}")
    for evidence in output.get("retrieval_evidence") or []:
        table = evidence.get("table_name")
        for column in evidence.get("column_names") or []:
            if table and column:
                values.add(f"{table}.{column}")
    for binding_group in ("semantic_bindings", "output_bindings"):
        for binding in (output.get(binding_group) or {}).values():
            if isinstance(binding, dict):
                table = binding.get("table_name") or binding.get("table")
                column = binding.get("column_name") or binding.get("column")
                if table and column:
                    values.add(f"{table}.{column}")
    return sorted(values, key=_norm)


def _relation_pairs(relation: dict[str, Any]) -> list[list[str]]:
    left_table = relation.get("left_table") or relation.get("from_table") or relation.get("source_table")
    right_table = relation.get("right_table") or relation.get("to_table") or relation.get("target_table")
    left_columns = relation.get("source_columns") or [relation.get("left_column") or relation.get("from_column")]
    right_columns = relation.get("target_columns") or [relation.get("right_column") or relation.get("to_column")]
    if not left_table or not right_table:
        return []
    return [
        [f"{left_table}.{left}", f"{right_table}.{right}"]
        for left, right in zip(left_columns, right_columns)
        if left and right
    ]


def _joins(output: dict[str, Any]) -> list[list[str]]:
    plan = output.get("schema_plan")
    relations = plan.relations if plan is not None else []
    return [pair for relation in relations for pair in _relation_pairs(relation)]


def _metrics_by_suite(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    suites = sorted({str(row.get("suite") or "unclassified") for row in rows})
    return {
        suite: evaluate_schema_cases(
            [row for row in rows if str(row.get("suite") or "unclassified") == suite]
        )
        for suite in suites
    }


def _validate_labels(cases: list[dict[str, Any]], catalog) -> None:
    """Fail fast when a golden label drifts away from the effective M-Schema."""
    errors: list[str] = []
    for case in cases:
        case_id = str(case.get("id") or "<missing-id>")
        tables = {
            hit.name.casefold(): hit
            for hit in catalog.tables_for_scope(case.get("data_scope") or ["risk_mart"])
        }
        for table_name in [
            *(case.get("expected_tables") or []),
            *(case.get("expected_plan_tables") or []),
            *(case.get("forbidden_tables") or []),
        ]:
            if str(table_name).casefold() not in tables:
                errors.append(f"{case_id}: unknown table {table_name}")
        for field_name in case.get("expected_columns") or []:
            table_name, separator, column_name = str(field_name).partition(".")
            hit = tables.get(table_name.casefold())
            known_columns = {
                str(column.get("name") or "").casefold()
                for column in (hit.columns if hit else [])
            }
            if not separator or hit is None or column_name.casefold() not in known_columns:
                errors.append(f"{case_id}: unknown column {field_name}")
        for relation in case.get("expected_joins") or []:
            if not isinstance(relation, list) or len(relation) != 2:
                errors.append(f"{case_id}: invalid join label {relation!r}")
                continue
            for field_name in relation:
                table_name, separator, column_name = str(field_name).partition(".")
                hit = tables.get(table_name.casefold())
                known_columns = {
                    str(column.get("name") or "").casefold()
                    for column in (hit.columns if hit else [])
                }
                if not separator or hit is None or column_name.casefold() not in known_columns:
                    errors.append(f"{case_id}: unknown join endpoint {field_name}")
    if errors:
        raise RuntimeError("Invalid golden labels:\n- " + "\n- ".join(errors))


def evaluate_case(node, case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    query = str(case["question"])
    frozen = case.get("query_intent")
    intent = QueryIntent.model_validate(frozen) if frozen else parse_query_intent(query)
    state = NL2SQLState(
        user_query=query,
        user_id="schema-golden-eval",
        data_scope=list(case.get("data_scope") or ["risk_mart"]),
        query_intent=intent,
    )
    output = node(state)
    output = output.model_dump(mode="json") if isinstance(output, NL2SQLState) else output
    predicted_tables = [item.table_name for item in output.get("retrieved_schema") or []]
    predicted_columns = _columns(output)
    predicted_joins = _joins(output)
    expected_tables = list(case.get("expected_tables") or [])
    plan = output.get("schema_plan")
    planned_tables = plan_table_names(plan) if plan is not None else predicted_tables
    plan_labeled = bool(case.get("expected_plan_tables"))
    expected_plan_tables = case.get("expected_plan_tables") or expected_tables
    schema_plan_exact = {_norm(x) for x in planned_tables} == {
        _norm(x) for x in expected_plan_tables
    }
    clarified = bool(
        output.get("business_clarification")
        or output.get("field_ambiguities")
        or output.get("unsupported_outputs")
    )
    metric_row = {
        **case,
        "predicted_tables": predicted_tables,
        "predicted_columns": predicted_columns,
        "predicted_joins": predicted_joins,
        "plan_labeled": plan_labeled,
        "schema_plan_exact": schema_plan_exact,
        "clarified": clarified,
    }
    detail = {
        "id": case.get("id"),
        "question": query,
        "predicted_tables": predicted_tables,
        "predicted_columns": predicted_columns,
        "predicted_joins": predicted_joins,
        "planned_tables": planned_tables,
        "schema_plan_exact": schema_plan_exact if plan_labeled else None,
        "clarified": clarified,
        "retrieval_confidence": output.get("retrieval_confidence", 0.0),
        "unresolved_slots": list(plan.unresolved_slots) if plan is not None else [],
        "retrieval_evidence": output.get("retrieval_evidence") or [],
    }
    return metric_row, detail


def run_golden_evaluation(
    cases_path: str | Path = DEFAULT_CASES,
    *,
    m_schema_path: str | Path | None = None,
) -> dict[str, Any]:
    cases_path = Path(cases_path).resolve()
    payload = yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}
    cases = payload.get("cases") or []
    if not cases:
        raise RuntimeError(f"Golden set is empty: {cases_path}")

    configured_mschema = payload.get("m_schema_path")
    dataset_mschema = None
    if configured_mschema:
        dataset_mschema = (cases_path.parent / str(configured_mschema)).resolve()
    deps = build_deps(
        m_schema_path=m_schema_path or dataset_mschema,
        executor=InMemoryExecutor(),
    )
    _validate_labels(cases, deps.catalog)
    node = make_schema_retrieval_node(deps)
    evaluated: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for case in cases:
        metric_row, detail = evaluate_case(node, case)
        evaluated.append(metric_row)
        details.append(detail)
        expected = {_norm(x) for x in case.get("expected_tables") or []}
        predicted = {_norm(x) for x in detail["predicted_tables"]}
        expected_columns = {_norm(x) for x in case.get("expected_columns") or []}
        predicted_columns = {_norm(x) for x in detail["predicted_columns"]}
        expected_joins = {
            tuple(sorted(_norm(endpoint) for endpoint in relation))
            for relation in case.get("expected_joins") or []
        }
        predicted_joins = {
            tuple(sorted(_norm(endpoint) for endpoint in relation))
            for relation in detail["predicted_joins"]
        }
        passed = (
            expected <= predicted
            and expected_columns <= predicted_columns
            and expected_joins <= predicted_joins
            and (not case.get("expected_plan_tables") or detail["schema_plan_exact"])
            and (
                "expected_clarification" not in case
                or bool(case["expected_clarification"]) == detail["clarified"]
            )
        )
        detail.update({
            "suite": case.get("suite"),
            "tags": case.get("tags") or [],
            "passed": passed,
            "expected_tables": case.get("expected_tables") or [],
            "expected_columns": case.get("expected_columns") or [],
            "expected_joins": case.get("expected_joins") or [],
            "expected_clarification": case.get("expected_clarification"),
        })

    metrics = evaluate_schema_cases(evaluated)
    return {
        "dataset_version": payload.get("version", 1),
        "description": payload.get("description", ""),
        "coverage": payload.get("coverage") or {},
        "metrics": metrics,
        "metrics_by_suite": _metrics_by_suite(evaluated),
        "cases": details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Production Schema path golden-set evaluation")
    parser.add_argument("--cases", default=str(DEFAULT_CASES))
    parser.add_argument("--m-schema")
    parser.add_argument("--json-output")
    parser.add_argument("--min-table-recall", type=float, default=1.0)
    parser.add_argument("--min-column-recall", type=float, default=0.0)
    args = parser.parse_args()

    report = run_golden_evaluation(args.cases, m_schema_path=args.m_schema)
    for case in report["cases"]:
        mark = "PASS" if case["passed"] else "MISS"
        print(f"[{mark}] {case['id']}: {case['question']}")
    metrics = report["metrics"]
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    passed = (
        metrics["table_recall_at_k"] >= args.min_table_recall
        and metrics["column_recall"] >= args.min_column_recall
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
