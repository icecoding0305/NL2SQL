from nl2sql_agent.services.few_shot_store import FewShotStore
from nl2sql_agent.services.config_loader import ConfigLoader


def _store(examples):
    store = FewShotStore.__new__(FewShotStore)
    store.examples = examples
    return store


def test_skeleton_retrieval_prefers_same_aggregate_shape_over_business_nouns():
    store = _store([
        {
            "user_query": "统计每个产品的平均贷款金额",
            "sql": "SELECT product_id, AVG(amount) FROM loan GROUP BY product_id",
            "tags": ["group", "avg"],
        },
        {
            "user_query": "查询客户贷款信息清单",
            "sql": "SELECT customer_id, amount FROM loan",
            "tags": ["detail"],
        },
    ])

    result = store.retrieve("统计每个地区的平均代偿金额", top_k=1)
    assert result[0]["tags"] == ["group", "avg"]
    assert "group_by" in result[0]["sql_structure"]
    assert "agg:avg" in result[0]["question_skeleton"]


def test_prompt_patterns_do_not_expose_example_identifiers_or_sql():
    store = _store([{
        "user_query": "查询逾期借据清单",
        "sql": "SELECT SECRET_COL FROM SECRET_TABLE WHERE SECRET_COL > 0",
        "tags": ["detail", "where"],
    }])

    patterns = store.retrieve_patterns("查询异常记录清单")
    rendered = str(patterns)
    assert "SECRET_COL" not in rendered
    assert "SECRET_TABLE" not in rendered
    assert "sql" not in patterns[0]


def test_v2_config_covers_representative_query_shapes(deps):
    pattern_ids = {item["id"] for item in deps.few_shot.plan_patterns}
    assert {
        "single_table_detail_filter",
        "grouped_multi_metric",
        "grouped_having",
        "top_n_ranking",
        "two_table_detail_join",
        "exists_semijoin",
        "not_exists_antijoin",
        "multi_fact_preaggregate",
        "time_bucket_aggregate",
        "conditional_aggregate",
    } <= pattern_ids
    assert len(pattern_ids) == len(deps.few_shot.plan_patterns)


def test_v2_plan_patterns_are_schema_independent(deps):
    rendered = str(deps.few_shot.plan_patterns)
    assert "dwd_" not in rendered.lower()
    assert "LOAN_AMT" not in rendered
    assert "SELECT " not in rendered.upper()


def test_sql_fallback_is_limited_by_dialect_and_available_tables(deps):
    allowed = deps.few_shot.retrieve(
        "查询贷款总金额",
        dialect="mysql",
        available_tables={"dwd_ar_loan_info"},
    )
    wrong_dialect = deps.few_shot.retrieve(
        "查询贷款总金额",
        dialect="postgres",
        available_tables={"dwd_ar_loan_info"},
    )
    wrong_schema = deps.few_shot.retrieve(
        "查询贷款总金额",
        dialect="mysql",
        available_tables={"other_table"},
    )
    assert allowed
    assert wrong_dialect == []
    assert wrong_schema == []


def test_v2_loader_excludes_unverified_sql_examples(tmp_path):
    (tmp_path / "few_shot.yaml").write_text(
        """
version: 2
sql_fallback_examples:
  - id: rejected
    user_query: query
    sql: SELECT 1
    verified: false
  - id: accepted
    user_query: query
    sql: SELECT 1
    verified: true
""".strip(),
        encoding="utf-8",
    )
    store = FewShotStore(ConfigLoader(tmp_path))
    assert [item["id"] for item in store.sql_examples] == ["accepted"]


def test_representative_queries_hit_expected_v2_patterns(deps):
    cases = {
        "统计每个产品累计贷款金额超过10000元的产品": "grouped_having",
        "查询贷款总额最高的前10个客户": "top_n_ranking",
        "查询没有贷款记录的客户姓名": "not_exists_antijoin",
        "统计每个客户累计贷款金额、累计代偿本金和累计代偿总额": "multi_fact_preaggregate",
    }
    for query, expected in cases.items():
        patterns = deps.few_shot.retrieve_patterns(query)
        assert patterns[0]["pattern_id"] == expected
