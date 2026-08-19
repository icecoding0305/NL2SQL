"""Per-request LLM telemetry without retaining prompt or response content."""

from __future__ import annotations

from contextvars import ContextVar, Token
from time import perf_counter
from typing import Any


_node: ContextVar[str | None] = ContextVar("llm_trace_node", default=None)
_trace_id: ContextVar[str | None] = ContextVar("llm_trace_id", default=None)
_calls: ContextVar[list[dict[str, Any]] | None] = ContextVar("llm_trace_calls", default=None)


def current_node() -> str | None:
    return _node.get()


def begin_capture(node: str, trace_id: str) -> tuple[Token, Token, Token]:
    return (_node.set(node), _trace_id.set(trace_id), _calls.set([]))


def end_capture(tokens: tuple[Token, Token, Token]) -> list[dict[str, Any]]:
    calls = list(_calls.get() or [])
    node_token, trace_token, calls_token = tokens
    _calls.reset(calls_token)
    _trace_id.reset(trace_token)
    _node.reset(node_token)
    return calls


def record_call(
    *,
    provider: str,
    model: str,
    operation: str,
    started_at: float,
    prompt_chars: int,
    max_tokens: int,
    output_chars: int = 0,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    request_id: str | None = None,
    error: Exception | None = None,
) -> None:
    """Append metadata only; prompt and model output are intentionally excluded."""
    calls = _calls.get()
    if calls is None:
        return
    calls.append(
        {
            "node": _node.get(),
            "trace_id": _trace_id.get(),
            "attempt": len(calls) + 1,
            "provider": provider,
            "model": model,
            "operation": operation,
            "duration_ms": round((perf_counter() - started_at) * 1000, 2),
            "status": "error" if error else "success",
            "prompt_chars": prompt_chars,
            "output_chars": output_chars,
            "max_tokens": max_tokens,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "request_id": request_id,
            "structured_status": None,
            "error_type": type(error).__name__ if error else None,
            "error": str(error)[:500] if error else None,
        }
    )


def annotate_last_call(*, status: str, error: Exception | None = None) -> None:
    calls = _calls.get()
    if not calls:
        return
    calls[-1]["structured_status"] = status
    if error is not None:
        calls[-1]["validation_error_type"] = type(error).__name__
        calls[-1]["validation_error"] = str(error)[:500]


def usage_value(usage: object | None, *names: str) -> int | None:
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None
