from nl2sql_agent.services.text_encoding import (
    clean_semantic_text,
    is_likely_mojibake,
    normalize_query_payload,
    repair_mojibake,
)


QUERY = "查询贷款金额超过 1000 元且逾期本金余额大于 0"


def test_repairs_gbk_bytes_decoded_as_latin1():
    broken = QUERY.encode("gbk").decode("latin-1")
    assert repair_mojibake(broken) == QUERY


def test_repairs_utf8_bytes_decoded_as_latin1():
    broken = QUERY.encode("utf-8").decode("latin-1")
    assert repair_mojibake(broken) == QUERY


def test_preserves_normal_chinese_english_and_identifiers():
    assert repair_mojibake(QUERY) == QUERY
    assert repair_mojibake("LOAN_AMT > 1000") == "LOAN_AMT > 1000"
    assert repair_mojibake("café") == "café"


def test_normalizes_conversation_history_recursively():
    broken = QUERY.encode("gbk").decode("latin-1")
    payload = [{"role": "user", "content": broken}]
    assert normalize_query_payload(payload) == [{"role": "user", "content": QUERY}]


def test_irreversible_mojibake_is_dropped_from_semantic_metadata():
    broken = "\ufffd\ufffd\ufffd\ufffd\ufffd\ufffd"
    assert is_likely_mojibake(broken) is True
    assert clean_semantic_text(broken) == ""
    assert clean_semantic_text("贷款金额") == "贷款金额"
