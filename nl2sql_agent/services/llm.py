"""LLM 调用层(可插拔 provider)。

- 抽象基类 BaseLLMClient:complete_json / complete_structured / complete_sql 基于 complete 实现
- AnthropicLLMClient:Anthropic Messages API
- DeepSeekLLMClient:DeepSeek 的 OpenAI 兼容接口(base_url 可配)
- build_llm():按环境变量选择 provider

选择规则(优先级):
1. LLM_PROVIDER=deepseek → DeepSeek;LLM_PROVIDER=anthropic → Anthropic
2. 未显式设置时,配置了 DEEPSEEK_API_KEY 则用 DeepSeek,否则 Anthropic

model 名一律从环境变量读取,不硬编码。
测试环境用 FakeLLM 替换(见 nl2sql_agent.testing)。
"""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from pydantic import BaseModel


class EnvConfigError(RuntimeError):
    pass


@dataclass
class SQLResult:
    sql: str
    used_tables: list[str] = field(default_factory=list)


def extract_json(text: str) -> str:
    """从 LLM 输出中提取第一个完整 JSON 对象(容忍 markdown 代码块与前后废话)。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("LLM 输出中没有 JSON 对象")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    raise ValueError("LLM 输出的 JSON 对象不完整")


class BaseLLMClient(ABC):
    """LLM 客户端统一接口。子类实现 complete 与 _complete_tool。"""

    @abstractmethod
    def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        """纯文本补全。"""

    @abstractmethod
    def _complete_tool(self, prompt: str, name: str, description: str, schema: dict) -> dict | None:
        """通过 function calling 强制结构化输出;模型未走工具调用时返回 None。"""

    # ---------- 结构化输出(优先 function calling,纯文本解析兜底) ----------
    def complete_json(self, prompt: str, schema: dict, retries: int = 2) -> dict:
        """要求模型输出符合 schema 的 JSON。

        优先用 function calling 强制模型走工具调用(避免"回显 schema 定义"的偶发问题),
        模型不支持/未走工具时回退到纯文本 + 手动抽取,解析失败带错误重试。
        """
        props = schema.get("properties", {})
        keys = list(schema.get("required") or props.keys())
        field_hint = ", ".join(f'"{k}"' for k in keys)
        instruction = (
            "\n\n只输出一个 JSON 对象,不要输出任何其它文字、不要用 markdown 代码块、不要解释。\n"
            f"这个对象必须包含以下字段: {field_hint}。\n"
            "字段值必须是具体的实际内容(数据/数组/数字),不要输出字段的结构定义或示例说明。"
        )
        struct_keys = {"type", "properties", "required", "additionalProperties", "$schema", "title"}

        def _valid(data: dict) -> bool:
            """判定是否"回显了 schema 定义"或缺少目标字段(这两种都无效,应重试)。"""
            if not isinstance(data, dict):
                return False
            has_target = bool(set(keys) & set(data.keys()))
            if not has_target:
                return False
            # 键里出现 schema 结构特征且缺失目标字段 → 模型把结构定义回显了
            if struct_keys & set(data.keys()) and not has_target:
                return False
            return True

        last_err: Exception | None = None
        for _ in range(retries + 1):
            # 1) function calling(可靠;DeepSeek thinking 模式不支持则跳过)
            try:
                data = self._complete_tool(prompt, "emit_json", "输出符合给定 JSON 结构的实际数据", schema)
                if data is not None and _valid(data):
                    return data
            except Exception as e:  # noqa: BLE001
                last_err = e
            # 2) 纯文本兜底(解析后校验必填字段,回显即重试)
            try:
                text = self.complete(prompt + instruction)
                data = json.loads(extract_json(text))
                if not _valid(data):
                    raise ValueError(f"输出缺少目标字段 {keys}(疑似回显 schema 定义)")
                return data
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise ValueError(f"LLM 结构化输出多次解析失败: {last_err}")

    def complete_structured(self, prompt: str, model: type[BaseModel], retries: int = 2) -> BaseModel:
        """返回符合 Pydantic schema 的实例，并让嵌套校验错误参与模型重试。

        ``complete_json`` 只能检查顶层 JSON 形状；过去嵌套字段写错会在它返回后才失败，
        导致 retries 实际没有覆盖 Pydantic 校验。这里把解析和 model_validate 放进同一循环。
        """
        schema = model.model_json_schema()
        attempt_prompt = prompt
        last_err: Exception | None = None
        previous_data: dict | None = None
        for _ in range(retries + 1):
            previous_data = None
            try:
                previous_data = self.complete_json(attempt_prompt, schema, retries=0)
                return model.model_validate(previous_data)
            except Exception as e:  # noqa: BLE001
                last_err = e
                previous_text = json.dumps(
                    previous_data, ensure_ascii=False, default=str
                )[:4000] if previous_data is not None else "无可解析 JSON"
                attempt_prompt = (
                    prompt
                    + "\n\n上一轮结构化输出未通过 Pydantic 校验。"
                    + "请根据错误修正字段名、必填字段和嵌套结构，不要重复原输出。\n"
                    + f"上一轮输出: {previous_text}\n"
                    + f"校验错误: {e}\n"
                    + "必须严格符合以下 JSON Schema:\n"
                    + json.dumps(schema, ensure_ascii=False, default=str)
                )
        raise ValueError(f"LLM 结构化输出多次解析失败: {last_err}")

    def complete_sql(self, prompt: str, retries: int = 2) -> SQLResult:
        """要求模型同时返回 SQL 与用到的表清单(供模块 8 交叉比对)。"""
        schema = {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "used_tables": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["sql", "used_tables"],
        }
        data = self.complete_json(prompt, schema, retries=retries)
        return SQLResult(
            sql=str(data.get("sql", "")),
            used_tables=[str(t) for t in data.get("used_tables", [])],
        )

    def summarize(self, query: str, rows: list[dict], retries: int = 1) -> str:
        prompt = (
            "把下面的查询结果整理成一段简洁的中文摘要,保留关键数字与单位,"
            "不要编造结果里没有的数字:\n"
            f"查询: {query}\n"
            f"结果(前 {min(len(rows), 50)} 行):\n"
            f"{json.dumps(rows[:50], ensure_ascii=False, default=str)}"
        )
        return self.complete(prompt, max_tokens=500)


class AnthropicLLMClient(BaseLLMClient):
    """Anthropic Messages API。"""

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    @classmethod
    def from_env(cls) -> "AnthropicLLMClient":
        import anthropic

        model = os.getenv("ANTHROPIC_MODEL")
        if not model:
            raise EnvConfigError(
                "环境变量 ANTHROPIC_MODEL 未设置(model 名不允许硬编码,请从环境变量读取)"
            )
        api_key = os.getenv("ANTHROPIC_API_KEY")
        return cls(anthropic.Anthropic(api_key=api_key), model=model)

    def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )

    def _complete_tool(self, prompt: str, name: str, description: str, schema: dict) -> dict | None:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=2000,
            tools=[{"name": name, "description": description, "input_schema": schema}],
            tool_choice={"type": "tool", "name": name},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == name:
                return block.input
        return None


class DeepSeekLLMClient(BaseLLMClient):
    """DeepSeek 的 OpenAI 兼容接口。

    默认 base_url=https://api.deepseek.com,可用 DEEPSEEK_BASE_URL 覆盖。
    """

    def __init__(self, client, model: str):
        self.client = client
        self.model = model

    @classmethod
    def from_env(cls) -> "DeepSeekLLMClient":
        import openai

        api_key = os.getenv("DEEPSEEK_API_KEY")
        model = os.getenv("DEEPSEEK_MODEL")
        if not api_key:
            raise EnvConfigError("环境变量 DEEPSEEK_API_KEY 未设置")
        if not model:
            raise EnvConfigError(
                "环境变量 DEEPSEEK_MODEL 未设置(model 名不允许硬编码,如 deepseek-chat)"
            )
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        return cls(openai.OpenAI(api_key=api_key, base_url=base_url), model=model)

    def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp.choices[0].message.content
        return content or ""

    def _complete_tool(self, prompt: str, name: str, description: str, schema: dict) -> dict | None:
        # DeepSeek 的 thinking/reasoning 模式不支持 tool_choice(会 400),
        # 这里统一走纯文本路径(complete_json 里的回显检测负责校验)。Anthropic 保留 tool use。
        return None


def _load_nodes_config() -> dict:
    from pathlib import Path

    from nl2sql_agent.services.config_loader import ConfigLoader

    cfg_dir = Path(__file__).resolve().parent.parent / "config"
    return (ConfigLoader(cfg_dir).load("model_config.yaml") or {}).get("nodes", {})


def get_model_for_node(node_key: str) -> BaseLLMClient:
    """按 config/model_config.yaml 的 nodes.<node_key> 选模型(离线任务用更便宜模型)。

    未配置该节点 → 回退主模型。
    """
    cfg = _load_nodes_config().get(node_key, {})
    model = cfg.get("model")
    if not model:
        return build_llm()
    import openai

    return DeepSeekLLMClient(
        openai.OpenAI(
            api_key=cfg.get("api_key") or os.getenv("DEEPSEEK_API_KEY"),
            base_url=cfg.get("base_url")
            or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        ),
        model=model,
    )


def _is_deepseek() -> bool:
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    return provider == "deepseek" or (not provider and os.getenv("DEEPSEEK_API_KEY"))


def build_llm() -> BaseLLMClient:
    """主模型(按环境变量选择 provider),用于计划生成、结果解释等思考类任务。"""
    return DeepSeekLLMClient.from_env() if _is_deepseek() else AnthropicLLMClient.from_env()


def build_sql_llm() -> BaseLLMClient | None:
    """SQL 专用模型(可选)。配置后,模块 7(SQL 生成)用它;未配置返回 None 回退主模型。

    两种配置方式,优先级:
    1. 独立模型(可指向任意 OpenAI 兼容端点,如千问/DashScope):
       SQL_MODEL + SQL_API_KEY + SQL_BASE_URL
    2. 同主 provider 的模型名:DEEPSEEK_SQL_MODEL / ANTHROPIC_SQL_MODEL
    model 名一律从环境变量读取,不硬编码。
    """
    import openai

    # 1) 完全独立的 SQL 模型(千问等其它 OpenAI 兼容端点)
    sql_model = os.getenv("SQL_MODEL")
    sql_base = os.getenv("SQL_BASE_URL")
    sql_key = os.getenv("SQL_API_KEY")
    if sql_model:
        if not sql_base:
            raise EnvConfigError("配置了 SQL_MODEL,但缺少 SQL_BASE_URL(独立模型需指定端点)")
        if not sql_key:
            raise EnvConfigError("配置了 SQL_MODEL,但缺少 SQL_API_KEY")
        return DeepSeekLLMClient(
            openai.OpenAI(api_key=sql_key, base_url=sql_base), model=sql_model
        )
    # 2) 同主 provider 的 SQL 模型名
    if _is_deepseek():
        model = os.getenv("DEEPSEEK_SQL_MODEL")
        if not model:
            return None
        return DeepSeekLLMClient(
            openai.OpenAI(
                api_key=os.getenv("DEEPSEEK_API_KEY"),
                base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            ),
            model=model,
        )
    model = os.getenv("ANTHROPIC_SQL_MODEL")
    if not model:
        return None
    import anthropic

    return AnthropicLLMClient(
        anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")), model=model
    )
