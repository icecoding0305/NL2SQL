"""基于字段画像校准文本筛选的物理字段、操作符和值。"""

from __future__ import annotations

import re
from typing import Callable, Iterable

from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.state import FieldCandidate


_ADMIN_SUFFIX_RE = re.compile(
    r"(?:特别行政区|壮族自治区|回族自治区|维吾尔自治区|自治区|省|市|区|县)$"
)


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def _column_index(tables: Iterable[TableDef]) -> dict[tuple[str, str], dict]:
    return {
        (table.name, str(column.get("name") or "")): column
        for table in tables
        for column in table.columns
    }


def _examples(column: dict) -> list[str]:
    values = [*list(column.get("examples") or [])]
    values.extend(list((column.get("profile") or {}).get("examples") or []))
    return list(dict.fromkeys(str(item) for item in values if item is not None))


def _match_profile_value(value: str, column: dict) -> tuple[float, str | None, str]:
    wanted = _normalized(value)
    controlled = str(column.get("category") or "") == "enum"
    best = (0.0, None, "")
    for example in _examples(column):
        actual = _normalized(example)
        if actual == wanted:
            current = (1.0, example, "字段样本与筛选值完全一致")
        elif (
            controlled
            and _ADMIN_SUFFIX_RE.sub("", actual) == _ADMIN_SUFFIX_RE.sub("", wanted)
        ):
            current = (0.96, example, "枚举样本与筛选值仅行政区后缀不同")
        elif wanted and wanted in actual:
            current = (
                0.82 if controlled else 0.55,
                example if controlled else None,
                "字段样本包含筛选值",
            )
        else:
            current = (0.0, None, "")
        if current[0] > best[0]:
            best = current
    return best


def ground_text_binding(
    value: object,
    options: list[FieldCandidate],
    tables: Iterable[TableDef],
    value_lookup: Callable[[FieldCandidate, dict, str], list[str]] | None = None,
) -> tuple[FieldCandidate, str, object, list[str]] | None:
    """优先选择值域证据最强的字段，并给出数据库实际值或安全匹配方式。"""
    if not options:
        return None
    if not isinstance(value, str):
        selected = options[0]
        return selected, "=", value, []

    columns = _column_index(tables)
    ranked: list[tuple[float, float, FieldCandidate, str | None, str, dict]] = []
    for candidate in options:
        column = columns.get((candidate.table_name, candidate.column_name), {})
        match_score, matched_value, evidence = _match_profile_value(value, column)
        ranked.append((
            match_score,
            candidate.final_score,
            candidate,
            matched_value,
            evidence,
            column,
        ))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2].table_name, item[2].column_name))

    # M-Schema 仅保存有限样本。低基数、非敏感枚举未命中时，允许调用方
    # 参数化探测与当前筛选值相关的少量真实值，不加载完整值域。
    if ranked[0][0] < 0.8 and value_lookup is not None:
        enriched: list[tuple[float, float, FieldCandidate, str | None, str, dict]] = []
        for match_score, candidate_score, candidate, matched_value, evidence, column in ranked:
            runtime_values: list[str] = []
            if (
                str(column.get("category") or "") == "enum"
                and not column.get("sensitive")
            ):
                runtime_values = value_lookup(candidate, column, value)
            if runtime_values:
                runtime_column = {**column, "examples": [
                    *list(column.get("examples") or []), *runtime_values,
                ]}
                match_score, matched_value, evidence = _match_profile_value(
                    value, runtime_column
                )
                if evidence:
                    evidence = f"实时值域验证：{evidence}"
            enriched.append((
                match_score, candidate_score, candidate, matched_value, evidence, column,
            ))
        ranked = sorted(
            enriched,
            key=lambda item: (-item[0], -item[1], item[2].table_name, item[2].column_name),
        )

    match_score, _, selected, matched_value, evidence, column = ranked[0]

    if match_score >= 0.8 and matched_value is not None:
        operator = "="
        grounded_value: object = matched_value
    elif match_score > 0 and str(column.get("category") or "") == "text":
        operator = "LIKE"
        grounded_value = f"%{value}%"
    else:
        # 没有值域证据时保留用户的等值语义，避免把姓名、编号等条件
        # 不加区分地扩大成模糊匹配。
        operator = "="
        grounded_value = value
    return selected, operator, grounded_value, [evidence] if evidence else []
