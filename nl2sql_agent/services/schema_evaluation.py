"""Thread-safe orchestration for the read-only Schema golden-set evaluation."""

from __future__ import annotations

import threading
import time
from typing import Any

import yaml

from nl2sql_agent.eval.run_schema_golden_eval import (
    DEFAULT_CASES,
    run_golden_evaluation,
)


class SchemaEvaluationService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_report: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def dataset_summary(self) -> dict[str, Any]:
        payload = yaml.safe_load(DEFAULT_CASES.read_text(encoding="utf-8")) or {}
        return {
            "version": payload.get("version", 1),
            "description": payload.get("description", ""),
            "coverage": payload.get("coverage") or {},
            "case_count": len(payload.get("cases") or []),
        }

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "dataset": self.dataset_summary(),
            "report": self._last_report,
        }

    def run(self) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Schema 评测正在运行，请稍后查看结果")
        try:
            started_at = time.time()
            report = run_golden_evaluation()
            report.update({
                "started_at": started_at,
                "finished_at": time.time(),
            })
            report["duration_seconds"] = round(
                report["finished_at"] - started_at, 3
            )
            self._last_report = report
            return report
        finally:
            self._lock.release()
