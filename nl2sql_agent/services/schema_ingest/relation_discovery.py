"""Conservative relationship discovery from schema keys and safe field profiles."""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher
import re

from nl2sql_agent.services.schema_ingest.mysql_fetcher import (
    ColumnMeta,
    IndexMeta,
    TableMeta,
)


IGNORED_COLUMNS = {
    "created_at", "updated_at", "create_time", "update_time", "etl_time",
    "status", "state", "type", "platform_code", "tenant_id", "version", "app_no",
}
JOIN_SUFFIXES = ("_id", "_no", "_code", "_key", "编号", "编码")
NUMERIC_TYPES = {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric"}
TEXT_TYPES = {"char", "varchar", "text"}


def _base_type(column: ColumnMeta) -> str:
    return (column.type or column.raw_type).lower().split("(", 1)[0]


def _compatible(left: ColumnMeta, right: ColumnMeta) -> bool:
    left_type, right_type = _base_type(left), _base_type(right)
    return (
        left_type == right_type
        or left_type in NUMERIC_TYPES and right_type in NUMERIC_TYPES
        or left_type in TEXT_TYPES and right_type in TEXT_TYPES
    )


def _is_key(column: ColumnMeta) -> bool:
    return bool(column.primary_key or column.unique)


def _comment_similarity(left: ColumnMeta, right: ColumnMeta) -> float | None:
    def normalize(value: str) -> str:
        return "".join(re.findall(r"[0-9a-zA-Z\u4e00-\u9fff]+", value.lower()))

    left_comment = normalize(left.comment)
    right_comment = normalize(right.comment)
    if not left_comment or not right_comment:
        return None
    return SequenceMatcher(None, left_comment, right_comment).ratio()


def _sample_overlap(left: ColumnMeta, right: ColumnMeta) -> float | None:
    left_values = {str(value) for value in left.profile.get("examples", []) if value is not None}
    right_values = {str(value) for value in right.profile.get("examples", []) if value is not None}
    if not left_values or not right_values:
        return None
    return len(left_values & right_values) / max(1, min(len(left_values), len(right_values)))


def _profile_uniqueness(column: ColumnMeta) -> float | None:
    non_null = int(column.profile.get("non_null_count") or 0)
    distinct = int(column.profile.get("approx_distinct") or 0)
    if non_null > 0:
        return min(1.0, distinct / non_null)
    values = [value for value in column.profile.get("examples", []) if value is not None]
    if len(values) >= 3:
        return len({str(value) for value in values}) / len(values)
    return None


def _entity_token(column_name: str) -> str:
    normalized = column_name.lower()
    for suffix in JOIN_SUFFIXES:
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)].strip("_")
    return normalized


def _warehouse_anchor_score(table: TableMeta, column: ColumnMeta) -> float:
    """Estimate whether a column is the canonical entity side without DB constraints."""
    if _is_key(column):
        return 1.0
    name = table.table_name.lower()
    token = _entity_token(column.name)
    uniqueness = _profile_uniqueness(column) or 0.0
    score = 0.40 * uniqueness
    if column.indexed:
        score += 0.18
    if token and token in name:
        score += 0.18
    if any(marker in name for marker in ("_info", "_base", "_master", "_dim")):
        score += 0.10
    if any(marker in name for marker in ("dwd_ip_", "dwd_ar_", "dwd_prd_")):
        score += 0.08
    if any(marker in name for marker in ("_detail", "_his", "dwd_ev_", "_flow")):
        score -= 0.10
    return max(0.0, min(1.0, score))


def _table_entity_subtype(table: TableMeta) -> str:
    text = f"{table.table_name} {table.table_comment}".lower()
    if any(token in text for token in ("indv", "personal", "个人", "自然人")):
        return "individual"
    if any(token in text for token in ("corp", "comp", "enterprise", "企业", "公司")):
        return "corporate"
    return ""


