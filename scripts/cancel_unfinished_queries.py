"""Mark persisted unfinished queries as cancelled without deleting history."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "nl2sql.db"


def main() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT trace_id, status FROM queries "
            "WHERE status IN ('running', 'pending_review')"
        ).fetchall()
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE queries SET status='cancelled', next_node=NULL, finished_at=?, "
            "execution_error=COALESCE(execution_error, 'Cancelled by user') "
            "WHERE status IN ('running', 'pending_review')",
            (now,),
        )
        remaining = conn.execute(
            "SELECT COUNT(*) FROM queries "
            "WHERE status IN ('running', 'pending_review')"
        ).fetchone()[0]

    print(
        json.dumps(
            {
                "cancelled": len(rows),
                "running": sum(status == "running" for _, status in rows),
                "pending_review": sum(
                    status == "pending_review" for _, status in rows
                ),
                "remaining": remaining,
            }
        )
    )


if __name__ == "__main__":
    main()
