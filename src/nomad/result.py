import json
from collections.abc import Mapping, Sequence
from typing import Any


ERROR_TYPES = frozenset(
    {
        "unconfigured",
        "local_mode",
        "invalid_config",
        "target_not_found",
        "unsafe_local_cwd",
        "unsafe_remote_path",
        "dangerous_command",
        "interactive_command",
        "ssh_timeout",
        "ssh_auth_failed",
        "ssh_host_key_failed",
        "ssh_connection_refused",
        "ssh_unknown_failure",
        "ssh_proxy_unavailable",
        "tunnel_start_failed",
        "tunnel_not_running",
        "tunnel_port_in_use",
        "command_timeout",
        "path_traversal",
        "remote_command_failed",
        "rsync_failed",
        "rsync_delete_threshold_exceeded",
        "task_exists",
        "task_not_found",
        "internal_error",
    }
)


def success_result(
    *,
    tool: str,
    message: str,
    target: str | None = None,
    data: Mapping[str, Any] | None = None,
    diagnostics: Sequence[Any] | None = None,
    next_action: Mapping[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "ok": True,
        "tool": tool,
        "target": target,
        "message": message,
        "data": dict(data or {}),
        "diagnostics": _to_list(diagnostics),
        "next_action": dict(next_action) if next_action is not None else None,
    }
    return _to_json(payload)


def failure_result(
    *,
    tool: str,
    error_type: str,
    message: str,
    recoverable: bool,
    target: str | None = None,
    details: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    diagnostics: Sequence[Any] | None = None,
    next_action: Mapping[str, Any] | None = None,
) -> str:
    if error_type not in ERROR_TYPES:
        raise ValueError(f"Unknown error_type: {error_type}")

    payload: dict[str, Any] = {
        "ok": False,
        "tool": tool,
        "target": target,
        "error_type": error_type,
        "message": message,
        "details": dict(details or {}),
        "recoverable": recoverable,
        "data": dict(data or {}),
        "diagnostics": _to_list(diagnostics),
        "next_action": dict(next_action) if next_action is not None else None,
    }
    return _to_json(payload)


def _to_list(value: Sequence[Any] | None) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _to_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
