"""File logging helpers for the Nomad MCP stdio server."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
import traceback
from inspect import Signature
from pathlib import Path
from typing import Any, TextIO


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
PYPI_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>pypi-)[A-Za-z0-9_-]{20,}",
    re.IGNORECASE,
)
GITHUB_TOKEN_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_])
    (?P<prefix>
        github_pat_
        |
        gh[pousr]_
    )
    [A-Za-z0-9_]{20,}
    """,
    re.IGNORECASE | re.VERBOSE,
)
OPENAI_PROJECT_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>sk-proj-)[A-Za-z0-9_-]{20,}"
)
OPENAI_LEGACY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>sk-)[A-Za-z0-9]{20,}"
)
AWS_ACCESS_KEY_RE = re.compile(
    r"(?<![A-Z0-9])(?P<prefix>A[KS]IA)[A-Z0-9]{16}(?![A-Z0-9])"
)
SLACK_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<prefix>xox[abepors]-)[A-Za-z0-9-]{20,}",
    re.IGNORECASE,
)
PEM_PRIVATE_KEY_RE = re.compile(
    r"""
    -----BEGIN\ (?P<key_type>
        PRIVATE\ KEY
        |RSA\ PRIVATE\ KEY
        |EC\ PRIVATE\ KEY
        |OPENSSH\ PRIVATE\ KEY
    )-----
    .*?
    -----END\ (?P=key_type)-----
    """,
    re.DOTALL | re.VERBOSE,
)
ASSIGNMENT_RE = re.compile(
    r"""
    (?<![A-Za-z0-9_.-])
    (?P<key_quote>["']?)
    (?P<key>[A-Za-z_][A-Za-z0-9_.-]*)
    (?P=key_quote)
    (?P<separator>\s*(?:=|:)\s*)
    (?P<value>
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
        |
        (?:Bearer|Basic|Token)\s+[^\s,;}\]]+
        |
        \[[^\]\r\n]*\]
        |
        [^\s,;}\]]+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_LOGGER: logging.Logger | None = None


class _SecureFileHandler(logging.FileHandler):
    """A FileHandler whose stream is opened through validated directory fds."""

    def __init__(self, filename: Path, *, tighten_existing_parent: bool) -> None:
        self._tighten_existing_parent = tighten_existing_parent
        super().__init__(filename, mode="a", encoding="utf-8", delay=True)
        self.stream = self._open()

    def _open(self) -> TextIO:
        fd = _open_secure_log_file(
            Path(self.baseFilename),
            tighten_existing_parent=self._tighten_existing_parent,
        )
        try:
            return os.fdopen(fd, self.mode, encoding=self.encoding)
        except Exception:
            os.close(fd)
            raise


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

    new_handler = _SecureFileHandler(
        log_path,
        tighten_existing_parent=not bool(os.environ.get(LOG_ENV_VAR)),
    )
    new_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [pid=%(process)d] %(message)s")
    )

    logger.setLevel(logging.INFO)
    logger.propagate = False
    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        old_handler.close()

    logger.addHandler(new_handler)
    _LOGGER = logger
    return logger


def log_server_startup(cwd: str, version: str) -> None:
    try:
        get_mcp_logger().info(
            "server startup cwd=%s python=%s version=%s log_path=%s",
            cwd,
            sys.version.replace("\n", " "),
            version,
            get_log_path(),
        )
    except BaseException:
        return


def log_server_shutdown() -> None:
    try:
        get_mcp_logger().info("server shutdown")
    except BaseException:
        return


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
    redacted = PEM_PRIVATE_KEY_RE.sub(_redact_pem_private_key, redacted)
    redacted = _redact_sensitive_assignments(redacted)
    redacted = AUTH_RE.sub(r"\1[REDACTED]", redacted)
    redacted = PYPI_TOKEN_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = GITHUB_TOKEN_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = OPENAI_PROJECT_TOKEN_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = OPENAI_LEGACY_TOKEN_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = AWS_ACCESS_KEY_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = SLACK_TOKEN_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    return redacted


def _redact_pem_private_key(match: re.Match[str]) -> str:
    key_type = match.group("key_type")
    return (
        f"-----BEGIN {key_type}-----\n"
        "[REDACTED]\n"
        f"-----END {key_type}-----"
    )


def _redact_sensitive_assignments(value: str) -> str:
    pieces: list[str] = []
    output_position = 0
    search_position = 0
    while match := ASSIGNMENT_RE.search(value, search_position):
        if not _is_sensitive_assignment_key(match.group("key")):
            search_position = match.start() + 1
            continue
        pieces.append(value[output_position:match.start()])
        pieces.append(_redact_sensitive_assignment(match))
        output_position = match.end()
        search_position = match.end()
    pieces.append(value[output_position:])
    return "".join(pieces)


def _redact_sensitive_assignment(match: re.Match[str]) -> str:
    key = match.group("key")
    value = match.group("value")
    if value.strip("\"'") == "[REDACTED]":
        return match.group(0)
    replacement = "[REDACTED]"
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        replacement = f"{value[0]}{replacement}{value[-1]}"
    return (
        f"{match.group('key_quote')}{key}{match.group('key_quote')}"
        f"{match.group('separator')}{replacement}"
    )


def _is_sensitive_assignment_key(key: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper()
    parts = tuple(part for part in normalized.split("_") if part)
    if normalized == "KEY" or normalized.endswith("_KEY"):
        return True
    if normalized in {
        "API_KEY",
        "API_TOKEN",
        "AUTH",
        "AUTHORIZATION",
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSWORD",
        "PASSWD",
        "SECRET",
    }:
        return True
    return any(
        part
        in {
            "AUTH",
            "AUTHORIZATION",
            "CREDENTIAL",
            "CREDENTIALS",
            "PASSWORD",
            "PASSWD",
            "SECRET",
            "TOKEN",
        }
        for part in parts
    )


def _logger_points_to(logger: logging.Logger, log_path: Path) -> bool:
    expected = os.path.abspath(os.fspath(log_path))
    return any(
        isinstance(handler, logging.FileHandler)
        and getattr(handler, "baseFilename", None) == expected
        for handler in logger.handlers
    )


def _open_secure_log_file(
    log_path: Path,
    *,
    tighten_existing_parent: bool,
) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("secure MCP logging requires O_NOFOLLOW support")

    absolute_path = Path(os.path.abspath(os.fspath(log_path)))
    if not absolute_path.name:
        raise ValueError("MCP log path must name a file")

    parent_fd = _open_secure_parent(
        absolute_path.parent,
        tighten_existing=tighten_existing_parent,
    )
    try:
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
        )
        fd = os.open(absolute_path.name, flags, 0o600, dir_fd=parent_fd)
        try:
            file_stat = os.fstat(fd)
            _validate_log_file(file_stat, absolute_path)
            os.fchmod(fd, 0o600)
            _validate_log_file(os.fstat(fd), absolute_path, expected_mode=0o600)
            return fd
        except Exception:
            os.close(fd)
            raise
    finally:
        os.close(parent_fd)


def _open_secure_parent(parent: Path, *, tighten_existing: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    current_fd = os.open(os.path.sep, flags)
    parent_created = False
    try:
        for component in parent.parts[1:]:
            component_created = False
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, 0o700, dir_fd=current_fd)
                    component_created = True
                except FileExistsError:
                    pass
                next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            parent_created = component_created

        parent_stat = os.fstat(current_fd)
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise PermissionError(f"MCP log parent is not a directory: {parent}")
        if parent_stat.st_uid != os.getuid():
            raise PermissionError(f"MCP log parent is not owned by the current user: {parent}")
        parent_mode = stat.S_IMODE(parent_stat.st_mode)
        if parent_created or tighten_existing:
            os.fchmod(current_fd, 0o700)
            parent_mode = stat.S_IMODE(os.fstat(current_fd).st_mode)
            if parent_mode != 0o700:
                raise PermissionError(
                    f"MCP log parent permissions are not 0700: {parent}"
                )
        elif parent_mode & 0o022:
            raise PermissionError(
                f"Custom MCP log parent is group/world-writable: {parent}"
            )
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _validate_log_file(
    file_stat: os.stat_result,
    log_path: Path,
    *,
    expected_mode: int | None = None,
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise PermissionError(f"MCP log is not a regular file: {log_path}")
    if file_stat.st_uid != os.getuid():
        raise PermissionError(f"MCP log is not owned by the current user: {log_path}")
    if file_stat.st_nlink != 1:
        raise PermissionError(f"MCP log must have exactly one hard link: {log_path}")
    if expected_mode is not None and stat.S_IMODE(file_stat.st_mode) != expected_mode:
        raise PermissionError(
            f"MCP log permissions are not {expected_mode:04o}: {log_path}"
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
