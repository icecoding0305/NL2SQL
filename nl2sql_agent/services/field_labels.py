"""Business-facing field labels and safe SQL aliases."""

from __future__ import annotations

import re


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")
_ENUM_DETAIL = re.compile(r"(?:^|[,，;；])\s*\d{1,4}\s*[:：=]")


def concise_business_label(value: str | None, fallback: str = "") -> str:
    """Keep a short display name while leaving enum detail in the description.

    Database comments often look like ``还款状态(00:正常,03:逾期)``.  The whole
    comment is useful evidence but is not a suitable semantic label or SQL alias.
    """
    label = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", str(value or ""))).strip()
    for opener, closer in (("(", ")"), ("（", "）")):
        if opener not in label:
            continue
        prefix, detail = label.split(opener, 1)
        detail = detail.rsplit(closer, 1)[0]
        if prefix.strip() and (
            _ENUM_DETAIL.search(detail)
            or re.search(r"\d{1,4}\s*[:：=]", detail)
        ):
            label = prefix.strip()
            break
    label = label.strip(" ,，;；:：")
    return label or str(fallback or "").strip()


def safe_sql_alias(value: str | None, fallback: str = "字段", max_length: int = 48) -> str:
    """Return a compact identifier label; the SQL renderer must still quote it."""
    alias = concise_business_label(value, fallback=fallback)
    for separator in ("\n", "\r", "(", "（"):
        if separator in alias:
            prefix = alias.split(separator, 1)[0].strip()
            if prefix:
                alias = prefix
    alias = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub(" ", alias)).strip(" ,，;；:：")
    return (alias or fallback or "字段")[:max_length]
