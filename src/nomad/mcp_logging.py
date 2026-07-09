"""File logging helpers for the Nomad MCP stdio server."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from inspect import Signature
from pathlib import Path
from typing import Any


LOG_ENV_VAR = "NOMAD_MCP_LOG_PATH"
DEFAULT_LOG_PATH = Path.home() / ".nomad" / "nomad-mcp.log"
LOGGER_NAME = "nomad.mcp"
SENSITIVE_KEY_TOKENS = (
    "secret",
    "token",
    "password",
    "key",
    "auth",
    "credential",
    "env",
    "config",
    "cmd",
    "command",
)
SAFE_VALUE_KEYS = {"target", "task_name", "tail_lines"}
USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]+:[^/\s:@]+@")
AUTH_RE = re.compile(
    r"((?:authorization|auth_token)\s*(?::|=)\s*(?:Bearer|Basic|Token)\s+)[^\s]+",
    re.IGNORECASE,
)

_LOGGER: logging.Logger | None = None


def get_log_path() -> Path:
    """Returns the MCP log path, honoring tests or operator overrides."""
    override = os.environ.get(LOG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return DEFAULT_LOG_PATH


def get_mcp_logger() -> logging.Logger:
    """Builds a file-only MCP logger that never writes to stdout."""
    global _LOGGER

    log_path = get_log_path()
    logger = logging.getLogger(LOGGER_NAME)
    if _LOGGER is logger and _logger_points_to(logger, log_path):
        return logger

    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [pid=%(process)d] %(message)s")
    )
    logger.addHandler(handler)
    _LOGGER = logger
    return logger


def log_server_startup(cwd: str, version: str) -> None:
    get_mcp_logger().info(
        "server startup cwd=%s python=%s version=%s log_path=%s",
        cwd,
        sys.version.replace("\n", " "),
        version,
        get_log_path(),
    )


def log_server_shutdown() -> None:
    get_mcp_logger().info("server shutdown")


def summarize_call(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    signature: Signature | None = None,
) -> str:
    """Returns a short, redacted summary of tool call arguments."""
    if signature is not None:
        try:
            bound = signature.bind_partial(*args, **kwargs)
            payload = {
                key: _redact_value_for_key(key, value)
                for key, value in bound.arguments.items()
            }
            return json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)[:2000]
        except Exception:
            pass

    payload = {
        "args": [_redact_value(value) for value in args],
        "kwargs": {key: _redact_value(value) for key, value in kwargs.items()},
    }
    return json.dumps(payload, ensure_ascii=False, default=str, sort_keys=True)[:2000]


def summarize_result(result: Any) -> str:
    """Returns a compact ok/error summary without logging full command output."""
    if not isinstance(result, str):
        return f"type={type(result).__name__}"
    try:
        payload = json.loads(result)
    except Exception:
        return f"non_json_string len={len(result)}"

    parts = [f"ok={payload.get('ok')}"]
    if payload.get("error_type"):
        parts.append(f"error_type={payload.get('error_type')}")
    if payload.get("tool"):
        parts.append(f"tool={payload.get('tool')}")
    if payload.get("target") is not None:
        parts.append(f"target={payload.get('target')}")
    return " ".join(parts)


def format_traceback(exc: BaseException) -> str:
    return redact_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def redact_text(value: str) -> str:
    """Redacts common credentials from text before it reaches MCP logs or diagnostics."""
    redacted = USERINFO_RE.sub(r"\1***:***@", value)
    redacted = AUTH_RE.sub(r"\1[REDACTED]", redacted)
    return redacted


def _logger_points_to(logger: logging.Logger, log_path: Path) -> bool:
    expected = str(log_path)
    return any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", None) == expected
        for handler in logger.handlers
    )


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_value_for_key(str(key), item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value[:20]]
    if isinstance(value, str):
        return f"<str len={len(value)}>"
    return value


def _redact_value_for_key(key: str, value: Any) -> Any:
    lowered = key.lower()
    if lowered in SAFE_VALUE_KEYS:
        return redact_text(str(value))
    if any(token in lowered for token in SENSITIVE_KEY_TOKENS):
        return _summarize_sensitive_value(value)
    return _redact_value(value)


def _summarize_sensitive_value(value: Any) -> str:
    if isinstance(value, str):
        return f"<redacted str len={len(value)}>"
    if isinstance(value, dict):
        return f"<redacted dict keys={len(value)}>"
    if isinstance(value, (list, tuple)):
        return f"<redacted list len={len(value)}>"
    return f"<redacted {type(value).__name__}>"
