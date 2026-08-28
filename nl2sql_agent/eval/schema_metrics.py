"""M-Schema 改造效果指标的纯函数计算器。

线上/离线任务只需把每条样本的期望与预测写成 dict，即可比较 legacy/xiyan 两套快照。
"""

from __future__ import annotations


def _canonical(value):
    if isinstance(value, (list, tuple)):
        # Join direction is irrelevant for a two-ended relation.
        return tuple(sorted(str(item).strip().casefold() for item in value))
    return str(value).strip().casefold()


def _recall(expected: set, predicted: list | set) -> float:
    if not expected:
        return 1.0
    expected_normalized = {_canonical(item) for item in expected}
    predicted_normalized = {_canonical(item) for item in predicted}
    return len(expected_normalized & predicted_normalized) / len(expected_normalized)


def evaluate_schema_cases(cases: list[dict], table_k: int = 5) -> dict:
    if not cases:
        return {
            "case_count": 0,
            "table_labeled_case_count": 0,
            "column_labeled_case_count": 0,
            "join_labeled_case_count": 0,
            "forbidden_table_labeled_case_count": 0,
            "table_recall_at_k": 0.0,
            "column_recall": 0.0,
            "forbidden_table_rate": 0.0,
            "join_path_accuracy": 0.0,
            "schema_plan_exact_match": 0.0,
            "clarification_accuracy": 0.0,
            "sql_execution_accuracy": 0.0,
            "clarification_rate": 0.0,
            "human_modification_rate": 0.0,
            "avg_profile_seconds_per_table": 0.0,
            "avg_llm_cost_per_table": 0.0,
        }
    table_recalls = []
    column_recalls = []
    join_scores = []
    forbidden_scores = []
    plan_exact_scores = []
    clarification_scores = []
    for case in cases:
        if case.get("expected_tables"):
            table_recalls.append(
                _recall(set(case["expected_tables"]), case.get("predicted_tables", [])[:table_k])
            )
        if case.get("expected_columns"):
            column_recalls.append(
                _recall(set(case["expected_columns"]), case.get("predicted_columns", []))
            )
        if case.get("expected_joins"):
            join_scores.append(
                _recall(
                    {tuple(item) for item in case["expected_joins"]},
                    {tuple(item) for item in case.get("predicted_joins", [])},
                )
            )
        forbidden = {_canonical(item) for item in case.get("forbidden_tables", [])}
        predicted = {
            _canonical(item) for item in case.get("predicted_tables", [])[:table_k]
        }
        if forbidden:
            forbidden_scores.append(bool(forbidden & predicted))
        if case.get("plan_labeled"):
            plan_exact_scores.append(bool(case.get("schema_plan_exact")))
        if "expected_clarification" in case:
            clarification_scores.append(
                bool(case.get("clarified")) == bool(case.get("expected_clarification"))
            )
    count = len(cases)
    avg = lambda values: round(sum(values) / count, 6)  # noqa: E731
    labeled_avg = lambda values: round(sum(values) / len(values), 6) if values else 0.0  # noqa: E731
    return {
        "case_count": count,
        "table_labeled_case_count": sum(bool(case.get("expected_tables")) for case in cases),
        "column_labeled_case_count": len(column_recalls),
        "join_labeled_case_count": len(join_scores),
        "forbidden_table_labeled_case_count": len(forbidden_scores),
        "table_recall_at_k": labeled_avg(table_recalls),
        "column_recall": labeled_avg(column_recalls),
        "forbidden_table_rate": labeled_avg(forbidden_scores),
        "join_path_accuracy": labeled_avg(join_scores),
        "schema_plan_exact_match": labeled_avg(plan_exact_scores),
        "clarification_accuracy": labeled_avg(clarification_scores),
        "sql_execution_accuracy": avg([bool(case.get("execution_correct")) for case in cases]),
        "clarification_rate": avg([bool(case.get("clarified")) for case in cases]),
        "human_modification_rate": avg([bool(case.get("human_modified")) for case in cases]),
        "avg_profile_seconds_per_table": avg([
            float(case.get("profile_seconds_per_table", 0.0)) for case in cases
        ]),
        "avg_llm_cost_per_table": avg([
            float(case.get("llm_cost_per_table", 0.0)) for case in cases
        ]),
    }
