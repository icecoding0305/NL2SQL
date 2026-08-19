"""Shared access control for browser users and streaming queries."""

from __future__ import annotations

import os
import secrets


def platform_access_required() -> bool:
    return bool(os.getenv("PLATFORM_ACCESS_TOKEN", "").strip())


def verify_platform_token(provided: str | None) -> bool:
    expected = os.getenv("PLATFORM_ACCESS_TOKEN", "").strip()
    if not expected:
        return True
    return bool(provided) and secrets.compare_digest(str(provided), expected)
