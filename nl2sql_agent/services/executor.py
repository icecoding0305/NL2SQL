"""沙箱执行器(模块 10 的后端)。

- PostgresExecutor:只读账号 + READ ONLY 事务 + statement_timeout
- MySQLExecutor:READ ONLY 事务 + MAX_EXECUTION_TIME 超时 + EXPLAIN FORMAT=JSON
- InMemoryExecutor:测试/离线用,返回按表预置的行,支持模拟失败与空结果
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


@dataclass
class ExplainResult:
    estimated_rows: int


class SQLExecutor(ABC):
    @abstractmethod
    def explain(self, sql: str) -> ExplainResult: ...

    @abstractmethod
    def execute(self, sql: str, timeout_seconds: int = 30) -> list[dict]: ...


def estimate_max_rows(plan: dict) -> int:
    """从 EXPLAIN FORMAT=JSON 的 plan 里递归取最大的 rows 估值(偏保守)。

    嵌套子查询/连接时取最大值,作为"预估扫描行数"上限,避免低估风险。
    """
    total = 0
    rows = plan.get("rows")
    if rows is not None:
        try:
            total = max(total, int(rows))
        except (TypeError, ValueError):
            pass
    for value in plan.values():
        if isinstance(value, dict):
            total = max(total, estimate_max_rows(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    total = max(total, estimate_max_rows(item))
    return total


class PostgresExecutor(SQLExecutor):
    """只读执行:read-only 账号 + 事务 READ ONLY,超时由 statement_timeout 控制。"""

    def __init__(self, conninfo: str):
        import psycopg  # 延迟导入

        self._psycopg = psycopg
        self.conninfo = conninfo

    def _connect(self):
        return self._psycopg.connect(self.conninfo)

    def explain(self, sql: str) -> ExplainResult:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"EXPLAIN (FORMAT JSON) {sql}")
            plan = cur.fetchone()[0][0]["Plan"]
            return ExplainResult(estimated_rows=int(plan.get("Plan Rows", 0)))

    def execute(self, sql: str, timeout_seconds: int = 30, params: tuple | None = None) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN TRANSACTION READ ONLY")
                cur.execute(f"SET LOCAL statement_timeout = {timeout_seconds * 1000}")
                cur.execute(sql, params)
                cols = [d.name for d in cur.description or []]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute("COMMIT")
                return rows


class MySQLExecutor(SQLExecutor):
    """MySQL 只读执行。

    - 事务级只读:START TRANSACTION READ ONLY(不依赖只读账号,执行层保证不写库)
    - 超时:SET SESSION MAX_EXECUTION_TIME(毫秒),超时视为 execution_error
    - EXPLAIN FORMAT=JSON 提取预估扫描行数(取各表最大估值)
    """

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        charset: str = "utf8mb4",
    ):
        self.conn_kwargs = dict(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset=charset,
            connect_timeout=15,
            autocommit=False,
        )

    @classmethod
    def from_url(cls, url: str) -> "MySQLExecutor":
        from urllib.parse import parse_qs, urlsplit

        u = urlsplit(url)
        if u.scheme != "mysql":
            raise ValueError(f"不是 MySQL URL: {url}")
        query = parse_qs(u.query)
        return cls(
            host=u.hostname or "localhost",
            port=u.port or 3306,
            user=u.username or "",
            password=u.password or "",
            database=(u.path or "").lstrip("/"),
            charset=query.get("charset", ["utf8mb4"])[0],
        )

    def _connect(self):
        import pymysql  # 延迟导入

        self._pymysql = pymysql
        conn = self._pymysql.connect(
            **self.conn_kwargs, cursorclass=pymysql.cursors.DictCursor
        )
        return conn

    def explain(self, sql: str) -> ExplainResult:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN FORMAT=JSON {sql}")
                row = cur.fetchone()
                if not row:
                    return ExplainResult(estimated_rows=0)
                plan_text = row[0] if isinstance(row, (tuple, list)) else next(iter(row.values()))
                plan = json.loads(plan_text)
                return ExplainResult(estimated_rows=estimate_max_rows(plan))

    def execute(self, sql: str, timeout_seconds: int = 30, params: tuple | None = None) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("START TRANSACTION READ ONLY")  # 只读事务,双保险
                cur.execute(
                    "SET SESSION MAX_EXECUTION_TIME = %s", (int(timeout_seconds * 1000),)
                )
                cur.execute(sql, params)
                rows = cur.fetchall()
                cur.execute("COMMIT")
                return [dict(r) for r in rows]


class InMemoryExecutor(SQLExecutor):
    """测试/演示用:按 SQL 里的第一张表返回预置数据。

    可配置:
    - explain_rows:EXPLAIN 预估行数(用于触发敏感判定/执行前拒绝)
    - fail_execute_times:前 N 次 execute 抛错(用于验证执行报错退回模块 7)
    - empty_result:恒返回空结果
    - fail_explain:EXPLAIN 抛错
    """

    def __init__(
        self,
        tables: dict[str, list[dict]] | None = None,
        explain_rows: int = 100,
        fail_execute_times: int = 0,
        empty_result: bool = False,
        fail_explain: bool = False,
    ):
        self.tables = tables or {}
        self.explain_rows = explain_rows
        self.fail_execute_times = fail_execute_times
        self.empty_result = empty_result
        self.fail_explain = fail_explain
        self.execute_calls = 0
        self.explain_calls = 0

    def explain(self, sql: str) -> ExplainResult:
        self.explain_calls += 1
        if self.fail_explain:
            raise RuntimeError("EXPLAIN 模拟失败(测试)")
        return ExplainResult(estimated_rows=self.explain_rows)

    def execute(self, sql: str, timeout_seconds: int = 30, params: tuple | None = None) -> list[dict]:
        self.execute_calls += 1
        if self.execute_calls <= self.fail_execute_times:
            raise RuntimeError("模拟执行失败(test-forced error)")
        if self.empty_result:
            return []
        try:
            expr = sqlglot.parse_one(sql)
            tbl = next((t.name for t in expr.find_all(exp.Table)), None)
        except Exception:  # noqa: BLE001
            tbl = None
        return list(self.tables.get(tbl, []))
