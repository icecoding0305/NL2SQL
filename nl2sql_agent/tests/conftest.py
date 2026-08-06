"""pytest 共享夹具:构造 Fake 双打下的 Deps 与编译好的图。

测试使用隔离的 config 副本,避免脚本(如 ingest_schema.py)改写真实 config 影响测试。
"""

from __future__ import annotations

import shutil

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from nl2sql_agent.graph import build_graph
from nl2sql_agent.services.deps import CONFIG_DIR
from nl2sql_agent.testing import build_test_deps, set_test_config_dir

# 测试基线 schema_catalog(单借据表,归属 risk_mart 系统),避免被 ingest 脚本改写的真实配置影响测试
_TEST_SCHEMA = {
    "risk_mart": {
        "tables": [
            {
                "name": "dwd_ar_loan_info",
                "comment": "协议-贷款借据信息-贷款借据信息表",
                "business_line": "risk_mart",
                "shared": False,
                "columns": [
                    {"name": "LOAN_NO", "type": "varchar", "comment": "借据编码"},
                    {"name": "PRD_CODE", "type": "varchar", "comment": "产品编码"},
                    {"name": "LOAN_STATUS", "type": "varchar", "comment": "贷款状态"},
                    {"name": "OVD_BAL", "type": "decimal", "comment": "逾期本金余额"},
                    {"name": "PRIN_BAL", "type": "decimal", "comment": "贷款本金余额"},
                    {"name": "NORMAL_BAL", "type": "decimal", "comment": "正常本金余额"},
                    {"name": "LOAN_AMT", "type": "decimal", "comment": "贷款金额"},
                    {"name": "IDNUM", "type": "varchar", "comment": "证件号码", "sensitive": True},
                    {"name": "NAME", "type": "varchar", "comment": "客户姓名"},
                    {"name": "START_DATE", "type": "date", "comment": "借款开始日期"},
                    {"name": "PLATFORM_CODE", "type": "varchar", "comment": "平台代码"},
                ],
            }
        ]
    }
}


@pytest.fixture(scope="session", autouse=True)
def _isolated_config(tmp_path_factory):
    dst = tmp_path_factory.mktemp("testcfg")
    shutil.copytree(CONFIG_DIR, dst, dirs_exist_ok=True)
    # 用测试基线覆盖 schema_catalog(脚本改写真实配置不影响测试)
    (dst / "schema_catalog.yaml").write_text(
        yaml.safe_dump(_TEST_SCHEMA, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    set_test_config_dir(dst)
    yield
    set_test_config_dir(None)


@pytest.fixture
def deps():
    return build_test_deps()


@pytest.fixture
def graph(deps):
    return build_graph(deps, checkpointer=InMemorySaver())


def make_input(query: str, user_id: str = "u_risk", data_scope=None):
    # data_scope = 用户可访问的系统(业务线按系统维度),如 risk_mart(风险数据集市)/ dw / core
    return {
        "user_query": query,
        "user_id": user_id,
        "data_scope": data_scope or ["risk_mart"],
    }
