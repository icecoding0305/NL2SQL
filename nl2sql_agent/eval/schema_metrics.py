"""M-Schema 改造效果指标的纯函数计算器。

线上/离线任务只需把每条样本的期望与预测写成 dict，即可比较 legacy/xiyan 两套快照。
"""

from __future__ import annotations


def _recall(expected: set, predicted: list | set) -> float:
    if not expected:
        return 1.0
    return len(expected & set(predicted)) / len(expected)


def evaluate_schema_cases(cases: list[dict], table_k: int = 5) -> dict:
    if not cases:
        return {
            "case_count": 0,
            "table_recall_at_k": 0.0,
            "column_recall": 0.0,
            "join_path_accuracy": 0.0,
            "sql_execution_accuracy": 0.0,
            "clarification_rate": 0.0,
            "human_modification_rate": 0.0,
            "avg_profile_seconds_per_table": 0.0,
            "avg_llm_cost_per_table": 0.0,
        }
    table_recalls = []
    column_recalls = []
    join_scores = []
    for case in cases:
        table_recalls.append(
            _recall(set(case.get("expected_tables", [])), case.get("predicted_tables", [])[:table_k])
        )
        column_recalls.append(
            _recall(set(case.get("expected_columns", [])), case.get("predicted_columns", []))
        )
        join_scores.append(
            _recall(
                {tuple(item) for item in case.get("expected_joins", [])},
                {tuple(item) for item in case.get("predicted_joins", [])},
            )
        )
    count = len(cases)
    avg = lambda values: round(sum(values) / count, 6)  # noqa: E731
    return {
        "case_count": count,
        "table_recall_at_k": avg(table_recalls),
        "column_recall": avg(column_recalls),
        "join_path_accuracy": avg(join_scores),
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
