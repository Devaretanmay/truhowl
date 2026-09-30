# Copyright 2026 Truhowl Authors
# SPDX-License-Identifier: Apache-2.0
"""Secret redaction for AI-bound text and persisted audit state.

Source code is untrusted input from the perspective of secret handling:
repositories routinely contain hardcoded keys, and Hunt ships file
contents to the model provider as repair context. This module scrubs
well-known secret shapes before submission (LLMClient.complete) and
before audit persistence (hunt.write_audit).

Heuristic by design, not exhaustive: patterns favor precision over
recall (minimum lengths, provider prefixes, labeled assignments) so
ordinary code and prose pass through unchanged. Anything this misses
is a gap to extend here — centrally — never per-callsite scrubbing.
"""

from __future__ import annotations

import logging
import re

_logger = logging.getLogger("truhowl.redact")

REDACTED = "[REDACTED:{kind}]"

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9\-_]{10,}")),
    ("groq-key", re.compile(r"gsk_[A-Za-z0-9]{10,}")),
    ("github-token", re.compile(r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{10,}")),
    ("aws-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("slack-token", re.compile(r"xox[bpas]-[A-Za-z0-9\-]+")),
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
        r".*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("labeled-secret", re.compile(
        r"(?i)\b(api[_-]?key|api[_-]?secret|secret[_-]?key|access[_-]?token|"
        r"auth[_-]?token|client[_-]?secret)\b\s*[:=]\s*['\"]?"
        r"[^\s'\";,]{8,}['\"]?",
    )),
]


def _replace_labeled(match: re.Match[str]) -> str:
    return f"{match.group(1)}=[REDACTED:labeled-secret]"


def redact_secrets(text: str) -> tuple[str, int]:
    """Scrub secret shapes from text. Returns (scrubbed, redaction_count)."""
    if not text or not isinstance(text, str):
        return text, 0
    total = 0
    for kind, pattern in _SECRET_PATTERNS:
        if kind == "labeled-secret":
            text, n = pattern.subn(_replace_labeled, text)
        else:
            text, n = pattern.subn(REDACTED.format(kind=kind), text)
        total += n
    if total:
        _logger.debug("redacted %d secret-shaped value(s) from AI-bound text", total)
    return text, total


def redact_record(record: object) -> tuple[object, int]:
    """Recursively scrub strings in JSON-shaped audit data. Returns (clean, count)."""
    if isinstance(record, str):
        return redact_secrets(record)
    if isinstance(record, list):
        cleaned: list[object] = []
        total = 0
        for item in record:
            clean_item, n = redact_record(item)
            cleaned.append(clean_item)
            total += n
        return cleaned, total
    if isinstance(record, dict):
        cleaned_dict: dict[object, object] = {}
        total = 0
        for key, value in record.items():
            clean_value, n = redact_record(value)
            cleaned_dict[key] = clean_value
            total += n
        return cleaned_dict, total
    return record, 0


__all__ = ["redact_secrets", "redact_record", "REDACTED"]
