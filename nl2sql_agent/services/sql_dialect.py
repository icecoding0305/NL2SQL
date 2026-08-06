"""SQL 方言封装:基于 sqlglot 的解析、AST 校验、危险操作检测与行级权限注入。

用 AST 而非正则,是规格的硬要求(字段幻觉与危险操作都要结构上可判定)。
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp


class SqlDialect:
    def __init__(self, dialect: str = "postgres"):
        self.dialect = dialect

    def parse(self, sql: str, dialect: str | None = None):
        return sqlglot.parse_one(sql, read=dialect or self.dialect)

    def to_sql(self, expr, dialect: str | None = None) -> str:
        return expr.sql(dialect=dialect or self.dialect)

    # ---------- 危险操作检测(结构上判定,非关键字黑名单) ----------
    _SAFE_TOP = (exp.Select, exp.Subquery, exp.Union, exp.Except, exp.Intersect)

    def is_dangerous(self, expr) -> str | None:
        """非 SELECT 类顶层节点返回其类型名(危险),否则 None。

        DROP/DELETE/UPDATE/TRUNCATE/INSERT/ALTER 等在 sqlglot 中解析为
        exp.Drop / exp.Delete / exp.Update / exp.TruncateTable 等,
        都不在 _SAFE_TOP 内,因此危险操作在 AST 层面直接被识别。
        """
        if not isinstance(expr, self._SAFE_TOP):
            return type(expr).__name__
        # 顶层是 SELECT 但内部出现写操作命令,同样拦截
        for cmd in expr.find_all(exp.Command, exp.DDL):
            if not isinstance(cmd, self._SAFE_TOP):
                return type(cmd).__name__
        return None

    # ---------- AST 信息抽取 ----------
    def extract_tables(self, expr) -> list[str]:
        """按出现顺序去重的表名(排除 CTE 别名)。"""
        cte_names = {cte.alias_or_name for cte in expr.find_all(exp.CTE)}
        out: list[str] = []
        for t in expr.find_all(exp.Table):
            name = t.name
            if name and name not in cte_names and name not in out:
                out.append(name)
        return out

    def extract_columns(self, expr) -> list[tuple[str | None, str]]:
        """返回 [(限定表名或 None, 列名)],用于字段幻觉校验与敏感字段识别。"""
        out: list[tuple[str | None, str]] = []
        for c in expr.find_all(exp.Column):
            name = c.name
            if name == "*":
                continue
            out.append((c.table or None, name))
        return out

    def is_select_column(self, expr, table: str | None, column: str) -> bool:
        """判断某列是否出现在 SELECT 投影中(用于"导出"敏感判定)。

        table 传 None 表示不限表名,只看列名。
        """
        for c in expr.find_all(exp.Column):
            if c.name == column and (table is None or c.table is None or c.table == table):
                return True
        return False

    def is_column_in_aggregate(self, expr, column: str) -> bool:
        """判断某列是否出现在聚合函数参数内(SUM/AVG/COUNT 等)。"""
        for agg in expr.find_all(exp.AggFunc):
            for c in agg.find_all(exp.Column):
                if c.name == column:
                    return True
        return False

    # ---------- 行级权限注入 ----------
    def inject_row_level_filter(
        self, expr, dialect: str | None, column: str, table: str, values: list[str]
    ) -> str:
        """在 WHERE 中追加 `<table>.<column> IN (values...)`(代码逻辑,不交给 LLM)。

        注意:sqlglot 的 where() 默认 copy=True,返回新表达式,必须重新赋值。
        """
        if not values:
            return expr.sql(dialect=dialect or self.dialect)
        col = exp.column(column, table=table)
        cond = exp.In(this=col, expressions=[exp.Literal.string(v) for v in values])
        has_where = bool(expr.args.get("where"))
        expr = expr.where(cond, append=has_where, dialect=dialect or self.dialect)
        return expr.sql(dialect=dialect or self.dialect)

    # ---------- 未聚合强制 LIMIT ----------
    def has_aggregate_or_limit(self, expr) -> bool:
        if expr.args.get("limit"):
            return True
        if expr.args.get("group"):
            return True
        if expr.args.get("distinct"):
            return True
        if expr.find_all(exp.AggFunc):
            return True
        return False

    def enforce_limit(self, expr, limit: int, dialect: str | None = None) -> str:
        expr = expr.copy()
        expr.limit(limit)
        return expr.sql(dialect=dialect or self.dialect)


# ---------- 方言提示词(注入 SQL 生成 prompt,帮助 LLM 适配目标数据库) ----------

_DIALECT_TIPS: dict[str, str] = {
    "mysql": (
        "MySQL 语法要点:\n"
        "- 标识符用反引号 ` 包裹(如 `table_name`.`column_name`)\n"
        "- LIMIT offset, count (如 LIMIT 10, 20 表示跳过 10 行取 20 行)\n"
        "- 字符串连接用 CONCAT(), 日期格式化用 DATE_FORMAT()\n"
        "- 当前日期 CURDATE(), 当前时间 NOW()\n"
    ),
    "postgres": (
        "PostgreSQL 语法要点:\n"
        "- 标识符用双引号 \" 包裹(如 \"table_name\".\"column_name\")\n"
        "- LIMIT count OFFSET offset (如 LIMIT 20 OFFSET 10)\n"
        "- 字符串连接用 ||, 日期格式化用 TO_CHAR()\n"
        "- 当前日期 CURRENT_DATE, 当前时间 NOW()\n"
    ),
    "postgresql": (
        "PostgreSQL 语法要点:\n"
        "- 标识符用双引号 \" 包裹(如 \"table_name\".\"column_name\")\n"
        "- LIMIT count OFFSET offset (如 LIMIT 20 OFFSET 10)\n"
        "- 字符串连接用 ||, 日期格式化用 TO_CHAR()\n"
        "- 当前日期 CURRENT_DATE, 当前时间 NOW()\n"
    ),
}


def dialect_tips(dialect: str) -> str:
    """返回注入 SQL 生成 prompt 的方言语法速查提示。"""
    return _DIALECT_TIPS.get(dialect.lower(), f"请使用 {dialect} 标准 SQL 语法。")
