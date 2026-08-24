"""节点级单元测试:术语解析、Schema 检索、复杂度、计划校验、
静态校验(危险操作/字段幻觉/行级过滤)、敏感判定、澄清规则。

基于真实借据表 dwd_ar_loan_info,data_scope 为平台代码(PLATFORM_CODE)。
"""

from __future__ import annotations

import pytest

from nl2sql_agent.services.llm import SQLResult
from nl2sql_agent.services.term_mapping import TermResolutionStatus
from nl2sql_agent.state import NL2SQLState, QueryPlan, SchemaHit
from nl2sql_agent.testing import FakeLLM, build_test_deps

from .conftest import make_input


# 借据表关键列(用于构造测试 SchemaHit)
_COLUMNS = {
    "LOAN_NO": {"name": "LOAN_NO", "type": "varchar", "comment": "借据编码"},
    "CUST_ID": {"name": "CUST_ID", "type": "varchar", "comment": "客户ID"},
    "LOAN_STATUS": {"name": "LOAN_STATUS", "type": "varchar", "comment": "贷款状态"},
    "OVD_BAL": {"name": "OVD_BAL", "type": "decimal", "comment": "逾期本金余额"},
    "PRIN_BAL": {"name": "PRIN_BAL", "type": "decimal", "comment": "贷款本金余额"},
    "NORMAL_BAL": {"name": "NORMAL_BAL", "type": "decimal", "comment": "正常本金余额"},
    "LOAN_AMT": {"name": "LOAN_AMT", "type": "decimal", "comment": "贷款金额"},
    "IDNUM": {"name": "IDNUM", "type": "varchar", "comment": "证件号码"},
    "NAME": {"name": "NAME", "type": "varchar", "comment": "客户姓名"},
    "PLATFORM_CODE": {"name": "PLATFORM_CODE", "type": "varchar", "comment": "平台代码"},
}


def loan_schema(*columns: str) -> SchemaHit:
    return SchemaHit(
        table_name="dwd_ar_loan_info",
        columns=[_COLUMNS[c] for c in columns],
        business_terms=[],
    )


# ---------------- 术语映射 ----------------

def test_term_resolution_global(deps):
    tm = deps.term_mapping
    res = tm.resolve("逾期率", ["risk_mart"])
    assert res.status == TermResolutionStatus.FOUND
    assert res.entries[0].composite_metric is True
    assert res.entries[0].definition == "逾期借据数 / 总借据数(OVD_BAL>0 视为逾期)"
    # 业务线(平台)专属映射查不到时,全局兜底
    assert tm.resolve("贷款余额", ["risk_mart"]).status == TermResolutionStatus.FOUND
    # 库里没有的术语 → not found
    assert tm.resolve("门店进店量", ["risk_mart"]).status == TermResolutionStatus.NOT_FOUND


def test_extract_terms(deps):
    terms = deps.term_mapping.extract_terms("查询新信贷的逾期本金", ["risk_mart"])
    assert "逾期本金" in terms
    terms2 = deps.term_mapping.extract_terms("查询字节的逾期率", ["risk_mart"])
    assert "逾期率" in terms2


# ---------------- 模块 3:Schema 检索 ----------------

