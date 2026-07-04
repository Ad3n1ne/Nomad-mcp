"""
SSH Connection wrapper with ControlMaster connection sharing and hardware/runtime probing.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from urllib.parse import unquote, urlparse
from typing import List, Dict, Any, Optional



CONTROL_PATH = "/tmp/nomad_ssh_%C"
DEFAULT_CONNECT_TIMEOUT = 5
HOST_SHELL_META_RE = re.compile(r"[;|&$\\<>`]")
HOST_CONTROL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]")


class SshConfigError(Exception):
    """Raised when SSH argv cannot be built from the provided config."""


def build_ssh_args(
    ssh_host: str,
    *,
    timeout: int = DEFAULT_CONNECT_TIMEOUT,
    jump_host: str | None = None,
    use_proxy_for_ssh: bool = False,
    proxy_snapshot: dict[str, Any] | None = None,
) -> list[str]:
    """Builds a safe ssh argv list without executing SSH."""
    _validate_host_like("ssh_host", ssh_host)
    if jump_host is not None:
        _validate_host_like("jump_host", jump_host)
    if jump_host and use_proxy_for_ssh:
        raise SshConfigError("jump_host conflicts with use_proxy_for_ssh")

    argv = [
        "ssh",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={CONTROL_PATH}",
        "-o",
        "ControlPersist=60s",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "BatchMode=yes",
    ]

    if jump_host:
        argv.extend(["-J", jump_host])

    if use_proxy_for_ssh:
        argv.extend(["-o", _build_proxy_command(proxy_snapshot or {})])

    argv.append(ssh_host)
    return argv


def _build_proxy_command(proxy_snapshot: dict[str, Any]) -> str:
    proxy_url = proxy_snapshot.get("proxy_url")
    if proxy_url:
        return _proxy_command_from_url(proxy_url)

    proxy_port = proxy_snapshot.get("proxy_port")
    if proxy_port is not None:
        return _proxy_command("5", "127.0.0.1", proxy_port)

    raise SshConfigError("proxy snapshot missing proxy_url or proxy_port")


def _proxy_command_from_url(proxy_url: str) -> str:
    parsed = urlparse(proxy_url)
    scheme_to_nc = {"socks5": "5", "socks4": "4"}
    if parsed.scheme not in scheme_to_nc:
        raise SshConfigError(f"unsupported proxy scheme: {parsed.scheme}")
    if not parsed.hostname or parsed.port is None:
        raise SshConfigError("proxy_url must include host and port")
    proxy_host = unquote(parsed.hostname)
    _validate_host_like("proxy host", proxy_host)
    return _proxy_command(scheme_to_nc[parsed.scheme], proxy_host, parsed.port)


def _proxy_command(nc_version: str, host: str, port: Any) -> str:
    _validate_host_like("proxy host", host)
    if not isinstance(port, int):
        raise SshConfigError("proxy port must be an integer")
    if port < 1 or port > 65535:
        raise SshConfigError("proxy port must be between 1 and 65535")
    return f"ProxyCommand=nc -X {nc_version} -x {host}:{port} %h %p"


def _validate_host_like(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise SshConfigError(f"{field_name} must be a non-empty string")
    if value.startswith("-"):
        raise SshConfigError(f"{field_name} must not start with '-'")
    if HOST_CONTROL_OR_SPACE_RE.search(value):
        raise SshConfigError(f"{field_name} must not contain whitespace or control characters")
    if HOST_SHELL_META_RE.search(value):
        raise SshConfigError(f"{field_name} contains unsafe metacharacters")


def probe_ssh_connectivity(
    ssh_host: str, timeout: int = 5, jump_host: str | None = None
) -> bool:
    """Verifies connection with minimal timeout."""
    return probe_ssh_connectivity_result(ssh_host, timeout=timeout, jump_host=jump_host)["ok"]


def probe_ssh_connectivity_result(
    ssh_host: str, timeout: int = 5, jump_host: str | None = None
) -> dict[str, Any]:
    """Runs a lightweight SSH preflight and returns classified diagnostics."""
    try:
        argv = build_ssh_args(ssh_host, timeout=timeout, jump_host=jump_host) + ["echo ok"]
    except SshConfigError as exc:
        return _probe_result(
            ok=False,
            error_type="invalid_config",
            diagnostics=[str(exc)],
        )

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _probe_result(
            ok=False,
            error_type="ssh_timeout",
            diagnostics=[f"SSH probe timed out after {timeout} seconds."],
        )

    if completed.returncode == 0:
        return _probe_result(ok=True, diagnostics=["SSH probe succeeded."])

    stderr = completed.stderr or ""
    error_type = _classify_ssh_error(stderr)
    return _probe_result(
        ok=False,
        error_type=error_type,
        diagnostics=[stderr.strip() or f"SSH exited with code {completed.returncode}."],
    )


def _probe_result(
    *, ok: bool, error_type: str | None = None, diagnostics: list[str] | None = None
) -> dict[str, Any]:
    return {
        "ok": ok,
        "error_type": error_type,
        "recoverable": not ok,
        "diagnostics": diagnostics or [],
    }


def _classify_ssh_error(stderr: str) -> str:
    lowered = stderr.lower()
    if "permission denied" in lowered or "publickey" in lowered:
        return "ssh_auth_failed"
    if "host key verification failed" in lowered or "remote host identification has changed" in lowered:
        return "ssh_host_key_failed"
    if "connection refused" in lowered:
        return "ssh_connection_refused"
    return "ssh_unknown_failure"


def execute_remote_cmd_sync(
    ssh_host: str,
    cmd: str,
    env: Optional[Dict[str, str]] = None,
    *,
    timeout: int = 30,
    jump_host: str | None = None,
) -> tuple[int, str, str]:
    """Runs a remote command synchronously using standard SSH wrappers.
    
    Returns (returncode, stdout, stderr).
    """
    argv = build_ssh_args(ssh_host, timeout=timeout, jump_host=jump_host) + [cmd]
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.returncode, completed.stdout, completed.stderr


def get_tunnel_control_path(
    project_name: str,
    target_name: str,
    ssh_host: str,
    local_proxy_port: int,
    remote_bind_port: int,
) -> str:
    """Generates a unique control socket path for a reverse tunnel: /tmp/nomad_tunnel_<hash>."""
    key = f"{project_name}:{target_name}:{ssh_host}:{local_proxy_port}:{remote_bind_port}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"/tmp/nomad_tunnel_{digest}"


def validate_ssh_endpoint(ssh_host: str, jump_host: str | None = None) -> None:
    """Validates ssh_host and jump_host formats, raising SshConfigError if unsafe."""
    _validate_host_like("ssh_host", ssh_host)
    if jump_host is not None:
        _validate_host_like("jump_host", jump_host)


def build_ssh_control_args(
    ssh_host: str,
    socket_path: str,
    action: str,
    *,
    jump_host: str | None = None,
) -> list[str]:
    """Builds safe ssh argv for control operations (-O check / -O exit)."""
    validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    argv = ["ssh", "-S", socket_path, "-O", action]
    if jump_host:
        argv.extend(["-J", jump_host])
    argv.append(ssh_host)
    return argv


def build_tunnel_start_args(
    ssh_host: str,
    socket_path: str,
    remote_bind_port: int,
    local_proxy_port: int,
    *,
    jump_host: str | None = None,
) -> list[str]:
    """Builds safe ssh argv for reverse tunnel master process."""
    validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    argv = [
        "ssh",
        "-f",
        "-N",
        "-M",
        "-S",
        socket_path,
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-R",
        f"127.0.0.1:{remote_bind_port}:127.0.0.1:{local_proxy_port}",
    ]
    if jump_host:
        argv.extend(["-J", jump_host])
    argv.append(ssh_host)
    return argv



