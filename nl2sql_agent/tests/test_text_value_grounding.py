from nl2sql_agent.nodes.m2_clarify_time_range import _check_time_range
from nl2sql_agent.nodes.m2_query_resolution import _prefer_complete_graph
from nl2sql_agent.services.schema_catalog import TableDef
from nl2sql_agent.services.schema_planner import parse_query_intent
from nl2sql_agent.services.semantic_parser import build_semantic_graph
from nl2sql_agent.services.value_grounding import ground_text_binding
from nl2sql_agent.state import (
    FieldCandidate,
    SemanticGraph,
    SemanticOutput,
    SemanticPredicate,
    SemanticSubject,
)


QUERY = "查询户籍地址是上海的客户的基本信息"


def test_text_filter_parser_supports_shi_and_preserves_value():
    intent = parse_query_intent(QUERY)
    graph = build_semantic_graph(QUERY)

    assert [(item.text, item.operator, item.value) for item in intent.filters] == [
        ("户籍地址", "=", "上海"),
    ]
    assert graph.predicate is not None
    assert graph.predicate.concept == "户籍地址"
    assert graph.predicate.value == "上海"


def test_model_chinese_comparison_operator_is_canonicalized():
    predicate = SemanticPredicate(
        atom_id="address_filter", predicate_type="comparison",
        concept="户籍地址", operator="等于", value="上海",
    )

    assert predicate.operator == "="


def test_profile_value_prefers_controlled_region_field_and_normalizes_suffix():
    table = TableDef("customer", "客户信息表", "test", [
        {
            "name": "HOUSEADD", "type": "varchar", "comment": "户籍地址",
            "category": "text", "examples": ["上海市南京市某路123号"],
        },
        {
            "name": "HHDIST", "type": "varchar", "comment": "户籍省份",
            "category": "enum", "examples": ["四川省", "广东省", "上海市"],
            "profile": {"approx_distinct": 8, "examples": ["上海市"]},
        },
    ])
    options = [
        FieldCandidate(
            table_name="customer", column_name="HOUSEADD", column_comment="户籍地址",
            query_slot="户籍地址", final_score=0.9,
        ),
        FieldCandidate(
            table_name="customer", column_name="HHDIST", column_comment="户籍省份",
            query_slot="户籍地址", final_score=0.55,
        ),
    ]

    selected, operator, value, evidence = ground_text_binding("上海", options, [table])

    assert selected.column_name == "HHDIST"
    assert operator == "="
    assert value == "上海市"
    assert evidence


def test_runtime_enum_lookup_covers_values_missing_from_profile_samples():
    table = TableDef("customer", "客户信息表", "test", [
        {
            "name": "HOUSEADD", "type": "varchar", "comment": "户籍地址",
            "category": "text", "examples": ["上海市某路123号"],
        },
        {
            "name": "HHDIST", "type": "varchar", "comment": "户籍省份",
            "category": "enum", "examples": ["四川省", "广东省", "上海市"],
        },
    ])
    options = [
        FieldCandidate(
            table_name="customer", column_name="HOUSEADD", column_comment="户籍地址",
            query_slot="户籍地址", final_score=0.9,
        ),
        FieldCandidate(
            table_name="customer", column_name="HHDIST", column_comment="户籍省份",
            query_slot="户籍地址", final_score=0.55,
        ),
    ]

    def lookup(candidate, _column, value):
        assert value == "北京"
        return ["北京市"] if candidate.column_name == "HHDIST" else []

    selected, operator, value, evidence = ground_text_binding(
        "北京", options, [table], value_lookup=lookup
    )

    assert selected.column_name == "HHDIST"
    assert operator == "="
    assert value == "北京市"
    assert evidence[0].startswith("实时值域验证")


def test_age_attribute_does_not_trigger_missing_time_range():
    rule = {
        "time_intent_keywords": ["时间", "月份", "季度", "周", "年", "月", "日期", "日"],
        "range_present_patterns": [r"\d{4}年"],
        "message": "请补充时间范围",
    }

    assert _check_time_range("返回客户姓名、性别、年龄和户籍地址", rule) is None
    assert _check_time_range("按月统计贷款金额", rule) == "请补充时间范围"


def test_generic_model_concepts_cannot_replace_explicit_filter_semantics():
    subject = SemanticSubject(id="customer", kind="entity", concept="客户")
    fallback = SemanticGraph(
        subjects=[subject],
        predicate=SemanticPredicate(
            atom_id="atom_1", predicate_type="comparison", subject_id="customer",
            concept="户籍地址", operator="=", value="上海",
            source_text="户籍地址是上海", confidence=0.96,
        ),
    )
    candidate = SemanticGraph(
        subjects=[subject],
        outputs=[SemanticOutput(
            id="output_1", subject_id="customer", concept="户籍地址",
            grounding_concept="customer.address", source_text="户籍地址",
        )],
        predicate=SemanticPredicate(
            atom_id="atom_model", predicate_type="comparison", subject_id="customer",
            concept="比较", grounding_concept="comparison", operator="等于", value="上海",
            source_text="户籍地址是上海", confidence=1.0,
        ),
    )

    merged = _prefer_complete_graph(candidate, fallback)

    assert merged.predicate is not None
    assert merged.predicate.concept == "户籍地址"
    assert merged.predicate.operator == "="
    assert merged.outputs[0].grounding_concept == "户籍地址"


def test_query_resolution_drops_premature_basic_info_expansion():
    subject = SemanticSubject(id="customer", kind="entity", concept="客户")
    fallback = SemanticGraph(subjects=[subject])
    candidate = SemanticGraph(
        subjects=[subject],
        outputs=[
            SemanticOutput(
                id="model_name", subject_id="customer", concept="客户姓名",
                grounding_concept="customer.name", source_text="基本信息",
            ),
            SemanticOutput(
                id="model_id", subject_id="customer", concept="身份证号",
                grounding_concept="customer.id_number", source_text="基本信息",
            ),
        ],
    )

    merged = _prefer_complete_graph(candidate, fallback)

    assert merged.outputs == []
