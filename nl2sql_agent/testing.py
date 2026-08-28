"""测试/离线用双打:脚本化 FakeLLM 与依赖装配。

不依赖真实 LLM 与数据库,用于单元测试、验收测试与 eval 回归。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from nl2sql_agent.services.config_loader import ConfigLoader
from nl2sql_agent.services.deps import CONFIG_DIR, AppConfig, Deps
from nl2sql_agent.services.embedding.router import fake_embedding
from nl2sql_agent.services.executor import InMemoryExecutor, SQLExecutor
from nl2sql_agent.services.few_shot_store import FewShotStore
from nl2sql_agent.services.prompt_loader import PromptLoader
from nl2sql_agent.services.schema_catalog import SchemaCatalog
from nl2sql_agent.services.sql_dialect import SqlDialect
from nl2sql_agent.services.term_mapping import TermMappingService
from nl2sql_agent.services.vector_store import InMemoryVectorStore, VectorStoreAdapter
from nl2sql_agent.services.llm import SQLResult


@dataclass
class FakeLLM:
    """按 prompt 正则匹配返回脚本化的 SQL / 计划。

    - sql_rules: (pattern, responder) 列表;responder 可为 SQLResult 或 callable(llm)->SQLResult
    - plan_rules: (pattern, plan_dict) 列表
    规则按顺序匹配,取第一个命中。
    """

    sql_rules: list[tuple[str, Any]] = field(default_factory=list)
    plan_rules: list[tuple[str, dict]] = field(default_factory=list)
    sql_calls: int = 0
    plan_calls: int = 0
    last_plan_prompt: str = ""
    summarize_template: str = "查询「{query}」共返回 {n} 行结果。"

    def add_sql(self, pattern: str, responder):
        self.sql_rules.append((pattern, responder))

    def add_plan(self, pattern: str, plan: dict):
        self.plan_rules.append((pattern, plan))

    def complete_sql(self, prompt: str, retries: int = 2) -> SQLResult:
        self.sql_calls += 1
        for pattern, responder in self.sql_rules:
            if re.search(pattern, prompt) or re.search(pattern, self.last_plan_prompt):
                return responder(self) if callable(responder) else responder
        raise ValueError("FakeLLM: 没有匹配的 SQL 规则")

    def complete_structured(self, prompt: str, model: type[BaseModel], retries: int = 2) -> BaseModel:
        self.plan_calls += 1
        if model.__name__ == "QueryPlan":
            self.last_plan_prompt = prompt
        for pattern, plan in self.plan_rules:
            if re.search(pattern, prompt):
                data = dict(plan)
                if model.__name__ == "QueryPlan":
                    atom_ids = list(dict.fromkeys(
                        atom for atom in re.findall(r'"atom_id"\s*:\s*"([^"]+)"', prompt)
                        if atom != "boolean_root"
                    ))
                    data.setdefault("covered_atom_ids", atom_ids)
                    filters = [dict(item) for item in data.get("filters", [])]
                    if filters and atom_ids:
                        filters[0].setdefault("source_atom_ids", atom_ids)
                        data["filters"] = filters
                    metric = data.get("metric_logic")
                    if metric and atom_ids:
                        data["metric_logic"] = {**metric, "source_atom_ids": atom_ids}
                return model.model_validate(data)
        if model.__name__ == "QueryPlan":
            query_schema_table = re.search(
                r'"tables"\s*:\s*\[\s*\{\s*"name"\s*:\s*"([A-Za-z0-9_]+)"',
                prompt,
            )
            tables = [query_schema_table.group(1)] if query_schema_table else []
            if not tables:
                tables = re.findall(r'"table_name"\s*:\s*"([A-Za-z0-9_]+)"', prompt)
            if not tables:
                tables = [
                    value
                    for value in re.findall(r'"table"\s*:\s*"([A-Za-z0-9_]+)"', prompt)
                    if value.casefold() not in {"table", "unknown"}
                ]
            if not tables:
                # Prompt formats may represent Query M-Schema as text instead
                # of JSON objects. Test schemas use warehouse-style physical
                # identifiers; prefer those over contract placeholders.
                tables = list(dict.fromkeys(re.findall(
                    r"\b(?:dwd|dws|app)_[A-Za-z0-9_]+\b", prompt, re.IGNORECASE
                )))
            atom_ids = list(dict.fromkeys(
                atom for atom in re.findall(r'"atom_id"\s*:\s*"([^"]+)"', prompt)
                if atom != "boolean_root"
            ))
            if tables:
                return model.model_validate({
                    "target_tables": [tables[0]],
                    "join_logic": [],
                    "filters": [],
                    "metric_logic": None,
                    "group_by": [],
                    "covered_atom_ids": atom_ids,
                    "confidence": 0.75,
                })
        raise ValueError("FakeLLM: 没有匹配的计划规则")

    def summarize(self, query: str, rows: list[dict], retries: int = 1) -> str:
        return self.summarize_template.format(query=query, n=len(rows))

    def complete(self, prompt: str, max_tokens: int = 2000) -> str:
        raise NotImplementedError("FakeLLM 不实现自由文本 complete")


def _loan_row(no, name, status, ovd, platform, amt=200000.0, prin=185000.0):
    return {
        "LOAN_NO": no, "CONT_NO": f"C-{no}", "CUST_ID": f"CU-{no}", "PRD_CODE": "P01",
        "NAME": name, "IDTYPE": "IDCARD", "IDNUM": f"110101199001011{abs(hash(no)) % 10}{no[-3:]}",
        "START_DATE": "2026-01-01", "END_DATE": "2027-01-01", "LOAN_STATUS": status,
        "TOTAL_TERMS": 12, "LOAN_AMT": amt, "PRIN_BAL": prin, "NORMAL_BAL": prin - ovd,
        "OVD_BAL": ovd, "PLATFORM_CODE": platform,
    }


FAKE_TABLES: dict[str, list[dict]] = {
    "dwd_ar_loan_info": [
        _loan_row("LN20260801001", "张伟", "正常", 0.0, "XXD"),
        _loan_row("LN20260801002", "李娜", "逾期", 30000.0, "XXD"),
        _loan_row("LN20260801003", "王强", "逾期", 5000.0, "ZJ"),
        _loan_row("LN20260801004", "赵敏", "正常", 0.0, "ZJ"),
    ],
}


def _default_rules() -> dict:
    """验收测试用的默认 FakeLLM 脚本,基于真实借据表 dwd_ar_loan_info。

    规则按 prompt 内容(含原始查询)正则匹配。
    """
    rules = {
        "sql_rules": [
            (r"借据笔数", SQLResult(
                "SELECT COUNT(*) AS cnt FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )),
            (r"逾期率", SQLResult(
                "SELECT SUM(CASE WHEN OVD_BAL > 0 THEN 1 ELSE 0 END) "
                "/ COUNT(*) AS overdue_rate FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )),
            (r"贷款余额|本金余额", SQLResult(
                "SELECT SUM(PRIN_BAL) AS total_balance FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )),
            (r"逾期本金", SQLResult(
                "SELECT SUM(OVD_BAL) AS total_ovd FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )),
            (r"证件号|身份证|IDNUM", SQLResult(
                "SELECT LOAN_NO, IDNUM FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )),
            (r"贷款金额|放款金额", SQLResult(
                "SELECT SUM(LOAN_AMT) AS total FROM dwd_ar_loan_info",
                ["dwd_ar_loan_info"],
            )),
        ],
        "plan_rules": [
            (r"逾期率", {
                "target_tables": ["dwd_ar_loan_info"],
                "join_logic": [],
                "filters": [],
                "metric_logic": {
                    "metric_name": "逾期率",
                    "definition": "逾期借据数 / 总借据数(OVD_BAL>0 视为逾期)",
                    "columns": ["OVD_BAL", "LOAN_STATUS"],
                },
                "group_by": [],
                "confidence": 0.9,
            }),
        ],
    }
    return rules


# 测试隔离配置目录(由 conftest 设置,避免测试受脚本改写真实 config 影响)
_TEST_CONFIG_DIR = None


def set_test_config_dir(path) -> None:
    global _TEST_CONFIG_DIR
    _TEST_CONFIG_DIR = path


def build_test_deps(
    *,
    llm: FakeLLM | None = None,
    executor: SQLExecutor | None = None,
    vector_store: VectorStoreAdapter | None = None,
    base_dir=None,
) -> Deps:
    """构造不依赖外部服务的 Deps(可替换 llm / executor / vector_store)。"""
    base_dir = base_dir or _TEST_CONFIG_DIR or CONFIG_DIR
    loader = ConfigLoader(base_dir)
    settings = loader.load("settings.yaml")
    config = AppConfig(
        dialect=settings.get("dialect", "postgres"),
        schema_search_top_k=int(settings.get("schema_search_top_k", 5)),
        execution_limit=int(settings.get("execution", {}).get("limit", 1000)),
        execution_timeout_seconds=int(settings.get("execution", {}).get("timeout_seconds", 30)),
        explain_row_threshold=int(settings.get("execution", {}).get("explain_row_threshold", 1_000_000)),
        row_level_filter=settings.get("row_level_filter", {"enabled": True, "column": "business_line"}),
        clarification_rules=loader.load("clarification_rules.yaml").get("clarification_rules", {}),
        complexity_rules=loader.load("complexity_rules.yaml").get("complexity_rules", {}),
        sensitive_rules=loader.load("sensitive_rules.yaml").get("sensitive_rules", {}),
    )
    term_mapping = TermMappingService(loader)
    catalog = SchemaCatalog(loader)
    llm = llm or FakeLLM(**_default_rules())
    executor = executor or InMemoryExecutor(tables=FAKE_TABLES)
    # 测试用确定性词袋 embedding(不下载模型、可复现)
    vector_store = vector_store or InMemoryVectorStore(catalog, embed=fake_embedding)
    return Deps(
        config=config,
        loader=loader,
        llm=llm,
        term_mapping=term_mapping,
        catalog=catalog,
        vector_store=vector_store,
        executor=executor,
        few_shot=FewShotStore(loader),
        sql=SqlDialect(config.dialect),
        prompts=PromptLoader(base_dir / "prompts"),
    )
