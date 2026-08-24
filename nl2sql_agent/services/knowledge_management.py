"""Migration and publication validation for enterprise knowledge records."""

from __future__ import annotations

from typing import Any

import sqlglot
from sqlglot import exp

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.knowledge_store import KnowledgeStore


def seed_from_legacy_config(store: KnowledgeStore, loader: ConfigLoader) -> None:
    """Import legacy YAML once; source_key makes repeated startup idempotent."""
    mapping_dir = loader.base_dir / "term_mapping"
    for path in sorted(mapping_dir.glob("*.yaml")):
        namespace = path.stem
        data = loader.load(f"term_mapping/{path.name}") or {}
        for term, config in data.items():
            store.seed({
                "knowledge_type": "term",
                "name": term,
                "description": config.get("definition", ""),
                "namespace": namespace,
                "status": "published",
                "source": "legacy_yaml",
                "source_key": f"term:{namespace}:{term}",
                "created_by": "migration",
                "payload": {
                    "resolved_fields": list(config.get("resolved_fields", [])),
                    "composite_metric": bool(config.get("composite_metric", False)),
                    "aggregation": config.get("aggregation"),
                },
            })
            aliases = list(config.get("aliases", []))
            if aliases:
                store.seed({
                    "knowledge_type": "synonym",
                    "name": f"{term}的同义表达",
                    "description": f"旧术语配置中迁移的“{term}”同义表达",
                    "namespace": namespace,
                    "status": "published",
                    "source": "legacy_yaml",
                    "source_key": f"synonym:{namespace}:{term}",
                    "created_by": "migration",
                    "payload": {
                        "canonical_term": term,
                        "aliases": aliases,
                        "relation_type": "equivalent",
                    },
                })

    predicate_config = loader.load("business_predicates.yaml") or {}
    for rule_id, rule in (predicate_config.get("business_predicates") or {}).items():
        store.seed({
            "knowledge_type": "business_rule",
            "name": str(rule.get("concept") or rule_id),
            "description": str(rule.get("assumption") or ""),
            "namespace": "global",
            "status": "published",
            "source": "legacy_yaml",
            "source_key": f"business_rule:{rule_id}",
            "created_by": "migration",
            "payload": {"rule_id": rule_id, "rule_type": "predicate", **rule},
        })

    few_shot = loader.load("few_shot.yaml") or {}
    for pattern in few_shot.get("plan_patterns", []):
        store.seed({
            "knowledge_type": "optimization_case",
            "name": str(pattern.get("id") or "未命名计划模式"),
            "description": str(pattern.get("question_pattern") or ""),
            "namespace": "global",
            "status": "published" if pattern.get("enabled", True) else "disabled",
            "source": "legacy_yaml",
            "source_key": f"plan_pattern:{pattern.get('id')}",
            "created_by": "migration",
            "payload": {"case_type": "plan_pattern", **pattern},
        })
    for example in few_shot.get("sql_fallback_examples", []):
        store.seed({
            "knowledge_type": "optimization_case",
            "name": str(example.get("id") or "未命名 SQL 示例"),
            "description": str(example.get("user_query") or ""),
            "namespace": "global",
            "status": "published" if example.get("verified") else "draft",
            "source": "legacy_yaml",
            "source_key": f"sql_example:{example.get('id')}",
            "created_by": "migration",
            "payload": {"case_type": "sql_fallback", **example},
        })


def validate_knowledge(item: dict, schema_tables: list[dict] | None = None) -> list[str]:
    """Validate publishable structure without exposing a full schema to a model."""
    errors: list[str] = []
    payload = item.get("payload") or {}
    kind = item.get("knowledge_type")
    table_map = {
        table.get("table_name"): {column.get("name") for column in table.get("columns", [])}
        for table in (schema_tables or [])
    }

    def validate_binding(table: str | None, column: str | None) -> None:
        if not table or not column:
            errors.append("物理字段绑定必须同时包含表和字段")
        elif table_map and table not in table_map:
            errors.append(f"表 {table} 不属于当前数据库")
        elif table_map and column not in table_map[table]:
            errors.append(f"表 {table} 不存在字段 {column}")

    if kind == "term":
        bindings = payload.get("bindings") or []
        legacy_fields = payload.get("resolved_fields") or []
        if not bindings and not legacy_fields:
            errors.append("业务名词至少需要一个物理字段绑定")
        for binding in bindings:
            validate_binding(binding.get("table"), binding.get("column"))
    elif kind == "synonym":
        canonical = str(payload.get("canonical_term") or "").strip()
        aliases = [str(alias).strip() for alias in payload.get("aliases", []) if str(alias).strip()]
        if not canonical:
            errors.append("同义表达必须指定标准词")
        if not aliases:
            errors.append("至少需要一个同义表达")
        if canonical in aliases:
            errors.append("标准词不能同时作为自己的同义表达")
        if payload.get("relation_type") not in {
            "equivalent", "abbreviation", "broader", "narrower", "related", "forbidden",
        }:
            errors.append("同义关系类型无效")
    elif kind == "business_rule":
        if not payload.get("rule_type"):
            errors.append("业务规则必须指定规则类型")
        if payload.get("table") or payload.get("column"):
            validate_binding(payload.get("table"), payload.get("column"))
        if payload.get("rule_type") == "predicate":
            if not payload.get("operator"):
                errors.append("状态判断规则必须提供运算符")
    elif kind == "optimization_case":
        case_type = payload.get("case_type")
        if case_type == "sql_fallback":
            if not str(payload.get("user_query") or "").strip():
                errors.append("优化案例必须提供 SQL 对应的用户问题")
            sql = str(payload.get("sql") or "").strip()
            if not sql:
                errors.append("优化案例 SQL 不能为空")
            else:
                try:
                    expression = sqlglot.parse_one(sql, read=payload.get("dialect") or None)
                    if not isinstance(expression, (exp.Select, exp.Union, exp.Subquery)):
                        errors.append("优化案例只允许只读查询")
                    else:
                        cte_names = {
                            cte.alias_or_name for cte in expression.find_all(exp.CTE)
                            if cte.alias_or_name
                        }
                        payload["used_tables"] = sorted({
                            table.name for table in expression.find_all(exp.Table)
                            if table.name and table.name not in cte_names
                        })
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"SQL 无法解析: {exc}")
            used_tables = set(payload.get("used_tables") or [])
            if not used_tables:
                errors.append("SQL 中未识别到可校验的数据表")
            if table_map:
                missing = sorted(used_tables - set(table_map))
                if missing:
                    errors.append(f"案例引用了当前数据库不存在的表: {missing}")
        else:
            errors.append("企业优化案例只支持经过验证的准确 SQL")
    return errors
