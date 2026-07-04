"""
Output truncation and noise filtering.
"""
from __future__ import annotations

import re


DEFAULT_MAX_LINES = 200
DEFAULT_MAX_BYTES = 10240

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
NOISE_PATTERNS = [
    re.compile(r"^\s*$"),
    re.compile(r"^Downloading.*\d+%"),
    re.compile(r"^Already up to date\.$"),
]
ERROR_HINT_RE = re.compile(r"\b(error|failed|failure|traceback|exception)\b", re.IGNORECASE)
TRUNCATION_ADVICE = (
    "Try filtering with grep/head/tail and retry; for long-running task logs, "
    "use task_status(task_name, tail_lines=50)."
)


def safe_truncate(output: str, max_lines: int = 200, max_bytes: int = 10240) -> str:
    """Truncates output to avoid cluttering LLM context window."""
    cleaned = _strip_ansi(output)
    filtered = "\n".join(filter_noise(cleaned.splitlines()))

    lines = filtered.splitlines()
    messages = []
    if len(lines) > max_lines:
        truncated = len(lines) - max_lines
        filtered = "\n".join(lines[:max_lines])
        messages.append(f"truncated the last {truncated} lines")

    encoded = filtered.encode("utf-8")
    if len(encoded) > max_bytes:
        filtered = encoded[:max_bytes].decode("utf-8", errors="ignore")
        messages.append("truncated by bytes")

    if messages:
        return filtered + f"\n\n... [Output too long; {'; '.join(messages)}. {TRUNCATION_ADVICE}]"
    return filtered


def filter_noise(lines: list[str]) -> list[str]:
    """Filters progress bars, empty lines, and other logging noise."""
    kept = []
    for line in lines:
        if ERROR_HINT_RE.search(line):
            kept.append(line)
            continue
        if any(pattern.match(line) for pattern in NOISE_PATTERNS):
            continue
        kept.append(line)
    return kept


def _strip_ansi(output: str) -> str:
    return ANSI_ESCAPE_RE.sub("", output)
