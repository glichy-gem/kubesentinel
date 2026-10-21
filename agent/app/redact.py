"""Strip anything that looks like a secret before evidence is stored or sent to the LLM.

Defence in depth: collectors already avoid secret sources (env var values are
never read), but logs and diffs can contain anything.
"""

import re

RULES: list[tuple[re.Pattern[str], str]] = [
    # key=value / key: value where the key name suggests a secret; keep the key.
    (
        re.compile(
            r"(?i)\b([\w.-]*(?:password|passwd|secret|token|api[_-]?key|apikey|credential)[\w.-]*"
            r"\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED]"),  # Google API keys
    (re.compile(r"\bAQ\.[0-9A-Za-z_-]{30,}"), "[REDACTED]"),  # Google auth keys
    # Telegram bot tokens, also inside URLs (".../bot123456:ABC..."), so no \b.
    (re.compile(r"(?<![0-9])\d{6,}:[A-Za-z0-9_-]{30,}"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"), "[REDACTED]"),  # GitHub tokens
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"), "Bearer [REDACTED]"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"),
        "[REDACTED]",
    ),  # JWTs
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        "[REDACTED]",
    ),
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://[REDACTED]@"),  # credentials in URLs
]


def redact(text: str) -> str:
    for pattern, replacement in RULES:
        text = pattern.sub(replacement, text)
    return text
