"""
Network reverse tunnel management tools.
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nomad.config import (
    ConfigError,
    guard_remote,
    load_config,
    resolve_target,
    resolve_target_with_name,
)
from nomad.result import failure_result, success_result
from nomad.security import write_audit_log
from nomad.ssh import (
    SshConfigError,
    build_ssh_args,
    build_ssh_control_args,
    build_tunnel_start_args,
    execute_remote_cmd_sync,
    get_tunnel_control_path,
    probe_ssh_connectivity_result,
    validate_ssh_endpoint,
)

PROXY_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)
USERINFO_RE = re.compile(r"(?<!\S)([A-Za-z][A-Za-z0-9+.-]*://)?[^\s/@:]+:[^\s/@]+@")


def get_tunnel_env(target_cfg: dict[str, Any]) -> dict[str, str]:
    """Generates environment variables for remote proxy access based on proxy_scheme."""
    network = target_cfg.get("network") or {}
    reverse_tunnel = network.get("reverse_tunnel") or {}
    scheme = (reverse_tunnel.get("proxy_scheme") or "socks5").lower()
    port = reverse_tunnel.get("remote_bind_port", 10800)

    if scheme == "http":
        url = f"http://127.0.0.1:{port}"
        return {"HTTP_PROXY": url, "HTTPS_PROXY": url}
    else:
        # Default socks5
        url = f"socks5://127.0.0.1:{port}"
        return {"ALL_PROXY": url}


def _check_tunnel_master(socket_path: str, ssh_host: str, jump_host: str | None = None) -> bool:
    """Checks if the reverse tunnel SSH master process is active."""
    try:
        cmd = build_ssh_control_args(ssh_host, socket_path, "check", jump_host=jump_host)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except (SshConfigError, subprocess.TimeoutExpired):
        return False


def _check_remote_port_in_use(
    ssh_host: str, remote_bind_port: int, jump_host: str | None = None
) -> bool:
    """Checks whether the remote_bind_port is already bound on remote 127.0.0.1."""
    remote_cmd = (
        f"nc -z 127.0.0.1 {remote_bind_port} || "
        f"lsof -i:{remote_bind_port} || "
        f"ss -tulpn | grep :{remote_bind_port}"
    )
    ret, _, _ = execute_remote_cmd_sync(ssh_host, remote_cmd, jump_host=jump_host, timeout=5)
    return ret == 0


def _detect_local_proxy() -> dict[str, Any]:
    for key in PROXY_ENV_KEYS:
        proxy_url = os.environ.get(key)
        if not proxy_url:
            continue
        parsed = _parse_proxy_url(proxy_url)
        return {
            "detected": True,
            "env_key": key,
            "url": parsed["url"],
            "scheme": parsed["scheme"],
            "host": parsed["host"],
            "port": parsed["port"],
        }
    return {
        "detected": False,
        "env_key": None,
        "url": None,
        "scheme": None,
        "host": None,
        "port": None,
    }


def _parse_proxy_url(proxy_url: str) -> dict[str, Any]:
    parsed = urlparse(proxy_url)
    if parsed.scheme and parsed.hostname:
        return {
            "url": _redact_proxy_url(parsed),
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": _parse_proxy_port(proxy_url),
        }
    return {
        "url": proxy_url,
        "scheme": None,
        "host": None,
        "port": _parse_proxy_port(proxy_url),
    }


def _redact_proxy_url(parsed) -> str:
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            host = f"{host}:{port}"
        netloc = f"***:***@{host}"
    return parsed._replace(netloc=netloc).geturl()


def _parse_proxy_port(proxy_url: str) -> int | None:
    parsed = urlparse(proxy_url)
    try:
        if parsed.port is not None:
            return parsed.port
    except ValueError:
        return None
    if proxy_url.isdigit():
        return int(proxy_url)
    return None


def _proxy_snapshot(local_proxy: dict[str, Any]) -> dict[str, Any]:
    if not local_proxy.get("detected"):
        return {}
    if local_proxy.get("scheme") and local_proxy.get("host"):
        return {"proxy_url": os.environ.get(local_proxy["env_key"], "")}
    if local_proxy.get("port") is not None:
        return {"proxy_port": local_proxy["port"]}
    return {}


def _read_ssh_config(
    ssh_host: str,
    *,
    jump_host: str | None,
    use_proxy_for_ssh: bool,
    proxy_snapshot: dict[str, Any],
) -> dict[str, Any]:
    try:
        ssh_args = build_ssh_args(
            ssh_host,
            timeout=5,
            jump_host=jump_host,
            use_proxy_for_ssh=use_proxy_for_ssh,
            proxy_snapshot=proxy_snapshot,
        )
        cmd = ["ssh", "-G"] + ssh_args[1:-1] + [ssh_host]
    except SshConfigError as exc:
        return {"ok": False, "config": _default_ssh_config(ssh_host), "error": str(exc)}

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "config": _default_ssh_config(ssh_host),
            "error": "ssh -G timed out after 5 seconds.",
        }

    if completed.returncode != 0:
        return {
            "ok": False,
            "config": _default_ssh_config(ssh_host),
            "error": completed.stderr.strip() or f"ssh -G exited with code {completed.returncode}.",
        }

    parsed_config = _parse_ssh_g_output(completed.stdout)
    if not parsed_config.get("hostname"):
        parsed_config["hostname"] = ssh_host
    return {"ok": True, "config": parsed_config, "error": None}


def _default_ssh_config(ssh_host: str) -> dict[str, Any]:
    return {
        "hostname": ssh_host,
        "port": 22,
        "user": None,
        "proxyjump": None,
        "proxycommand": None,
        "identityfile": [],
    }


def _parse_ssh_g_output(output: str) -> dict[str, Any]:
    parsed: dict[str, Any] = _default_ssh_config("")
    identity_files: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        key, value = parts[0].lower(), parts[1].strip()
        if value.lower() == "none":
            value_obj: Any = None
        else:
            value_obj = value
        if key == "identityfile" and value_obj is not None:
            identity_files.append(str(value_obj))
        elif key == "port" and value_obj is not None:
            try:
                parsed["port"] = int(str(value_obj))
            except ValueError:
                parsed["port"] = value_obj
        elif key == "proxycommand" and value_obj is not None:
            parsed[key] = _redact_command_userinfo(str(value_obj))
        elif key in {"hostname", "user", "proxyjump"}:
            parsed[key] = value_obj
    parsed["identityfile"] = identity_files
    return parsed


def _redact_command_userinfo(value: str) -> str:
    return USERINFO_RE.sub(lambda match: f"{match.group(1) or ''}***:***@", value)


def _resolve_dns(hostname: str, port: int | str) -> dict[str, Any]:
    if not hostname:
        return {"checked": False, "hostname": hostname, "addresses": [], "error": "missing hostname"}
    try:
        ipaddress.ip_address(hostname)
        return {
            "checked": False,
            "hostname": hostname,
            "addresses": [hostname],
            "error": None,
            "reason": "hostname is already an IP address",
        }
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(hostname, int(port), type=socket.SOCK_STREAM)
    except Exception as exc:
        return {"checked": True, "hostname": hostname, "addresses": [], "error": str(exc)}

    addresses = sorted({item[4][0] for item in infos if item[4]})
    return {"checked": True, "hostname": hostname, "addresses": addresses, "error": None}


def _check_direct_tcp(hostname: str, port: int | str) -> dict[str, Any]:
    cmd = ["nc", "-z", "-w", "3", hostname, str(port)]
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "host": hostname,
            "port": port,
            "returncode": None,
            "stderr": "nc timed out after 4 seconds.",
            "cmd": cmd,
        }

    status = "reachable" if completed.returncode == 0 else "unreachable"
    return {
        "status": status,
        "host": hostname,
        "port": port,
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
        "cmd": cmd,
    }


def _run_ssh_batch(
    ssh_host: str,
    *,
    jump_host: str | None,
    use_proxy_for_ssh: bool,
    proxy_snapshot: dict[str, Any],
) -> dict[str, Any]:
    try:
        cmd = build_ssh_args(
            ssh_host,
            timeout=5,
            jump_host=jump_host,
            use_proxy_for_ssh=use_proxy_for_ssh,
            proxy_snapshot=proxy_snapshot,
        ) + ["echo ok"]
    except SshConfigError as exc:
        return {
            "status": "invalid_config",
            "classification": "invalid_config",
            "returncode": None,
            "stderr": str(exc),
            "cmd": None,
        }

    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "classification": "timeout",
            "returncode": None,
            "stderr": "SSH batch test timed out after 5 seconds.",
            "cmd": cmd,
        }

    classification = (
        "ok" if completed.returncode == 0 else _classify_ssh_stderr(completed.stderr)
    )
    return {
        "status": "ok" if completed.returncode == 0 else "failed",
        "classification": classification,
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
        "cmd": cmd,
    }


def _classify_ssh_stderr(stderr: str) -> str:
    lowered = stderr.lower()
    if "timed out" in lowered or "operation timed out" in lowered:
        return "timeout"
    if "permission denied" in lowered or "publickey" in lowered:
        return "permission_denied"
    if "host key verification failed" in lowered or "remote host identification has changed" in lowered:
        return "host_key_failed"
    if "no route to host" in lowered:
        return "no_route_to_host"
    if "connection refused" in lowered:
        return "connection_refused"
    return "unknown"


def _build_diagnosis_suggestions(
    *,
    ssh_config_result: dict[str, Any],
    dns_result: dict[str, Any],
    direct_tcp: dict[str, Any],
    local_proxy: dict[str, Any],
    ssh_batch: dict[str, Any],
    use_proxy_for_ssh: bool,
) -> list[str]:
    suggestions: list[str] = []
    if ssh_config_result.get("error"):
        suggestions.append("Inspect local SSH config for this Host alias; ssh -G did not complete cleanly.")
    if dns_result.get("checked") and dns_result.get("error"):
        suggestions.append("Fix DNS or HostName resolution for the target hostname.")
    if direct_tcp.get("status") == "timeout":
        suggestions.append("Direct TCP port test timed out; check network reachability, firewall, or routing.")
    elif direct_tcp.get("status") == "unreachable":
        suggestions.append("Direct TCP port is unreachable; verify the host, port, firewall, or required jump host.")

    classification = ssh_batch.get("classification")
    if classification == "permission_denied":
        suggestions.append("SSH reached the server but authentication failed; check keys, user, and agent state.")
    elif classification == "host_key_failed":
        suggestions.append("SSH host key verification failed; inspect known_hosts before retrying.")
    elif classification == "no_route_to_host":
        suggestions.append("No route to host; check VPN, routing, security group, or proxy/jump configuration.")
    elif classification == "connection_refused":
        suggestions.append("The SSH port refused connections; confirm sshd is running and the configured port is correct.")
    elif classification == "timeout":
        suggestions.append("SSH batch test timed out; check route, firewall, jump host, or proxy settings.")
    elif classification == "invalid_config":
        suggestions.append("The configured SSH proxy/jump settings are invalid for this target.")

    if use_proxy_for_ssh and not local_proxy.get("detected"):
        suggestions.append("Target requires SSH proxying but no local proxy environment variable was detected.")
    elif local_proxy.get("detected"):
        suggestions.append("A local proxy environment variable is present; credentials were redacted in this report.")

    if not suggestions:
        suggestions.append("Basic network diagnostics passed; investigate remote command, rsync, or application-level failures next.")
    return suggestions


def tunnel_start(target: str = "default") -> str:
    """Starts a persistent reverse SSH tunnel for the target."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="tunnel_start",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)

    if remote_guard == "unconfigured":
        return failure_result(
            tool="tunnel_start",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="tunnel_start",
            target=target,
            message="Remote synchronization and tunnels are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    try:
        target_cfg = resolve_target(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="tunnel_start",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    project_name = config.get("project_name", "unnamed")
    ssh_host = target_cfg["ssh_host"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")
    reverse_tunnel = (target_cfg.get("network") or {}).get("reverse_tunnel") or {}

    if not reverse_tunnel.get("enabled", False):
        return failure_result(
            tool="tunnel_start",
            target=target,
            message=f"Reverse tunnel is disabled for target '{target}'.",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=["Enable 'network.reverse_tunnel.enabled' in config to use tunnels."],
        )

    # Validate SSH endpoint formats first before any subprocess calls
    try:
        validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    except SshConfigError as exc:
        return failure_result(
            tool="tunnel_start",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    # Perform SSH preflight prior to master check to guarantee endpoint safety and connectivity
    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="tunnel_start",
            target=target,
            message=f"SSH preflight failed for target '{target}'.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            diagnostics=preflight.get("diagnostics", []),
            next_action={"tool": "net_diagnose", "args": {"target": target}},
        )

    local_proxy_port = reverse_tunnel.get("local_proxy_port", 7890)
    remote_bind_port = reverse_tunnel.get("remote_bind_port", 10800)

    socket_path = get_tunnel_control_path(
        project_name, target, ssh_host, local_proxy_port, remote_bind_port
    )

    if _check_tunnel_master(socket_path, ssh_host, jump_host=jump_host):
        env = get_tunnel_env(target_cfg)
        return success_result(
            tool="tunnel_start",
            target=target,
            message=f"Reverse tunnel for target '{target}' is already running.",
            data={
                "target": target,
                "status": "running",
                "socket_path": socket_path,
                "remote_bind_port": remote_bind_port,
                "local_proxy_port": local_proxy_port,
                "env": env,
            },
        )

    if _check_remote_port_in_use(ssh_host, remote_bind_port, jump_host=jump_host):
        return failure_result(
            tool="tunnel_start",
            target=target,
            message=f"Remote bind port {remote_bind_port} is already in use on target '{target}'.",
            error_type="tunnel_port_in_use",
            recoverable=True,
            diagnostics=[f"Port {remote_bind_port} on {ssh_host} is bound by another process."],
        )

    try:
        start_cmd = build_tunnel_start_args(
            ssh_host,
            socket_path,
            remote_bind_port,
            local_proxy_port,
            jump_host=jump_host,
        )
        completed = subprocess.run(start_cmd, capture_output=True, text=True, timeout=10)
        if completed.returncode != 0:
            return failure_result(
                tool="tunnel_start",
                target=target,
                message=f"Failed to start reverse tunnel for target '{target}'.",
                error_type="tunnel_start_failed",
                recoverable=True,
                diagnostics=[completed.stderr.strip() or f"ssh exited with code {completed.returncode}"],
            )
    except SshConfigError as exc:
        return failure_result(
            tool="tunnel_start",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="tunnel_start",
            target=target,
            message="Reverse tunnel start process timed out.",
            error_type="command_timeout",
            recoverable=True,
        )

    write_audit_log(
        project_name,
        "tunnel_start",
        f"target={target} ssh_host={ssh_host} remote_port={remote_bind_port}",
    )

    env = get_tunnel_env(target_cfg)
    return success_result(
        tool="tunnel_start",
        target=target,
        message=f"Reverse tunnel for target '{target}' started successfully.",
        data={
            "target": target,
            "status": "running",
            "socket_path": socket_path,
            "remote_bind_port": remote_bind_port,
            "local_proxy_port": local_proxy_port,
            "env": env,
        },
    )


def net_diagnose(target: str = "default") -> str:
    """Runs read-only SSH/network diagnostics for a configured target."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="net_diagnose",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="net_diagnose",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="net_diagnose",
            target=target,
            message="Remote network diagnostics are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    try:
        target_name, target_cfg = resolve_target_with_name(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="net_diagnose",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    ssh_host = target_cfg["ssh_host"]
    network = target_cfg.get("network") or {}
    jump_host = network.get("jump_host")
    use_proxy_for_ssh = bool(network.get("use_proxy_for_ssh"))
    local_proxy = _detect_local_proxy()
    proxy_snapshot = _proxy_snapshot(local_proxy)

    try:
        validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    except SshConfigError as exc:
        return failure_result(
            tool="net_diagnose",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    ssh_config_result = _read_ssh_config(
        ssh_host,
        jump_host=jump_host,
        use_proxy_for_ssh=use_proxy_for_ssh,
        proxy_snapshot=proxy_snapshot,
    )
    ssh_config = ssh_config_result["config"]
    hostname = ssh_config.get("hostname") or ssh_host
    port = ssh_config.get("port") or 22

    dns_result = _resolve_dns(hostname, port)
    direct_tcp = _check_direct_tcp(hostname, port)
    ssh_batch = _run_ssh_batch(
        ssh_host,
        jump_host=jump_host,
        use_proxy_for_ssh=use_proxy_for_ssh,
        proxy_snapshot=proxy_snapshot,
    )

    suggestions = _build_diagnosis_suggestions(
        ssh_config_result=ssh_config_result,
        dns_result=dns_result,
        direct_tcp=direct_tcp,
        local_proxy=local_proxy,
        ssh_batch=ssh_batch,
        use_proxy_for_ssh=use_proxy_for_ssh,
    )

    return success_result(
        tool="net_diagnose",
        target=target_name,
        message=f"Network diagnostics completed for target '{target_name}'.",
        data={
            "target": target_name,
            "ssh_host": ssh_host,
            "ssh_config": ssh_config,
            "ssh_config_error": ssh_config_result["error"],
            "dns": dns_result,
            "direct_tcp": direct_tcp,
            "local_proxy": local_proxy,
            "ssh_batch": ssh_batch,
            "suggestions": suggestions,
        },
    )


def tunnel_status(target: str = "default") -> str:
    """Checks the status of the reverse tunnel for the target."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="tunnel_status",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)

    if remote_guard == "unconfigured":
        return failure_result(
            tool="tunnel_status",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="tunnel_status",
            target=target,
            message="Remote synchronization and tunnels are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )


    try:
        target_cfg = resolve_target(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="tunnel_status",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
        )

    project_name = config.get("project_name", "unnamed")
    ssh_host = target_cfg["ssh_host"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")
    reverse_tunnel = (target_cfg.get("network") or {}).get("reverse_tunnel") or {}
    local_proxy_port = reverse_tunnel.get("local_proxy_port", 7890)
    remote_bind_port = reverse_tunnel.get("remote_bind_port", 10800)

    try:
        validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    except SshConfigError as exc:
        return failure_result(
            tool="tunnel_status",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    socket_path = get_tunnel_control_path(
        project_name, target, ssh_host, local_proxy_port, remote_bind_port
    )

    is_running = _check_tunnel_master(socket_path, ssh_host, jump_host=jump_host)
    status_str = "running" if is_running else "stopped"
    env = get_tunnel_env(target_cfg) if is_running else {}

    return success_result(
        tool="tunnel_status",
        target=target,
        message=f"Tunnel status for target '{target}': {status_str}",
        data={
            "target": target,
            "status": status_str,
            "socket_path": socket_path,
            "remote_bind_port": remote_bind_port,
            "local_proxy_port": local_proxy_port,
            "env": env,
        },
    )


def tunnel_stop(target: str = "default") -> str:
    """Stops the persistent reverse SSH tunnel for the target."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="tunnel_stop",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)

    if remote_guard == "unconfigured":
        return failure_result(
            tool="tunnel_stop",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="tunnel_stop",
            target=target,
            message="Remote synchronization and tunnels are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )


    try:
        target_cfg = resolve_target(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="tunnel_stop",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
        )

    project_name = config.get("project_name", "unnamed")
    ssh_host = target_cfg["ssh_host"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")
    reverse_tunnel = (target_cfg.get("network") or {}).get("reverse_tunnel") or {}
    local_proxy_port = reverse_tunnel.get("local_proxy_port", 7890)
    remote_bind_port = reverse_tunnel.get("remote_bind_port", 10800)

    try:
        validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    except SshConfigError as exc:
        return failure_result(
            tool="tunnel_stop",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    socket_path = get_tunnel_control_path(
        project_name, target, ssh_host, local_proxy_port, remote_bind_port
    )

    try:
        cmd = build_ssh_control_args(ssh_host, socket_path, "exit", jump_host=jump_host)
        subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except Exception:
        pass

    sock_file = Path(socket_path)
    if sock_file.exists():
        try:
            sock_file.unlink()
        except Exception:
            pass

    write_audit_log(
        project_name,
        "tunnel_stop",
        f"target={target} ssh_host={ssh_host} remote_port={remote_bind_port}",
    )

    return success_result(
        tool="tunnel_stop",
        target=target,
        message=f"Reverse tunnel for target '{target}' stopped.",
        data={
            "target": target,
            "status": "stopped",
            "socket_path": socket_path,
        },
    )