def _group_anchor_score(
    candidate: tuple[TableMeta, ColumnMeta],
    matches: list[tuple[TableMeta, ColumnMeta]],
) -> float:
    table, column = candidate
    score = _warehouse_anchor_score(table, column)
    subtype = _table_entity_subtype(table)
    if subtype:
        support = sum(
            1 for other_table, _ in matches
            if other_table.table_name != table.table_name
            and _table_entity_subtype(other_table) == subtype
        )
        score += min(0.15, support * 0.04)
    return score


def _looks_temporal_identifier(column: ColumnMeta) -> bool:
    text = f"{column.name} {column.comment}".lower()
    return any(token in text for token in ("month", "date", "time", "月份", "日期", "时间"))


def _canonical_edge(relation: dict) -> tuple:
    left = (str(relation.get("source_table") or ""), tuple(relation.get("source_columns") or []))
    right = (str(relation.get("target_table") or ""), tuple(relation.get("target_columns") or []))
    return tuple(sorted((left, right)))


def tables_from_mschema(
    schema: dict, comment_overrides: dict | None = None
) -> list[TableMeta]:
    """Restore discovery metadata from the latest persisted M-Schema.

    This makes relationship discovery independently repeatable after Schema
    synchronization.  Pending comment overrides are applied as well so a user
    can correct metadata before explicitly starting discovery.
    """
    overrides = comment_overrides or {}
    tables: list[TableMeta] = []
    for table_name, table in (schema.get("tables") or {}).items():
        columns = []
        for position, (column_name, field) in enumerate(
            (table.get("fields") or {}).items(), start=1
        ):
            profile = dict(field.get("profile") or {})
            if not profile.get("examples") and field.get("examples"):
                profile["examples"] = list(field.get("examples") or [])
            columns.append(ColumnMeta(
                name=column_name,
                type=str(field.get("type") or ""),
                raw_type=str(field.get("raw_type") or field.get("type") or ""),
                comment=str(
                    overrides.get((table_name, column_name))
                    or field.get("comment")
                    or ""
                ),
                sensitive=bool(field.get("sensitive")),
                nullable=bool(field.get("nullable", True)),
                default=field.get("default"),
                primary_key=bool(field.get("primary_key")),
                unique=bool(field.get("unique")),
                indexed=bool(field.get("indexed")),
                ordinal_position=position,
                category=str(field.get("category") or ""),
                semantic_role=str(field.get("dim_or_meas") or ""),
                time_granularity=field.get("time_granularity"),
                profile=profile,
            ))
        tables.append(TableMeta(
            table_name=table_name,
            table_comment=str(
                overrides.get((table_name, None)) or table.get("comment") or ""
            ),
            columns=columns,
            row_count_estimate=int(table.get("row_count_estimate") or 0),
            schema_name=str(schema.get("schema") or ""),
            primary_keys=list(table.get("primary_keys") or []),
            unique_keys=[list(item) for item in (table.get("unique_keys") or [])],
            indexes=[IndexMeta(
                name=str(item.get("name") or ""),
                columns=list(item.get("columns") or []),
                unique=bool(item.get("unique")),
            ) for item in (table.get("indexes") or [])],
            preliminary_description=str(table.get("preliminary_description") or ""),
            description_confidence=float(table.get("description_confidence") or 0),
        ))
    return tables


