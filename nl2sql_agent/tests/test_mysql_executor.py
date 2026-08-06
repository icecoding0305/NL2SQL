"""MySQL 执行器测试(不联网):URL 解析、EXPLAIN 行数提取。"""

from __future__ import annotations

import pytest

from nl2sql_agent.services.executor import MySQLExecutor, estimate_max_rows


def test_mysql_url_parsing():
    ex = MySQLExecutor.from_url(
        "mysql://fbrisk:secret@mysql5.sqlpub.com:3310/nl2sql?charset=utf8mb4"
    )
    assert ex.conn_kwargs["host"] == "mysql5.sqlpub.com"
    assert ex.conn_kwargs["port"] == 3310
    assert ex.conn_kwargs["user"] == "fbrisk"
    assert ex.conn_kwargs["password"] == "secret"
    assert ex.conn_kwargs["database"] == "nl2sql"
    assert ex.conn_kwargs["charset"] == "utf8mb4"


def test_mysql_url_default_port():
    ex = MySQLExecutor.from_url("mysql://u:p@localhost/db")
    assert ex.conn_kwargs["port"] == 3306
    assert ex.conn_kwargs["database"] == "db"


def test_mysql_url_rejects_wrong_scheme():
    with pytest.raises(ValueError):
        MySQLExecutor.from_url("postgresql://u:p@h/db")


def test_estimate_max_rows_recursive():
    # 单表
    assert estimate_max_rows({"query_block": {"table": {"rows": 123}}}) == 123
    # 嵌套连接取最大
    plan = {
        "query_block": {
            "nested_loop": [
                {"table": {"rows": 10}},
                {"table": {"rows": 500}},
            ]
        }
    }
    assert estimate_max_rows(plan) == 500
    # 非法值忽略
    assert estimate_max_rows({"table": {"rows": "N/A"}}) == 0
    # 空 plan
    assert estimate_max_rows({}) == 0
