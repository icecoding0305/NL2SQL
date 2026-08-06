"""旧版 Schema 导入命令（已弃用）。

用法:
    uv run python scripts/import_schema_from_db.py

步骤:
1. 连数据库查 INFORMATION_SCHEMA(表名/表注释/列名/列类型/列注释)
2. 自动标注敏感字段(列名/注释含 身份证/证件/手机 等)
3. 写入 schema_catalog.yaml(默认按共享表模型:shared: true)
4. 用当前 embedding 全量重建向量索引

请改用 ``scripts/ingest_schema.py --mode full``。该命令先生成 M-Schema，
再自动投影 catalog，避免形成第二事实源。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nl2sql_agent.services.deps import CONFIG_DIR, build_deps, load_env  # noqa: E402
from nl2sql_agent.services.schema_importer import refresh_catalog_from_db  # noqa: E402


def main() -> None:
    print("[已弃用] 请改用: uv run python scripts/ingest_schema.py --mode full")
    load_env()
    deps = build_deps()
    # 从执行器拿数据库名(MySQLExecutor 的 conn_kwargs.database),兜底 nl2sql
    database = getattr(deps.executor, "conn_kwargs", {}).get("database") or "nl2sql"
    n = refresh_catalog_from_db(deps.executor, database, CONFIG_DIR)
    print(f"已从库导入 {n} 张表 → schema_catalog.yaml")

    # 重建 deps(新 schema_catalog 生效)并重建 embedding 索引
    deps2 = build_deps()
    deps2.vector_store.rebuild_index()
    print(f"已用真 embedding 重建向量索引({type(deps2.vector_store).__name__})")


if __name__ == "__main__":
    main()
