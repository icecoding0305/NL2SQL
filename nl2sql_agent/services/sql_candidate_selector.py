"""Local, deterministic pre-selection for model-generated SQL candidates."""

from __future__ import annotations

import hashlib

import sqlglot
from sqlglot import exp

from nl2sql_agent.services.llm import SQLResult
from nl2sql_agent.state import NL2SQLState


DANGEROUS = (exp.Delete, exp.Update, exp.Insert, exp.Drop, exp.Alter, exp.Create, exp.TruncateTable)


def _shape_score(expression: exp.Expression, state: NL2SQLState) -> tuple[float, list[str]]:
    plan = state.query_plan
    if plan is None:
        return 0.0, []
    selects = list(expression.find_all(exp.Select))
    root = expression if isinstance(expression, exp.Select) else (selects[0] if selects else None)
    if root is None:
        return -0.4, ["缺少 SELECT"]
    checks = [
        (bool(plan.filters), any(item.args.get("where") is not None for item in selects), "WHERE"),
        (bool(plan.having), any(item.args.get("having") is not None for item in selects), "HAVING"),
        (bool(plan.group_by), any(item.args.get("group") is not None for item in selects), "GROUP BY"),
        (bool(plan.order_by), root.args.get("order") is not None, "ORDER BY"),
        (plan.limit is not None, root.args.get("limit") is not None, "LIMIT"),
    ]
    mismatches = [label for expected, actual, label in checks if expected != actual]
    return 0.3 - 0.12 * len(mismatches), [f"结构不匹配:{item}" for item in mismatches]


def score_sql_candidate(
    result: SQLResult,
    state: NL2SQLState,
    dialect: str,
) -> tuple[float, list[str]]:
    errors: list[str] = []
    try:
        expression = sqlglot.parse_one(result.sql, read=dialect)
    except Exception as exc:  # noqa: BLE001
        return 0.0, [f"SQL 解析失败:{exc}"]
    if isinstance(expression, DANGEROUS) or any(expression.find(kind) for kind in DANGEROUS):
        return 0.0, ["包含非只读语句"]

    score = 0.3
    available = {item.table_name.lower() for item in state.retrieved_schema}
    referenced = {table.name.lower() for table in expression.find_all(exp.Table) if table.name}
    unknown = referenced - available
    if unknown:
        errors.append("计划外表:" + ",".join(sorted(unknown)))
        score -= 0.3
    else:
        score += 0.2

    if state.query_plan:
        targets = {item.lower() for item in state.query_plan.target_tables}
        missing = targets - referenced
        if missing:
            errors.append("缺少目标表:" + ",".join(sorted(missing)))
            score -= 0.2
        else:
            score += 0.2
    shape_score, shape_errors = _shape_score(expression, state)
    score += shape_score
    errors.extend(shape_errors)
    return max(0.0, min(1.0, round(score, 4))), errors


def rank_sql_candidates(
    results: list[SQLResult], state: NL2SQLState, dialect: str
) -> list[tuple[SQLResult, float, list[str]]]:
    """Deduplicate candidates, score them, and return best-first."""
    ranked: list[tuple[SQLResult, float, list[str]]] = []
    seen: set[str] = set()
    for result in results:
        normalized = " ".join((result.sql or "").split()).lower()
        fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if not normalized or fingerprint in seen:
            continue
        seen.add(fingerprint)
        score, errors = score_sql_candidate(result, state, dialect)
        ranked.append((result, score, errors))
    return sorted(ranked, key=lambda item: (-item[1], len(item[2])))
