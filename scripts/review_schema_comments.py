"""表注释人工审核命令行工具。

用法:
    uv run python scripts/review_schema_comments.py list --status pending
    uv run python scripts/review_schema_comments.py show --id 123
    uv run python scripts/review_schema_comments.py approve --id 123 [--edit "改后的说明"]
    uv run python scripts/review_schema_comments.py reject --id 123 --reason "含义不明"

approve 会把注释写入覆盖层(schema_metadata_override),后续入库构建 embedding 文本
时优先取覆盖内容,而不是原生空注释;并更新对应审核条目为 approved。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nl2sql_agent.services.schema_ingest.review_queue import ReviewStore  # noqa: E402


def _show(rec: dict) -> str:
    target = rec["table_name"] + (f".{rec['column_name']}" if rec["column_name"] else " [表]")
    return (
        f"#{rec['id']} [{rec['status']}] {rec['datasource']}.{target}\n"
        f"  草稿注释: {rec['draft_comment'] or '(空)'}\n"
        f"  置信度: {float(rec.get('draft_confidence') or 0):.3f} | "
        f"校验问题: {rec.get('validation_errors') or []}\n"
        f"  审核人: {rec['reviewer'] or '-'} | 原因: {rec['reject_reason'] or '-'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="表注释人工审核")
    parser.add_argument("--review-db", default="data/schema_ingest.db")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出待审核条目")
    p.add_argument("--status", default="pending")
    p.add_argument("--datasource", default=None)

    p = sub.add_parser("show", help="查看单条")
    p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("approve", help="审核通过(写入覆盖层)")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--edit", default=None, help="覆盖草稿的最终注释")
    p.add_argument("--reviewer", default="cli")

    p = sub.add_parser("reject", help="驳回")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--reviewer", default="cli")

    args = parser.parse_args()

    store = ReviewStore(Path(args.review_db))

    if args.cmd == "list":
        rows = store.list_reviews(status=args.status, datasource=args.datasource)
        print(f"共 {len(rows)} 条(status={args.status}):\n")
        for r in rows:
            print(_show(r))
            print()

    elif args.cmd == "show":
        rec = store.get_review(args.id)
        print(_show(rec) if rec else f"#{args.id} 不存在")

    elif args.cmd == "approve":
        rec = store.get_review(args.id)
        if not rec:
            print(f"#{args.id} 不存在")
            return
        final = args.edit if args.edit is not None else rec["draft_comment"]
        if store.approve(args.id, final, args.reviewer):
            target = rec["table_name"] + (f".{rec['column_name']}" if rec["column_name"] else " [表]")
            print(f"已通过 #{args.id} {target} → 覆盖层: {final!r}")
        else:
            print(f"#{args.id} 不存在或状态不允许")

    elif args.cmd == "reject":
        if store.reject(args.id, args.reason, args.reviewer):
            print(f"已驳回 #{args.id}: {args.reason}")
        else:
            print(f"#{args.id} 不存在或状态不允许")


if __name__ == "__main__":
    main()
