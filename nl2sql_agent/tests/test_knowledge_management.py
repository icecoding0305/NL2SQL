from nl2sql_agent.services.knowledge_management import validate_knowledge
from nl2sql_agent.services.knowledge_store import KnowledgeStore
from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.term_mapping import TermMappingService, TermResolutionStatus


def test_knowledge_store_versions_and_builds_database_runtime_bundle(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    term = store.create({
        "knowledge_type": "term",
        "name": "逾期本金",
        "description": "当前逾期本金余额",
        "database_id": "db-a",
        "namespace": "risk_mart",
        "status": "published",
        "priority": 10,
        "payload": {
            "bindings": [{"table": "loan", "column": "ovd_bal"}],
            "composite_metric": False,
        },
    })
    store.create({
        "knowledge_type": "synonym",
        "name": "逾期本金同义表达",
        "database_id": "db-a",
        "namespace": "risk_mart",
        "status": "published",
        "payload": {
            "canonical_term": "逾期本金",
            "aliases": ["逾期金额"],
            "relation_type": "equivalent",
        },
    })

    updated = store.update(term["id"], {**term, "description": "更新后的业务解释"})
    assert updated and updated["version"] == 2
    bundle = store.runtime_bundle("db-a", "risk_mart")
    assert bundle["terms"]["逾期本金"]["resolved_fields"] == ["ovd_bal"]
    assert bundle["terms"]["逾期本金"]["aliases"] == ["逾期金额"]
    assert store.runtime_bundle("db-b", "risk_mart")["terms"] == {}


def test_related_but_not_equivalent_synonym_is_not_used_for_runtime_rewrite(tmp_path):
    store = KnowledgeStore(tmp_path / "knowledge.db")
    store.create({
        "knowledge_type": "term", "name": "不良贷款", "status": "published",
        "payload": {"resolved_fields": ["npl_flag"]},
    })
    store.create({
        "knowledge_type": "synonym", "name": "不良和逾期", "status": "published",
        "payload": {
            "canonical_term": "不良贷款", "aliases": ["逾期贷款"],
            "relation_type": "related",
        },
    })
    assert store.runtime_bundle("db-a", "risk_mart")["terms"]["不良贷款"]["aliases"] == []


def test_publish_validation_checks_physical_fields_and_sql_safety():
    schema = [{
        "table_name": "loan",
        "columns": [{"name": "ovd_bal", "type": "decimal", "comment": "逾期本金"}],
    }]
    valid_term = {
        "knowledge_type": "term",
        "payload": {"bindings": [{"table": "loan", "column": "ovd_bal"}]},
    }
    invalid_term = {
        "knowledge_type": "term",
        "payload": {"bindings": [{"table": "loan", "column": "OVD_BAL"}]},
    }
    dangerous_case = {
        "knowledge_type": "optimization_case",
        "payload": {
            "case_type": "sql_fallback", "user_query": "删除贷款",
            "sql": "DELETE FROM loan", "dialect": "mysql",
        },
    }
    valid_case = {
        "knowledge_type": "optimization_case",
        "payload": {
            "case_type": "sql_fallback", "user_query": "查询逾期余额",
            "sql": "SELECT ovd_bal FROM loan", "dialect": "mysql",
        },
    }
    assert validate_knowledge(valid_term, schema) == []
    assert any("OVD_BAL" in error for error in validate_knowledge(invalid_term, schema))
    assert any("只允许只读" in error for error in validate_knowledge(dangerous_case, schema))
    assert validate_knowledge(valid_case, schema) == []
    assert valid_case["payload"]["used_tables"] == ["loan"]


def test_database_knowledge_overlay_preserves_real_column_case(tmp_path):
    (tmp_path / "term_mapping").mkdir()
    service = TermMappingService(ConfigLoader(tmp_path.resolve()), {
        "逾期本金": {
            "business_line": "risk_mart",
            "resolved_fields": ["ovd_bal"],
            "definition": "当前逾期本金余额",
            "aliases": ["逾期金额"],
        },
    })
    resolution = service.resolve("逾期本金", ["risk_mart"])
    assert resolution.status == TermResolutionStatus.FOUND
    assert resolution.entries[0].resolved_fields == ["ovd_bal"]
    assert service.extract_terms("统计逾期金额", ["risk_mart"]) == ["逾期本金"]
