"""LLM 层测试:provider 选择、DeepSeek 客户端、结构化输出解析(不联网)。"""

from __future__ import annotations

import pytest

from nl2sql_agent.services.llm import (
    AnthropicLLMClient,
    DeepSeekLLMClient,
    EnvConfigError,
    SQLResult,
    build_llm,
    extract_json,
)


# ---------------- provider 选择 ----------------

def test_build_llm_deepseek_when_provider_set(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    llm = build_llm()
    assert isinstance(llm, DeepSeekLLMClient)
    assert llm.model == "deepseek-chat"


def test_build_llm_deepseek_auto_when_key_set(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    assert isinstance(build_llm(), DeepSeekLLMClient)


def test_build_llm_anthropic_by_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    assert isinstance(build_llm(), AnthropicLLMClient)


def test_deepseek_requires_key_and_model(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-chat")
    with pytest.raises(EnvConfigError):
        DeepSeekLLMClient.from_env()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    with pytest.raises(EnvConfigError):
        DeepSeekLLMClient.from_env()


# ---------------- DeepSeek 客户端(假 OpenAI 兼容端点,不联网) ----------------

class _FakeMessage:
    def __init__(self, content: str | None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeCompletions:
    def __init__(self, contents: list[str], tool_support: bool = True):
        self._contents = list(contents)
        self.calls = 0
        self.tool_support = tool_support

    def create(self, **kwargs):
        self.calls += 1
        raw = self._contents.pop(0)
        msg = _FakeMessage(content=raw)
        if kwargs.get("tools") and self.tool_support:
            # 模拟 function calling:arguments 为 JSON 字符串
            fn = type("F", (), {"name": "emit_json", "arguments": raw})()
            msg = _FakeMessage(content=None, tool_calls=[type("TC", (), {"function": fn})()])
        return type("Resp", (), {"choices": [type("C", (), {"message": msg})()]})()


class _FakeChat:
    def __init__(self, contents: list[str], tool_support: bool = True):
        self.completions = _FakeCompletions(contents, tool_support)


class _FakeClient:
    def __init__(self, contents: list[str], tool_support: bool = True):
        self.chat = _FakeChat(contents, tool_support)


def _answer_schema():
    return {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}}


def test_deepseek_complete_and_json():
    # DeepSeek thinking 模式不支持 tool_choice → 恒走纯文本 + extract_json(容忍 markdown)
    client = DeepSeekLLMClient(_FakeClient([
        '{"sql": "SELECT 1", "used_tables": ["t"]}',
        '{"answer": 42}',
        '```json\n{"answer": 7}\n```',
    ]), model="deepseek-chat")

    out = client.complete_sql("生成 SQL")
    assert isinstance(out, SQLResult)
    assert out.sql == "SELECT 1"
    assert out.used_tables == ["t"]

    data = client.complete_json("返回 JSON", _answer_schema())
    assert data == {"answer": 42}

    # 容忍 markdown 代码块
    data2 = client.complete_json("返回 JSON", _answer_schema())
    assert data2 == {"answer": 7}

    assert client.client.chat.completions.calls == 3


def test_complete_json_rejects_schema_echo():
    # 模型回显 schema 定义(缺目标字段)→ 判定无效 → 重试后返回正确数据
    schema_echo = '{"type":"object","properties":{"answer":{"type":"integer"}},"required":["answer"]}'
    client = DeepSeekLLMClient(
        _FakeClient([schema_echo, '{"answer": 7}']),
        model="deepseek-chat",
    )
    data = client.complete_json("返回 JSON", _answer_schema())
    assert data == {"answer": 7}
    assert client.client.chat.completions.calls == 2


def test_complete_structured_validates_pydantic():
    from pydantic import BaseModel, Field

    class P(BaseModel):
        a: int = Field(gt=0)

    client = DeepSeekLLMClient(_FakeClient(['{"a": 1}']), model="deepseek-chat")
    obj = client.complete_structured("plan", P)
    assert isinstance(obj, P) and obj.a == 1


def test_complete_structured_retries_pydantic_nested_validation():
    from pydantic import BaseModel, ConfigDict

    class Metric(BaseModel):
        model_config = ConfigDict(extra="forbid")
        metric_name: str

    class Plan(BaseModel):
        metric_logic: Metric | None

    client = DeepSeekLLMClient(
        _FakeClient([
            '{"metric_logic":{"name":"total_amount"}}',
            '{"metric_logic":{"metric_name":"代偿金额"}}',
        ]),
        model="deepseek-chat",
    )
    obj = client.complete_structured("plan", Plan, retries=1)
    assert obj.metric_logic is not None
    assert obj.metric_logic.metric_name == "代偿金额"
    assert client.client.chat.completions.calls == 2


def test_complete_structured_retries_then_raises():
    from pydantic import BaseModel

    class P(BaseModel):
        a: int

    # 每轮先试 function calling(非法参数)→ 再试纯文本(非 JSON)→ 两轮后抛错
    client = DeepSeekLLMClient(
        _FakeClient(["不是 JSON", "仍是垃圾", "不是 JSON", "仍是垃圾"]),
        model="deepseek-chat",
    )
    with pytest.raises(ValueError, match="多次解析失败"):
        client.complete_structured("plan", P, retries=1)


# ---------------- 通用 JSON 提取 ----------------

def test_extract_json_robustness():
    import json

    # 容忍前后废话
    assert extract_json('前文 {"k": 1} 后文') == '{"k": 1}'
    # 用 json.dumps 生成规范(平衡)JSON,内含引号与花括号的字符串值
    obj = {"a": [1, {"b": "}"}]}
    expected = json.dumps(obj, ensure_ascii=False)  # '{"a": [1, {"b": "}"}]}'
    assert extract_json("```json\n" + expected + "\n```") == expected
    with pytest.raises(ValueError):
        extract_json("没有 JSON")
