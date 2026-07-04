"""
Remote execution tools.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from typing import Any, Dict

from nomad.config import (
    ConfigError,
    guard_remote,
    load_config,
    resolve_target,
)
from nomad.result import failure_result, success_result
from nomad.security import (
    check_dangerous_command,
    check_interactive_command,
    verify_local_cwd_safety,
    verify_remote_path_safety,
    write_audit_log,
)
from nomad.ssh import (
    SshConfigError,
    build_ssh_args,
    get_tunnel_control_path,
    probe_ssh_connectivity_result,
    validate_ssh_endpoint,
)
from nomad.tools.network import _check_tunnel_master, get_tunnel_env, tunnel_start
from nomad.truncate import safe_truncate


def run_remote(cmd: str, target: str = "default") -> str:
    """Executes a command on the remote host workspace under target limits."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="run_remote",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="run_remote",
            target=target,
            message="Remote execution is disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    try:
        target_cfg = resolve_target(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    ssh_host = target_cfg["ssh_host"]
    remote_path = target_cfg["remote_path"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")
    project_name = config.get("project_name", "unnamed")

    cwd_err = verify_local_cwd_safety()
    if cwd_err is not None:
        return failure_result(
            tool="run_remote",
            target=target,
            message="Current working directory is unsafe for remote operations.",
            error_type=cwd_err,
            recoverable=False,
        )

    path_err = verify_remote_path_safety(remote_path)
    if path_err is not None:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Safety check failed for {remote_path}"],
        )

    interactive_hit = check_interactive_command(cmd)
    if interactive_hit is not None:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"Interactive command '{interactive_hit}' rejected in non-interactive remote execution.",
            error_type="interactive_command",
            recoverable=False,
            diagnostics=[f"Command '{cmd}' requires TTY/interactive shell."],
        )

    dangerous_hit = check_dangerous_command(cmd, is_remote=True)
    if dangerous_hit is not None:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"Dangerous command pattern '{dangerous_hit}' rejected.",
            error_type="dangerous_command",
            recoverable=False,
            diagnostics=[f"Command '{cmd}' contains dangerous operations."],
        )



    try:
        validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    except SshConfigError as exc:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"SSH preflight failed for target '{target}'.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            diagnostics=preflight.get("diagnostics", []),
            next_action={"tool": "net_diagnose", "args": {"target": target}},
        )

    diagnostics: list[str] = []
    env_vars: dict[str, str] = {}
    network = target_cfg.get("network") or {}
    reverse_tunnel = network.get("reverse_tunnel") or {}
    if reverse_tunnel.get("enabled", False):
        local_proxy_port = reverse_tunnel.get("local_proxy_port", 7890)
        remote_bind_port = reverse_tunnel.get("remote_bind_port", 10800)
        socket_path = get_tunnel_control_path(
            project_name, target, ssh_host, local_proxy_port, remote_bind_port
        )
        if not _check_tunnel_master(socket_path, ssh_host, jump_host=jump_host):
            start_res_str = tunnel_start(target)
            try:
                start_res = json.loads(start_res_str)
            except Exception:
                start_res = {}
            if not start_res.get("ok"):
                return failure_result(
                    tool="run_remote",
                    target=target,
                    message=start_res.get("message", "Failed to start reverse tunnel."),
                    error_type=start_res.get("error_type", "tunnel_start_failed"),
                    recoverable=start_res.get("recoverable", True),
                    diagnostics=start_res.get("diagnostics", []),
                    next_action=start_res.get("next_action"),
                )

        tunnel_env = get_tunnel_env(target_cfg)
        env_vars.update(tunnel_env)

    user_extra_env = (target_cfg.get("runtime") or {}).get("extra_env") or {}
    for key, val in user_extra_env.items():
        if key in env_vars and env_vars[key] != str(val):
            diagnostics.append(
                f"Env '{key}' from tunnel was overridden by user runtime.extra_env."
            )
        env_vars[key] = str(val)


    env_exports = [f"export {key}={shlex.quote(str(val))}" for key, val in env_vars.items()]
    remote_shell_parts = [f"cd {shlex.quote(remote_path)}"]
    if env_exports:
        remote_shell_parts.extend(env_exports)
    remote_shell_parts.append(cmd)

    remote_shell_cmd = " && ".join(remote_shell_parts)

    timeout = (target_cfg.get("limits") or {}).get("command_timeout_seconds", 300)

    try:
        argv = build_ssh_args(ssh_host, timeout=5, jump_host=jump_host) + [remote_shell_cmd]
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )

        output_raw = (completed.stdout or "") + (completed.stderr or "")
        output_truncated = safe_truncate(output_raw)

        write_audit_log(
            project_name,
            "run_remote",
            f"target={target} ssh_host={ssh_host} exit_code={completed.returncode} cmd={cmd[:50]}",
        )

        if completed.returncode != 0:
            diag_lines = diagnostics + [
                completed.stderr.strip() or f"Command exited with code {completed.returncode}."
            ]
            return failure_result(
                tool="run_remote",
                target=target,
                message=f"Remote command failed with exit code {completed.returncode}.",
                error_type="remote_command_failed",
                recoverable=True,
                data={
                    "target": target,
                    "exit_code": completed.returncode,
                    "output": output_truncated,
                },
                diagnostics=diag_lines,
            )

        return success_result(
            tool="run_remote",
            target=target,
            message=f"Remote command executed successfully on target '{target}'.",
            data={
                "target": target,
                "exit_code": 0,
                "output": output_truncated,
            },
            diagnostics=diagnostics,
        )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="run_remote",
            target=target,
            message=f"Remote command timed out after {timeout} seconds.",
            error_type="command_timeout",
            recoverable=True,
            diagnostics=[f"Command exceeded timeout limit of {timeout}s."],
        )
