"""注释质量判断 + LLM 辅助生成候选注释(样例值先脱敏)。

质量规则来自 config/schema_ingest.yaml:
- 表注释字数 < min_table_comment_length → 视为空
- 字段注释覆盖率 < min_column_comment_coverage → 整张表打回待审核

LLM 生成:从表取样例值(敏感字段打码后)帮助模型理解字段含义,绝不允许把真实
敏感数据传给 LLM。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from nl2sql_agent.services.executor import SQLExecutor
from nl2sql_agent.services.schema_ingest.mysql_fetcher import TableMeta
from nl2sql_agent.services.schema_ingest.profiler import mask_value


def _quality_config(config: dict) -> dict:
    return config.get("quality_check", config)


def _weak_comment(comment: str, min_length: int, generic_comments: set[str]) -> bool:
    normalized = "".join((comment or "").split()).lower()
    return len(normalized) < min_length or normalized in generic_comments


def comment_quality_issues(table: TableMeta, config: dict) -> list[str]:
    """检查完整度和最低语义质量；默认要求每个字段都有有效描述。"""
    cfg = _quality_config(config)
    generic = {"".join(str(x).split()).lower() for x in cfg.get(
        "generic_comments", ["字段", "数据", "相关字段", "业务字段", "暂无"]
    )}
    issues: list[str] = []
    if _weak_comment(table.table_comment, int(cfg.get("min_table_comment_length", 4)), generic):
        issues.append("表描述缺失或过于笼统")
    weak_columns = [
        c.name for c in table.columns
        if _weak_comment(c.comment, int(cfg.get("min_column_comment_length", 2)), generic)
    ]
    coverage = table.comment_coverage
    if coverage < float(cfg.get("min_column_comment_coverage", 0.8)):
        issues.append(f"字段描述覆盖率 {coverage:.2%} 未达标")
    if cfg.get("require_all_columns", True) and weak_columns:
        issues.append("以下字段缺少有效描述:" + ",".join(weak_columns))
    return issues


def has_sufficient_comments(table: TableMeta, config: dict) -> bool:
    """注释质量是否达标(达标 → 直接入库,不经过审核)。"""
    return not comment_quality_issues(table, config)


# ---------------- 脱敏 ----------------

def fetch_masked_sample_values(
    executor: SQLExecutor, table: TableMeta, limit: int = 3, dialect: str = "mysql"
) -> dict[str, list[str]]:
    """取样例值(敏感列打码),返回 {列名: [脱敏样例] }。"""
    if not table.columns:
        return {}
    quote = '"' if dialect.lower() in {"postgres", "postgresql"} else "`"

    def quoted(name: str) -> str:
        return quote + name.replace(quote, quote * 2) + quote

    col_names = ", ".join(quoted(c.name) for c in table.columns)
    table_ref = quoted(table.table_name)
    if table.schema_name and dialect.lower() in {"postgres", "postgresql"}:
        table_ref = f"{quoted(table.schema_name)}.{table_ref}"
    rows = executor.execute(
        f"SELECT {col_names} FROM {table_ref} LIMIT {int(limit)}",
        timeout_seconds=15,
    )
    samples: dict[str, list[str]] = {c.name: [] for c in table.columns}
    for r in rows:
        for c in table.columns:
            v = r.get(c.name)
            if v is None:
                continue
            samples[c.name].append(mask_value(v) if c.sensitive else str(v)[:20])
    return samples


# ---------------- LLM 生成候选注释 ----------------

def _field_evidence(table: TableMeta, sample_values: dict[str, list[str]]) -> str:
    lines = []
    for c in table.columns:
        vals = sample_values.get(c.name) or c.profile.get("examples", [])
        value_text = ", ".join(str(v) for v in vals[:2]) or "(无样例)"
        lines.append(
            f"{c.name}({c.raw_type or c.type}) 原生注释:{c.comment or '(空)'} "
            f"角色:{c.semantic_role or 'unknown'}/{c.category or 'unknown'} "
            f"PK:{c.primary_key} UNIQUE:{c.unique} NULLABLE:{c.nullable} 样例值:{value_text}"
        )
    return "\n".join(lines)


def _bounded_evidence(evidence: str, max_fields: int = 12) -> str:
    lines = evidence.splitlines()
    selected = lines[:max_fields]
    if len(lines) > max_fields:
        selected.append(f"（其余 {len(lines) - max_fields} 个字段未传入本次提示词）")
    return "\n".join(selected)


def generate_database_context(tables: list[TableMeta], llm, prompts=None) -> str:
    summary = [
        {
            "table": table.table_name,
            "comment": table.table_comment,
            "column_count": len(table.columns),
            "key_columns": [
                column.name for column in table.columns
                if column.primary_key or column.unique
            ][:6],
            "foreign_keys": [
                f"{rel.source_table}.{','.join(rel.source_columns)} -> "
                f"{rel.target_table}.{','.join(rel.target_columns)}"
                for rel in table.relations
            ],
        }
        for table in tables
    ]
    schema = {
        "type": "object",
        "required": ["context"],
        "properties": {"context": {"type": "string"}},
    }
    try:
        prompt_text = (
            prompts.render("schema_comment/database_context",
                tables_summary=json.dumps(summary, ensure_ascii=False),
            )
            if prompts else
            "基于以下结构概括数据库业务域、核心实体和表间关系。不得臆造结构之外的事实。"
            "只输出 JSON:{\"context\":\"...\"}\n" + json.dumps(summary, ensure_ascii=False)
        )
        data = llm.complete_json(prompt_text, schema, retries=1)
        return str(data.get("context") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _generate_preliminary_context(
    table: TableMeta, database_context: str, evidence: str, llm, prompts=None
) -> str:
    schema = {
        "type": "object", "required": ["context"],
        "properties": {"context": {"type": "string"}},
    }
    try:
        prompt_text = (
            prompts.render("schema_comment/preliminary_context",
                database_context=database_context or "(未知)",
                table_name=table.table_name,
                evidence=_bounded_evidence(evidence),
            )
            if prompts else
            f"数据库上下文:{database_context or '(未知)'}\n表:{table.table_name}\n{evidence}\n"
            "初步说明该表的数据粒度、核心实体和用途，不得补造字段。"
            "只输出 JSON:{\"context\":\"...\"}"
        )
        data = llm.complete_json(prompt_text, schema, retries=1)
        return str(data.get("context") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _generate_category_context(table: TableMeta, evidence: str, llm, prompts=None) -> str:
    groups: dict[str, list[str]] = defaultdict(list)
    for column in table.columns:
        groups[column.category or "unknown"].append(column.name)
    schema = {
        "type": "object", "required": ["context"],
        "properties": {"context": {"type": "string"}},
    }
    try:
        prompt_text = (
            prompts.render("schema_comment/category_context",
                table_name=table.table_name,
                field_groups=json.dumps(groups, ensure_ascii=False),
                evidence=_bounded_evidence(evidence),
            )
            if prompts else
            f"表:{table.table_name}\n字段分类:{json.dumps(groups, ensure_ascii=False)}\n{evidence}\n"
            "辨析同类字段的差别和联系，尤其是金额、日期、状态和编码字段；不得猜测不存在的关系。"
            "只输出 JSON:{\"context\":\"...\"}"
        )
        data = llm.complete_json(prompt_text, schema, retries=1)
        return str(data.get("context") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def build_comment_generation_prompt(
    table: TableMeta,
    sample_values: dict[str, list[str]],
    database_context: str = "",
    preliminary_context: str = "",
    category_context: str = "",
) -> str:
    evidence = _field_evidence(table, sample_values)
    return (
        f"为下面这张表补全缺失或低质量的注释。表名:{table.table_name}\n"
        f"数据库上下文:{database_context or '(未知)'}\n"
        f"表初步理解:{preliminary_context or '(无)'}\n"
        f"同类字段辨析:{category_context or '(无)'}\n"
        f"现有表注释:{table.table_comment or '(空)'}\n字段证据:\n{evidence}\n"
        "描述必须能由字段名、类型、约束、脱敏样例和上下文支持；字段描述保持简洁，"
        "不能包含真实敏感值，不要臆造业务口径。"
        '只输出 JSON:{"table_comment": "...", "columns": {"列名": "注释"}}'
    )


_COMMENT_SCHEMA = {
    "type": "object",
    "required": ["table_comment", "columns"],
    "properties": {
        "table_comment": {"type": "string"},
        "columns": {"type": "object"},
    },
}


def validate_comment_draft(table: TableMeta, draft: dict) -> tuple[dict, list[str], float]:
    """事实/泄漏/重复校验，并给出用于审核排序的保守置信度。"""
    allowed = {column.name for column in table.columns}
    columns = dict(draft.get("columns") or {})
    errors: list[str] = []
    unknown = sorted(set(columns) - allowed)
    if unknown:
        errors.append(f"生成结果包含不存在字段:{unknown}")
        for name in unknown:
            columns.pop(name, None)
    sensitive_pattern = re.compile(r"(?<!\d)(?:1\d{10}|\d{17}[\dXx])(?!\d)")
    for name, comment in list(columns.items()):
        if sensitive_pattern.search(str(comment)):
            errors.append(f"字段 {name} 描述疑似包含真实敏感值")
            columns.pop(name, None)
        elif len(str(comment)) > 100:
            errors.append(f"字段 {name} 描述超过 100 字")
    table_comment = str(draft.get("table_comment") or "").strip()
    if sensitive_pattern.search(table_comment):
        errors.append("表描述疑似包含真实敏感值")
        table_comment = ""
    elif len(table_comment) > 300:
        errors.append("表描述超过 300 字")
    reverse: dict[str, list[str]] = defaultdict(list)
    for name, comment in columns.items():
        normalized = "".join(str(comment).split())
        if normalized:
            reverse[normalized].append(name)
    duplicates = [names for names in reverse.values() if len(names) > 1]
    if duplicates:
        errors.append(f"多个字段描述完全相同:{duplicates}")
    evidence_ratio = sum(bool(column.profile) for column in table.columns) / max(len(table.columns), 1)
    classified_ratio = sum(column.category not in ("", "unknown") for column in table.columns) / max(len(table.columns), 1)
    confidence = 0.55 + 0.15 * evidence_ratio + 0.15 * classified_ratio
    if table.primary_keys or table.relations:
        confidence += 0.05
    confidence -= min(0.3, 0.1 * len(errors))
    confidence = round(max(0.0, min(1.0, confidence)), 3)
    cleaned = {**draft, "table_comment": table_comment, "columns": columns}
    return cleaned, errors, confidence


def generate_comment_draft(
    table: TableMeta,
    sample_values: dict[str, list[str]],
    llm,
    database_context: str = "",
    prompts=None,
) -> dict:
    """粗到细理解字段，再细到粗生成字段和表描述。"""
    evidence = _field_evidence(table, sample_values)
    preliminary = _generate_preliminary_context(table, database_context, evidence, llm, prompts)
    category_context = _generate_category_context(table, evidence, llm, prompts)
    # 宽表按字段批次生成，避免单次输出过长；只接受当前批次内的字段名。
    columns: dict[str, str] = {}
    batch_size = 30
    for start in range(0, len(table.columns), batch_size):
        batch = table.columns[start : start + batch_size]
        names = {column.name for column in batch}
        batch_evidence = "\n".join(
            line for line in evidence.splitlines()
            if any(line.startswith(name + "(") for name in names)
        )
        prompt_text = (
            prompts.render("schema_comment/field_batch",
                database_context=database_context or "(未知)",
                preliminary_context=preliminary or "(无)",
                category_context=category_context or "(无)",
                table_name=table.table_name,
                batch_evidence=batch_evidence,
            )
            if prompts else
            f"数据库上下文:{database_context or '(未知)'}\n"
            f"表初步理解:{preliminary or '(无)'}\n同类字段辨析:{category_context or '(无)'}\n"
            f"表:{table.table_name}\n本批字段证据:\n{batch_evidence}\n"
            "逐字段生成简洁、可区分、可由证据支持的描述；不能输出批次之外的字段，"
            "不能包含真实敏感值。只输出 JSON:{\"columns\":{\"列名\":\"描述\"}}"
        )
        batch_schema = {
            "type": "object", "required": ["columns"],
            "properties": {"columns": {"type": "object"}},
        }
        data = llm.complete_json(prompt_text, batch_schema, retries=1)
        columns.update({
            key: str(value).strip()
            for key, value in (data.get("columns") or {}).items()
            if key in names and str(value).strip()
        })

    # 细到粗:字段描述确定后再反向汇总表描述。
    table_prompt = (
        prompts.render("schema_comment/table_comment",
            database_context=database_context or "(未知)",
            table_name=table.table_name,
            preliminary_context=preliminary or "(无)",
            columns_json=json.dumps(columns, ensure_ascii=False),
        )
        if prompts else
        f"数据库上下文:{database_context or '(未知)'}\n表:{table.table_name}\n"
        f"表初步理解:{preliminary or '(无)'}\n"
        f"字段描述:{json.dumps(columns, ensure_ascii=False)}\n"
        "根据字段描述总结表的数据粒度、核心实体和用途，不得引入字段之外的事实。"
        "只输出 JSON:{\"table_comment\":\"...\"}"
    )
    table_schema = {
        "type": "object", "required": ["table_comment"],
        "properties": {"table_comment": {"type": "string"}},
    }
    table_data = llm.complete_json(table_prompt, table_schema, retries=1)
    draft = {
        "table_comment": str(table_data.get("table_comment") or "").strip(),
        "columns": columns,
        "preliminary_description": preliminary,
        "category_context": category_context,
    }
    cleaned, errors, confidence = validate_comment_draft(table, draft)
    cleaned["validation_errors"] = errors
    cleaned["confidence"] = confidence
    return cleaned


def build_review_entries(
    table: TableMeta, draft: dict, config: dict | None = None
) -> list[tuple[str | None, str]]:
    """把草稿整理成审核条目:[(column_name_or_None, draft_comment), ...]。

    只收缺失注释的部分(已有的原生注释不重复审核)。
    """
    entries: list[tuple[str | None, str]] = []
    cfg = _quality_config(config or {})
    generic = {"".join(str(x).split()).lower() for x in cfg.get(
        "generic_comments", ["字段", "数据", "相关字段", "业务字段", "暂无"]
    )}
    if _weak_comment(table.table_comment, int(cfg.get("min_table_comment_length", 4)), generic) and draft.get("table_comment"):
        entries.append((None, draft["table_comment"]))
    for c in table.columns:
        if _weak_comment(c.comment, int(cfg.get("min_column_comment_length", 2)), generic) and draft["columns"].get(c.name):
            entries.append((c.name, draft["columns"][c.name]))
    return entries
