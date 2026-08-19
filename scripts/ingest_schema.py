"""表结构入库入口(全量/增量)。

用法:
    uv run python scripts/ingest_schema.py --mode full --datasource nl2sql
    uv run python scripts/ingest_schema.py --mode incremental --datasource nl2sql

行为:
- 注释质量达标(或已有审核覆盖)的表 → 直接写入向量库(表级+字段级 collection)
- 注释缺失/覆盖率不足的表 → LLM 生成候选注释草稿(样例脱敏)→ 进审核队列,不入向量库
- 增量模式:structure_hash 变化或有覆盖层的表才处理;被删的表从向量库清理
- 先生成扩展版 M-Schema:raw/effective 快照 + data/schema/{datasource}/m-schema.json
- 运行时直接读取 effective M-Schema，不再生成或依赖 schema_catalog.yaml

增量模式建议配合定时任务按天跑一次。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nl2sql_agent.services.config_loader import ConfigLoader  # noqa: E402
from nl2sql_agent.services.deps import CONFIG_DIR, build_deps, load_env  # noqa: E402
from nl2sql_agent.services.schema_ingest.diff_sync import sync  # noqa: E402
from nl2sql_agent.services.schema_ingest.review_queue import ReviewStore  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="表结构入库(全量/增量)")
    parser.add_argument("--mode", choices=["full", "incremental"], default="full")
    parser.add_argument("--datasource", default=None, help="元数据分片名(默认取数据库名)")
    parser.add_argument("--schema-name", default="nl2sql", help="MySQL 库名")
    parser.add_argument("--business-line", default="risk_mart", help="系统命名空间(risk_mart/dw/core)")
    parser.add_argument("--review-db", default="data/schema_ingest.db")
    args = parser.parse_args()

    load_env()
    deps = build_deps()
    datasource = args.datasource or getattr(deps.executor, "conn_kwargs", {}).get("database") or args.schema_name

    config = ConfigLoader(CONFIG_DIR).load("schema_ingest.yaml") or {}
    store = ReviewStore(Path(args.review_db))

    report = sync(
        datasource, args.schema_name, deps, config, store,
        mode=args.mode, business_line=args.business_line,
    )
    print(
        f"[{args.mode}] datasource={datasource} "
        f"入库={report.ingested} 待审核={report.queued} 跳过={report.skipped} 删除={report.removed}"
    )
    for e in report.errors:
        print(f"  [错误] {e}")
    if report.queued:
        print(f"提示:有 {report.queued} 个待审核条目,运行:")
        print("  uv run python scripts/review_schema_comments.py list")
    print(f"待审核总数: {store.pending_count(datasource)}")
    if report.mschema_path:
        print(f"M-Schema:{report.mschema_path} (snapshot={report.snapshot_id})")


if __name__ == "__main__":
    main()