def discover_relation_candidates(
    tables: list[TableMeta],
    existing_relations: list[dict] | None = None,
    *,
    min_confidence: float = 0.68,
    inferred_confidence: float = 0.84,
    max_candidates: int = 200,
    allow_profile_inference: bool = True,
    warehouse_anchor_threshold: float = 0.62,
    max_edges_per_identifier: int = 25,
) -> list[dict]:
    """Discover auditable one-column candidates; never returns runtime JOIN facts."""
    existing = {_canonical_edge(item) for item in (existing_relations or [])}
    columns_by_name: dict[str, list[tuple[TableMeta, ColumnMeta]]] = defaultdict(list)
    for table in tables:
        for column in table.columns:
            normalized = column.name.strip().lower()
            if (
                normalized in IGNORED_COLUMNS
                or normalized == "id"
                or not normalized.endswith(JOIN_SUFFIXES)
                or _looks_temporal_identifier(column)
            ):
                continue
            columns_by_name[normalized].append((table, column))

    candidates: list[dict] = []
    seen: set[tuple] = set(existing)
    for normalized_name, matches in columns_by_name.items():
        if len(matches) < 2:
            continue
        declared_anchors = [item for item in matches if _is_key(item[1])]
        ranked_anchors = sorted(
            declared_anchors or matches,
            key=lambda item: (-_group_anchor_score(item, matches), item[0].table_name),
        )
        target_table, target_column = ranked_anchors[0]
        target_anchor_score = _warehouse_anchor_score(target_table, target_column)
        profile_inferred = not _is_key(target_column)
        if profile_inferred and (
            not allow_profile_inference
            or target_anchor_score < warehouse_anchor_threshold
        ):
            continue

        edges_for_identifier = 0
        for source_table, source_column in sorted(
            matches, key=lambda item: item[0].table_name
        ):
            if source_table.table_name == target_table.table_name:
                continue
            if not _compatible(source_column, target_column):
                continue
            source_subtype = _table_entity_subtype(source_table)
            target_subtype = _table_entity_subtype(target_table)
            if (
                source_subtype and target_subtype and source_subtype != target_subtype
                and _warehouse_anchor_score(source_table, source_column)
                >= warehouse_anchor_threshold
            ):
                continue
            if edges_for_identifier >= max(0, int(max_edges_per_identifier)):
                break

            candidate = {
                "source_table": source_table.table_name,
                "source_columns": [source_column.name],
                "target_table": target_table.table_name,
                "target_columns": [target_column.name],
            }
            edge = _canonical_edge(candidate)
            if edge in seen:
                continue
            seen.add(edge)

            evidence = [f"字段名一致:{normalized_name}", "字段类型兼容"]
            if profile_inferred:
                target_uniqueness = _profile_uniqueness(target_column)
                evidence.extend([
                    f"画像锚点:{target_table.table_name}.{target_column.name}",
                    f"锚点评分:{target_anchor_score:.2f}",
                ])
                score = 0.47 + 0.28 * target_anchor_score
                if target_column.indexed:
                    score += 0.06
                if source_column.indexed:
                    score += 0.04
            else:
                target_uniqueness = 1.0
                evidence.append(
                    f"唯一键位于:{target_table.table_name}.{target_column.name}"
                )
                score = 0.42 + 0.13 + 0.22
                if source_column.indexed:
                    score += 0.08
                    evidence.append("多端字段已有索引")
            comment_similarity = _comment_similarity(source_column, target_column)
            if comment_similarity is not None:
                score += 0.06 * comment_similarity
                evidence.append(f"字段注释相似度:{comment_similarity:.2f}")
            overlap = _sample_overlap(source_column, target_column)
            if overlap is not None:
                score += (0.10 if profile_inferred else 0.15) * overlap
                evidence.append(f"安全样本覆盖率:{overlap:.2f}")
            score = min(0.89 if profile_inferred else 1.0, round(score, 4))
            if score < min_confidence:
                continue

            source_unique = _is_key(source_column)
            target_unique = _is_key(target_column)
            candidates.append({
                **candidate,
                "cardinality": (
                    "one_to_one" if source_unique and target_unique else "many_to_one"
                ),
                "preferred_join_type": "inner",
                "relation_type": (
                    "profile_inferred_candidate" if profile_inferred
                    else "inferred_candidate"
                ),
                "status": (
                    "candidate" if profile_inferred
                    else "inferred" if score >= inferred_confidence else "candidate"
                ),
                "confidence": score,
                "evidence": evidence,
                "validation_summary": {
                    "discovery_mode": "profile_anchor" if profile_inferred else "declared_key",
                    "name_match": "exact",
                    "type_compatible": True,
                    "comment_similarity": comment_similarity,
                    "sample_overlap": overlap,
                    "target_unique": target_unique,
                    "target_profile_uniqueness": target_uniqueness,
                    "anchor_score": target_anchor_score,
                },
                "source": "schema_relation_discovery",
                "enabled": False,
            })
            edges_for_identifier += 1

    return sorted(
        candidates,
        key=lambda item: (-float(item["confidence"]), item["source_table"], item["target_table"]),
    )[:max(0, int(max_candidates))]
