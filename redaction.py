from __future__ import annotations

import re
from collections.abc import Mapping


TOKEN_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+"),
    re.compile(r"Authorization:\s*Bearer\s+\S+", re.I),
    re.compile(r"xoxb-[A-Za-z0-9-]+"),
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{12,}"),
    re.compile("/" + r"Users/[A-Za-z0-9._-]+"),
)

SENSITIVE_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|KEY|AUTH)", re.I)


def redact(text: object) -> str:
    value = str(text)
    for pattern in TOKEN_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def redact_mapping(values: Mapping[str, object]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in values.items():
        if SENSITIVE_ENV.search(key):
            safe[key] = "[REDACTED]"
        else:
            safe[key] = redact(value)
    return safe
