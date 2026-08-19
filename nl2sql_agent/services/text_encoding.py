"""Conservative repair for browser/client text decoded with the wrong charset."""

from __future__ import annotations

from typing import Any


def _cjk_count(text: str) -> int:
    return sum(1 for char in text if "\u3400" <= char <= "\u9fff")


def repair_mojibake(text: str) -> str:
    """Repair Latin-1 mojibake only when the candidate is clearly more Chinese.

    Covers both common paths:
    - UTF-8 bytes decoded as Latin-1: ``æŸ¥è¯¢``
    - GBK bytes decoded as Latin-1: ``²éÑ¯``
    Normal Chinese, English, identifiers, and values are returned unchanged.
    """
    if not text or all(ord(char) < 128 for char in text):
        return text
    try:
        raw = text.encode("latin-1")
    except UnicodeEncodeError:
        return text

    original_cjk = _cjk_count(text)
    for encoding in ("utf-8", "gb18030"):
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _cjk_count(candidate) >= max(2, original_cjk + 2):
            return candidate
    return text


def normalize_query_payload(value: Any) -> Any:
    """Recursively normalize user-controlled strings in a query payload."""
    if isinstance(value, str):
        return repair_mojibake(value)
    if isinstance(value, list):
        return [normalize_query_payload(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_query_payload(item) for key, item in value.items()}
    return value