def test_schema_retrieval_by_system(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import make_schema_retrieval_node

    node = make_schema_retrieval_node(deps)
    # 表归属 risk_mart 系统:该系统的用户可检索到
    out = node(NL2SQLState(**make_input("查询新信贷的逾期本金", data_scope=["risk_mart"])))
    assert [h.table_name for h in out["retrieved_schema"]] == ["dwd_ar_loan_info"]
    # 其他系统(dw/core)用户检索不到——系统级隔离
    for scope in (["dw"], ["core"]):
        out2 = node(NL2SQLState(**make_input("查询新信贷的逾期本金", data_scope=scope)))
        assert [h.table_name for h in out2["retrieved_schema"]] == []


def test_schema_retrieval_term_layer_then_vector(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import make_schema_retrieval_node

    node = make_schema_retrieval_node(deps)
    out = node(NL2SQLState(**make_input("查询新信贷的逾期本金")))
    assert [h.table_name for h in out["retrieved_schema"]] == ["dwd_ar_loan_info"]
    assert out["retrieved_schema"][0].business_terms == ["逾期本金"]


def test_schema_retrieval_expands_domain_query(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import make_schema_retrieval_node

    out = make_schema_retrieval_node(deps)(
        NL2SQLState(**make_input("借了多少", data_scope=["risk_mart"]))
    )
    assert out["retrieval_confidence"] == 1.0
    assert out["retrieved_schema"][0].business_terms == ["贷款金额"]


def test_schema_catalog_covers_term_fields_across_tables(deps):
    from nl2sql_agent.services.schema_catalog import TableDef

    deps.catalog._tables_by_line["risk_mart"] = [  # noqa: SLF001
        TableDef("person", "个人客户", "risk_mart", [{"name": "PERSON_NAME"}]),
        TableDef("company", "企业客户", "risk_mart", [{"name": "COMPANY_NAME"}]),
    ]
    hits = deps.catalog.hits_covering_term_fields(
        "客户信息", ["PERSON_NAME", "COMPANY_NAME"], ["risk_mart"]
    )
    assert {hit.table_name for hit in hits} == {"person", "company"}


def test_relative_candidate_gap_and_dynamic_weights(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import _candidate_is_close, _hybrid_config

    assert _candidate_is_close(deps, 0.25, 0.18) is False
    table_weight, column_weight, _, _ = _hybrid_config(deps, "学历为本科的客户")
    assert column_weight > table_weight


def test_join_path_adds_only_bridge_tables(deps, monkeypatch):
    from nl2sql_agent.nodes.m3_schema_retrieval import _join_path_supplements
    from nl2sql_agent.services.schema_catalog import TableDef
    from nl2sql_agent.services.schema_ingest import text_builder

    deps.catalog._tables_by_line["risk_mart"] = [  # noqa: SLF001
        TableDef("customer", "客户", "risk_mart", [{"name": "CUST_ID"}]),
        TableDef("application", "申请", "risk_mart", [{"name": "APP_ID"}]),
        TableDef("loan", "借据", "risk_mart", [{"name": "LOAN_NO"}]),
    ]
    monkeypatch.setattr(
        text_builder,
        "load_mschema_vector_source",
        lambda metadata: ({
            "relations": [
                {"source_table": "customer", "target_table": "application"},
                {"source_table": "application", "target_table": "loan"},
            ]
        }, {}),
    )
    bridges = _join_path_supplements(
        deps, {"customer", "loan"}, ["risk_mart"]
    )
    assert [hit.table_name for hit in bridges] == ["application"]


def test_schema_retrieval_uses_column_vectors(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import make_schema_retrieval_node
    from nl2sql_agent.services.schema_catalog import TableDef

    customer = TableDef(
        name="customer_profile",
        comment="客户资料",
        business_line="risk_mart",
        columns=[{"name": "EDUCATION", "type": "varchar", "comment": "最高学历"}],
    )
    deps.catalog._tables_by_line.setdefault("risk_mart", []).append(customer)  # noqa: SLF001
    deps.vector_store._ensure_indexed()  # noqa: SLF001
    deps.vector_store.upsert(
        deps.vector_store.COLLECTION_TABLE,
        customer.name,
        "客户资料",
        {"table_name": customer.name, "business_line": "risk_mart"},
    )
    deps.vector_store.upsert(
        deps.vector_store.COLLECTION_COLUMN,
        f"{customer.name}#col#0",
        "EDUCATION 最高学历 学历",
        {"table_name": customer.name, "business_line": "risk_mart"},
    )

    out = make_schema_retrieval_node(deps)(
        NL2SQLState(**make_input("最高学历", data_scope=["risk_mart"]))
    )
    assert out["retrieved_schema"][0].table_name == customer.name


def test_schema_retrieval_expands_direct_relation_only(deps):
    from nl2sql_agent.nodes.m3_schema_retrieval import make_schema_retrieval_node
    from nl2sql_agent.services.schema_catalog import TableDef

    customer = TableDef(
        name="customer_profile",
        comment="客户资料",
        business_line="risk_mart",
        columns=[{"name": "CUST_ID", "type": "varchar", "comment": "客户编号"}],
    )
    deps.catalog._tables_by_line.setdefault("risk_mart", []).append(customer)  # noqa: SLF001
    deps.vector_store._ensure_indexed()  # noqa: SLF001
    deps.vector_store.upsert(
        deps.vector_store.COLLECTION_RELATION,
        "dwd_ar_loan_info#rel#0",
        "贷款金额对应的客户关联关系",
        {
            "table_name": "dwd_ar_loan_info",
            "target_table": customer.name,
            "business_line": "risk_mart",
        },
    )

    out = make_schema_retrieval_node(deps)(
        NL2SQLState(**make_input("贷款金额对应客户", data_scope=["risk_mart"]))
    )
    names = [hit.table_name for hit in out["retrieved_schema"]]
    assert "dwd_ar_loan_info" in names
    assert customer.name in names


def test_field_driven_schema_plan_selects_fact_entity_and_bridge():
    from nl2sql_agent.services.schema_catalog import TableDef
    from nl2sql_agent.services.schema_planner import (
        build_schema_plan,
        parse_query_intent,
        plan_table_names,
        rank_field_candidates,
    )

    tables = [
        TableDef("claim", "代偿记录明细表", "risk_mart", [
            {"name": "LOAN_NO", "type": "varchar", "comment": "借据编号"},
            {"name": "DC_BAL", "type": "decimal", "comment": "代偿本金", "semantic_role": "measure"},
            {"name": "DC_ALL_BAL", "type": "decimal", "comment": "代偿总额", "semantic_role": "measure"},
        ]),
        TableDef("loan", "贷款借据信息表", "risk_mart", [
            {"name": "LOAN_NO", "type": "varchar", "comment": "借据编号"},
            {"name": "CUST_ID", "type": "varchar", "comment": "客户编号"},
            {"name": "LOAN_AMT", "type": "decimal", "comment": "贷款金额", "semantic_role": "measure"},
        ]),
        TableDef("customer", "个人客户基本信息表", "risk_mart", [
            {"name": "CUST_ID", "type": "varchar", "comment": "客户编号", "primary_key": True},
            {"name": "NAME", "type": "varchar", "comment": "客户姓名"},
        ]),
        TableDef("repay", "还款明细表", "risk_mart", [
            {"name": "AMT", "type": "decimal", "comment": "还款金额", "semantic_role": "measure"},
        ]),
    ]
    relations = [
        {"source_table": "claim", "source_columns": ["LOAN_NO"], "target_table": "loan", "target_columns": ["LOAN_NO"], "relation_type": "foreign_key"},
        {"source_table": "loan", "source_columns": ["CUST_ID"], "target_table": "customer", "target_columns": ["CUST_ID"], "relation_type": "foreign_key"},
    ]
    intent = parse_query_intent("统计代偿金额超过10000的客户的基本信息")
    candidates = rank_field_candidates(intent, tables)
    plan = build_schema_plan(intent, tables, candidates, relations)

    assert intent.filters[0].operator == ">"
    assert plan.anchor_tables[0].table_name == "claim"
    assert plan.anchor_tables[0].selected_columns == ["DC_ALL_BAL"]
    assert plan.dimension_tables[0].table_name == "customer"
    assert plan.bridge_tables[0].table_name == "loan"
    assert plan_table_names(plan) == ["claim", "loan", "customer"]
    assert plan.unresolved_slots == []


def test_field_ambiguity_only_compares_same_business_slot():
    from nl2sql_agent.services.schema_planner import find_field_ambiguities
    from nl2sql_agent.state import FieldCandidate

    options = [
        FieldCandidate(table_name="claim_a", column_name="DC_AMT", query_slot="代偿金额", final_score=0.8, phrase_coverage=1.0),
        FieldCandidate(table_name="claim_b", column_name="CLAIM_AMT", query_slot="代偿金额", final_score=0.76, phrase_coverage=0.9),
        FieldCandidate(table_name="customer", column_name="CUST_ID", query_slot="客户", final_score=0.79),
    ]
    ambiguities = find_field_ambiguities(options)
    assert list(ambiguities) == ["代偿金额"]
    assert {item.table_name for item in ambiguities["代偿金额"]} == {"claim_a", "claim_b"}


# ---------------- 模块 4:复杂度 ----------------

def test_complexity_composite_metric(deps):
    from nl2sql_agent.nodes.m4_complexity_check import make_complexity_check_node

    st = NL2SQLState(
        **make_input("查询新信贷的逾期率"),
        retrieved_schema=[loan_schema("OVD_BAL", "LOAN_STATUS")],
    )
    out = make_complexity_check_node(deps)(st)
    assert out["is_complex"] is True
    assert any("复合口径" in r for r in out["complex_reasons"])


def test_complexity_simple_single_table(deps):
    from nl2sql_agent.nodes.m4_complexity_check import make_complexity_check_node

    st = NL2SQLState(
        **make_input("查询新信贷的贷款余额"),
        retrieved_schema=[loan_schema("PRIN_BAL")],
    )
    out = make_complexity_check_node(deps)(st)
    assert out["is_complex"] is False


# ---------------- 模块 6:计划校验 ----------------

def test_plan_validation_passes_and_rejects(deps):
    from nl2sql_agent.nodes.m6_plan_validation import validate_plan

    schema = [loan_schema("OVD_BAL", "LOAN_STATUS", "PRIN_BAL")]
    good = QueryPlan(
        target_tables=["dwd_ar_loan_info"],
        filters=[{"column": "OVD_BAL", "operator": ">", "value": 0}],
        metric_logic={
            "metric_name": "逾期率",
            "definition": "逾期借据数 / 总借据数(OVD_BAL>0 视为逾期)",
            "columns": ["OVD_BAL", "LOAN_STATUS"],
        },
        group_by=[],
        confidence=0.9,
    )
    assert validate_plan(good, schema, deps.term_mapping, ["risk_mart"]) == []

    bad_table = QueryPlan(target_tables=["unknown_table"], filters=[], metric_logic=None)
    errs = validate_plan(bad_table, schema, deps.term_mapping, ["risk_mart"])
    assert any("unknown_table" in e for e in errs)

    bad_metric = QueryPlan(
        target_tables=["dwd_ar_loan_info"],
        metric_logic={"metric_name": "逾期率", "definition": "错误口径", "columns": ["OVD_BAL"]},
        filters=[],
    )
    errs2 = validate_plan(bad_metric, schema, deps.term_mapping, ["risk_mart"])
    assert any("definition" in e and "不一致" in e for e in errs2)


# ---------------- 模块 8:静态校验 ----------------

def test_static_validation_dangerous_sql_blocked_no_retry(deps):
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    node = make_static_validation_node(deps)
    st = NL2SQLState(
        **make_input("恶意查询"),
        generated_sql="DROP TABLE dwd_ar_loan_info",
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[loan_schema("LOAN_NO")],
    )
    out = node(st)
    assert out["blocked_reason"] == "Drop"
    assert out["final_answer"].startswith("SQL 被拦截")
    assert out.get("retry_count", 0) == 0  # 危险操作不进入重试


def test_static_validation_field_hallucination(deps):
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    node = make_static_validation_node(deps)
    st = NL2SQLState(
        **make_input("查询"),
        generated_sql="SELECT foo_bar FROM dwd_ar_loan_info",
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[loan_schema("LOAN_NO")],
    )
    out = node(st)
    assert out["validation_errors"]
    assert any("foo_bar" in e for e in out["validation_errors"])
    assert out["retry_count"] == 1  # 非危险类错误 → 触发重试


def test_static_validation_used_tables_mismatch(deps):
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    node = make_static_validation_node(deps)
    st = NL2SQLState(
        **make_input("查询"),
        generated_sql="SELECT LOAN_NO FROM dwd_ar_loan_info",
        used_tables=["other_table"],  # 与 AST 不一致
        retrieved_schema=[loan_schema("LOAN_NO")],
    )
    out = node(st)
    assert out["validation_errors"]
    assert out["retry_count"] == 1


def test_static_validation_allows_order_by_alias(deps):
    # SELECT 别名在 ORDER BY 中引用是合法 SQL,不能被误判为字段幻觉
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    node = make_static_validation_node(deps)
    st = NL2SQLState(
        **make_input("查询放款金额最高的产品"),
        generated_sql="SELECT SUM(LOAN_AMT) AS total_amt FROM dwd_ar_loan_info ORDER BY total_amt DESC",
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[loan_schema("LOAN_AMT", "PLATFORM_CODE")],
    )
    out = node(st)
    assert not out["validation_errors"]


def test_static_validation_alias_sql(deps):
    # 千问生成带表别名的 JOIN SQL(如 t1/t2):
    # 1) 字段校验必须解析别名,不能误判"表 t2 不存在字段"
    # 2) 行级过滤注入必须用别名限定,不能生成 dwd_ar_loan_info.PLATFORM_CODE(别名遮蔽后不可引用)
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    deps.config.row_level_filter["enabled"] = True  # 启用行级过滤以验证别名注入
    node = make_static_validation_node(deps)
    cust = SchemaHit(
        table_name="dwd_ip_indv_cust_info",
        columns=[
            {"name": "CUST_ID", "type": "varchar", "comment": "客户ID"},
            {"name": "NAME", "type": "varchar", "comment": "客户姓名"},
            {"name": "RESIADDR", "type": "varchar", "comment": "居住地址"},
            {"name": "PLATFORM_CODE", "type": "varchar", "comment": "平台代码"},
        ],
        business_terms=[],
    )
    sql = ("SELECT DISTINCT t2.NAME FROM dwd_ar_loan_info AS t1 "
           "INNER JOIN dwd_ip_indv_cust_info AS t2 ON t1.CUST_ID = t2.CUST_ID "
           "WHERE t1.LOAN_AMT > 1000")
    st = NL2SQLState(
        **make_input("查询客户姓名"),
        generated_sql=sql,
        used_tables=["dwd_ar_loan_info", "dwd_ip_indv_cust_info"],
        retrieved_schema=[loan_schema("LOAN_AMT", "CUST_ID", "PLATFORM_CODE"), cust],
        row_level_filters={"PLATFORM_CODE": ["XXD"]},
    )
    out = node(st)
    assert not out["validation_errors"]                      # 别名不误判字段
    assert "t1.PLATFORM_CODE IN" in out["generated_sql"]     # 注入用别名限定
    assert "t2.PLATFORM_CODE IN" in out["generated_sql"]
    assert "risk_mart" not in out["generated_sql"]


def test_static_validation_injects_row_level_filter(deps):
    # 行级过滤默认关闭(业务线按系统维度);启用时按 data_scope 注入
    from nl2sql_agent.nodes.m8_static_validation import make_static_validation_node

    deps.config.row_level_filter["enabled"] = True
    node = make_static_validation_node(deps)
    st = NL2SQLState(
        **make_input("查询", data_scope=["risk_mart"]),
        generated_sql="SELECT LOAN_NO FROM dwd_ar_loan_info",
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[loan_schema("LOAN_NO", "PLATFORM_CODE")],
        row_level_filters={"PLATFORM_CODE": ["XXD", "ZJ"]},
    )
    out = node(st)
    assert "dwd_ar_loan_info.PLATFORM_CODE IN ('XXD', 'ZJ')" in out["generated_sql"]
    assert not out["validation_errors"]


# ---------------- 模块 9:敏感判定 ----------------

def test_sensitive_detects_sensitive_field(deps):
    from nl2sql_agent.nodes.m9_sensitive_check import make_sensitive_check_node

    st = NL2SQLState(
        **make_input("查询借据的证件号码"),
        generated_sql="SELECT LOAN_NO, IDNUM FROM dwd_ar_loan_info",
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[loan_schema("LOAN_NO", "IDNUM")],
    )
    out = make_sensitive_check_node(deps)(st)
    assert out["is_sensitive"] is True
    assert any("IDNUM" in r for r in out["sensitive_reasons"])


def test_sensitive_detects_amount_aggregation(deps):
    from nl2sql_agent.nodes.m9_sensitive_check import make_sensitive_check_node

    st = NL2SQLState(
        **make_input("查询贷款余额"),
        generated_sql="SELECT SUM(PRIN_BAL) AS total FROM dwd_ar_loan_info",
        used_tables=["dwd_ar_loan_info"],
        retrieved_schema=[loan_schema("PRIN_BAL")],
    )
    out = make_sensitive_check_node(deps)(st)
    assert out["is_sensitive"] is True


# ---------------- 模块 2:澄清(只做时间范围检查) ----------------

def test_clarify_time_range_missing(deps):
    from nl2sql_agent.nodes.m2_clarify_time_range import make_clarify_time_range_node

    node = make_clarify_time_range_node(deps)
    out = node(NL2SQLState(**make_input("查询新信贷贷款余额的时间段分布")))
    assert out["need_clarification"] is True
    assert out["clarification_reason"] == "missing_time_range"
    assert any("时间范围" in q for q in out["clarification_questions"])


def test_clarify_not_triggered_when_range_present(deps):
    from nl2sql_agent.nodes.m2_clarify_time_range import make_clarify_time_range_node

    node = make_clarify_time_range_node(deps)
    out = node(NL2SQLState(**make_input("查询新信贷近三个月的贷款余额")))
    assert out["need_clarification"] is False


def test_clarify_term_not_consulted(deps):
    # 模块 2 不再引用术语映射:任何查询只要不缺时间范围就不拦截
    from nl2sql_agent.nodes.m2_clarify_time_range import make_clarify_time_range_node

    node = make_clarify_time_range_node(deps)
    out = node(NL2SQLState(**make_input("查询新信贷的逾期率")))
    assert out["need_clarification"] is False
    # 术语库没有的新指标问法也不会被模块 2 拦截(交给模块 3/3.5)
    out2 = node(NL2SQLState(**make_input("查询放款成功率")))
    assert out2["need_clarification"] is False


def test_clarify_sensitivity_knob(deps):
    from nl2sql_agent.nodes.m2_clarify_time_range import make_clarify_time_range_node

    node = make_clarify_time_range_node(deps)
    deps.config.clarification_rules["sensitivity"] = 0.9
    # 时间规则 reliability 1.0,在 0.9 仍触发
    out = node(NL2SQLState(**make_input("查询新信贷贷款余额的时间段分布")))
    assert out["need_clarification"] is True
    assert out["clarification_reason"] == "missing_time_range"


def test_query_resolution_outputs_rewrite_and_decision_summary(deps):
    from nl2sql_agent.nodes.m2_query_resolution import make_query_resolution_node

    deps.config.clarification_rules.setdefault("query_resolution", {})["use_llm"] = False
    out = make_query_resolution_node(deps)(
        NL2SQLState(**make_input("  统计代偿金额超过10000的客户基本信息  "))
    )
    assert out["resolved_query"].rewritten_query == "统计代偿金额超过10000的客户基本信息"
    assert out["resolved_query"].query_type == "fact_filter"
    assert out["decision_summary"].understood_query
    assert out["need_clarification"] is False


def test_query_resolution_keeps_missing_time_as_business_question(deps):
    from nl2sql_agent.nodes.m2_query_resolution import make_query_resolution_node

    deps.config.clarification_rules.setdefault("query_resolution", {})["use_llm"] = False
    out = make_query_resolution_node(deps)(
        NL2SQLState(**make_input("查询贷款余额的时间段分布"))
    )
    assert out["need_clarification"] is True
    assert out["clarification_reason"] == "missing_time_range"
    assert "时间范围" in out["resolved_query"].unresolved_business_slots


def test_query_resolution_does_not_let_empty_model_fields_erase_explicit_outputs(deps):
    from nl2sql_agent.nodes.m2_query_resolution import make_query_resolution_node
    from nl2sql_agent.state import ResolvedQuery

    class IncompleteResolutionLLM:
        def complete_structured(self, prompt, model, retries=0):
            assert model is ResolvedQuery
            return ResolvedQuery(
                original_query="",
                rewritten_query="统计有逾期的客户姓名及地址",
                query_type="unknown",
                confidence=0,
            )

    deps.config.clarification_rules.setdefault("query_resolution", {})["use_llm"] = True
    deps.node_llms["query_resolution"] = IncompleteResolutionLLM()
    out = make_query_resolution_node(deps)(
        NL2SQLState(**make_input("统计有逾期的客户姓名及地址"))
    )

    resolved = out["resolved_query"]
    assert resolved.query_type != "unknown"
    assert {"客户姓名", "地址"} <= set(resolved.attributes)
    assert [item.concept for item in out["semantic_graph"].outputs] == ["客户姓名", "地址"]


def test_query_resolution_retries_invalid_structured_output_and_keeps_fallback_metrics(deps):
    from nl2sql_agent.nodes.m2_query_resolution import make_query_resolution_node

    query = "统计每个客户累计贷款金额、累计代偿本金和累计代偿总额"

    class InvalidResolutionLLM:
        def complete_structured(self, prompt, model, retries=0):
            assert retries == 1
            raise ValueError("rewritten_query must be a string")

    deps.config.clarification_rules.setdefault("query_resolution", {})["use_llm"] = True
    deps.node_llms["query_resolution"] = InvalidResolutionLLM()
    out = make_query_resolution_node(deps)(NL2SQLState(**make_input(query)))

    outputs = {item.concept: item for item in out["semantic_graph"].outputs}
    assert outputs["累计贷款金额"].aggregation == "sum"
    assert outputs["累计代偿本金"].aggregation == "sum"
    assert outputs["累计代偿总额"].aggregation == "sum"
    assert out["semantic_coverage"]["uncovered_mentions"] == []


def test_broad_topic_defers_model_metric_scope_question_to_schema(deps):
    from nl2sql_agent.nodes.m2_query_resolution import make_query_resolution_node
    from nl2sql_agent.state import ResolvedQuery

    query = "统计户籍地址为上海的客户的逾期情况"

    class MetricScopeQuestionLLM:
        def complete_structured(self, prompt, model, retries=0):
            assert model is ResolvedQuery
            return ResolvedQuery(
                original_query=query,
                rewritten_query=query,
                query_type="aggregation",
                unresolved_business_slots=["统计指标口径"],
                confidence=0.9,
            )

    deps.config.clarification_rules.setdefault("query_resolution", {})["use_llm"] = True
    deps.node_llms["query_resolution"] = MetricScopeQuestionLLM()

    out = make_query_resolution_node(deps)(NL2SQLState(**make_input(query)))

    assert out["need_clarification"] is False
    assert out["resolved_query"].unresolved_business_slots == []
    assert any(item.broad for item in out["semantic_graph"].outputs)


def test_high_confidence_broad_topic_uses_schema_stage_without_resolution_llm(deps):
    from nl2sql_agent.nodes.m2_query_resolution import make_query_resolution_node

    class MustNotCall:
        def complete_structured(self, *args, **kwargs):
            raise AssertionError("high-confidence broad topic should skip resolution LLM")

    deps.config.clarification_rules.setdefault("query_resolution", {})["use_llm"] = "auto"
    deps.node_llms["query_resolution"] = MustNotCall()

    out = make_query_resolution_node(deps)(NL2SQLState(**make_input(
        "统计户籍地址为上海的客户的逾期情况"
    )))

    assert out["need_clarification"] is False
    assert any(item.broad for item in out["semantic_graph"].outputs)
    assert out["semantic_coverage"]["uncovered_mentions"] == []


def test_semantic_graph_preserves_comparison_and_overdue_existence(deps):
    from nl2sql_agent.services.semantic_parser import (
        build_semantic_graph,
        required_atom_ids,
        semantic_graph_to_query_intent,
    )

    query = "统计贷款金额超过1000且有逾期的客户的基本信息"
    predicate_config = deps.loader.load("business_predicates.yaml")
    graph = build_semantic_graph(query, predicate_config)
    intent = semantic_graph_to_query_intent(graph, query, predicate_config)

    assert graph.predicate is not None
    assert graph.predicate.predicate_type == "exists"
    assert {child.predicate_type for child in graph.predicate.children} == {"comparison", "status"}
    assert required_atom_ids(graph) == {"atom_1", "atom_2", "atom_2_status"}
    assert {(item.text, item.operator, item.value) for item in intent.filters} == {
        ("贷款金额", ">", 1000),
        ("逾期本金余额", ">", 0),
    }
    assert any("当前逾期" in item.content for item in graph.assumptions)
    assert any("同一笔贷款" in item.content for item in graph.assumptions)


def test_plan_validation_rejects_missing_semantic_atom(deps):
    from nl2sql_agent.nodes.m6_plan_validation import validate_plan
    from nl2sql_agent.services.semantic_parser import build_semantic_graph

    query = "统计贷款金额超过1000且有逾期的客户的基本信息"
    graph = build_semantic_graph(query, deps.loader.load("business_predicates.yaml"))
    schema = [loan_schema("LOAN_AMT", "OVD_BAL")]
    incomplete = QueryPlan(
        target_tables=["dwd_ar_loan_info"],
        filters=[{
            "table": "dwd_ar_loan_info", "column": "LOAN_AMT",
            "operator": ">", "value": 1000, "source_atom_ids": ["atom_1"],
        }],
        covered_atom_ids=["atom_1"],
    )
    errors = validate_plan(
        incomplete, schema, deps.term_mapping, ["risk_mart"], semantic_graph=graph
    )
    assert any("atom_2" in error for error in errors)

    complete = QueryPlan(
        target_tables=["dwd_ar_loan_info"],
        filters=[
            {
                "table": "dwd_ar_loan_info", "column": "LOAN_AMT",
                "operator": ">", "value": 1000, "source_atom_ids": ["atom_1"],
            },
            {
                "table": "dwd_ar_loan_info", "column": "OVD_BAL",
                "operator": ">", "value": 0,
                "source_atom_ids": ["atom_2", "atom_2_status"],
            },
        ],
        covered_atom_ids=["atom_1", "atom_2", "atom_2_status"],
    )
    assert validate_plan(
        complete, schema, deps.term_mapping, ["risk_mart"], semantic_graph=graph
    ) == []

    wrong_binding = complete.model_copy(deep=True)
    wrong_binding.filters[1].column = "LOAN_AMT"
    errors = validate_plan(
        wrong_binding,
        schema,
        deps.term_mapping,
        ["risk_mart"],
        semantic_graph=graph,
        semantic_bindings={
            "atom_2_status": {
                "table_name": "dwd_ar_loan_info",
                "column_name": "OVD_BAL",
                "operator": ">",
                "value": 0,
            }
        },
    )
    assert any("Schema 绑定" in error for error in errors)
