"""
tmux async task management and helper tools for nomad.
"""
from __future__ import annotations

import base64
import json
import re
import shlex
import subprocess

from nomad.config import ConfigError, guard_remote, load_config, resolve_target_with_name
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


TASK_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")



def validate_task_name(task_name: str) -> None:
    """Validates that a task name contains only allowed characters and is of correct length."""
    if not isinstance(task_name, str) or not TASK_NAME_RE.fullmatch(task_name):
        raise ValueError(
            f"invalid task name '{task_name}': must match ^[a-z0-9_-]{{1,40}}$"
        )


def get_session_name(project_name: str, target_name: str, task_name: str) -> str:
    """Synthesizes a unique tmux session name and validates total length limit (<= 100)."""
    validate_task_name(task_name)
    session = f"{project_name}_{target_name}_{task_name}"
    if len(session) > 100:
        raise ValueError(
            f"session name exceeds 100 characters (length {len(session)}): '{session}'"
        )
    return session


def generate_task_script(
    remote_path: str, env_vars: dict[str, str], cmd: str, exit_file: str
) -> str:
    """Generates a bash script that exports variables, base64-decodes the user command,

    and writes the exit status code to exit_file.
    """
    cmd_b64 = base64.b64encode(cmd.encode("utf-8")).decode("utf-8")
    exports = []
    for k, v in sorted(env_vars.items()):
        if not isinstance(k, str) or not ENV_KEY_RE.fullmatch(k):
            raise ValueError(
                f"invalid env key '{k}': must match ^[A-Z_][A-Z0-9_]*$"
            )
        if not isinstance(v, str):
            raise ValueError(
                f"invalid env value for '{k}': must be a string"
            )
        exports.append(f"export {k}={shlex.quote(v)}")
    
    exports_str = "\n".join(exports)
    
    script = f"""#!/usr/bin/env bash
cd {shlex.quote(remote_path)} || exit 1
{exports_str}
echo {cmd_b64} | base64 -d | bash
echo $? > {shlex.quote(exit_file)}
"""
    return script


def task_start(cmd: str, task_name: str, target: str = "default") -> str:
    """Starts a long-running remote command under tmux and returns immediately.

    Prefer this over run_remote for uploads, builds, training, servers, scans, and
    any command that may run longer than a short synchronous probe.
    """
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="task_start",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="task_start",
            target=target,
            message="Remote tasks are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    try:
        target_name, target_cfg = resolve_target_with_name(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    project_name = config.get("project_name", "unnamed")
    ssh_host = target_cfg["ssh_host"]
    remote_path = target_cfg["remote_path"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")

    cwd_err = verify_local_cwd_safety()
    if cwd_err is not None:
        return failure_result(
            tool="task_start",
            target=target,
            message="Current working directory is unsafe for remote operations.",
            error_type=cwd_err,
            recoverable=False,
        )

    path_err = verify_remote_path_safety(remote_path)
    if path_err is not None:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Safety check failed for {remote_path}"],
        )

    try:
        session_name = get_session_name(project_name, target_name, task_name)
    except ValueError as exc:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Invalid task parameters: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    interactive_hit = check_interactive_command(cmd)
    if interactive_hit is not None:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Interactive command '{interactive_hit}' rejected in non-interactive remote execution.",
            error_type="interactive_command",
            recoverable=False,
            diagnostics=[f"Command '{cmd}' requires TTY/interactive shell."],
        )

    dangerous_hit = check_dangerous_command(cmd, is_remote=True)
    if dangerous_hit is not None:
        return failure_result(
            tool="task_start",
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
            tool="task_start",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="task_start",
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
            project_name, target_name, ssh_host, local_proxy_port, remote_bind_port
        )
        if not _check_tunnel_master(socket_path, ssh_host, jump_host=jump_host):
            start_res_str = tunnel_start(target_name)
            try:
                start_res = json.loads(start_res_str)
            except Exception:
                start_res = {}
            if not start_res.get("ok"):
                underlying_error = start_res.get("error_type", "tunnel_start_failed")
                diag_msg = start_res.get("diagnostics") or []
                diag_lines = [f"Underlying error type: {underlying_error}"] + list(diag_msg)
                return failure_result(
                    tool="task_start",
                    target=target,
                    message=start_res.get("message", "Failed to start reverse tunnel."),
                    error_type="tunnel_start_failed",
                    recoverable=start_res.get("recoverable", True),
                    diagnostics=diag_lines,
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

    try:
        check_tmux_argv = build_ssh_args(ssh_host, timeout=5, jump_host=jump_host) + [
            f"tmux has-session -t {shlex.quote(session_name)}"
        ]
        tmux_check_res = subprocess.run(check_tmux_argv, capture_output=True, text=True, timeout=10)
        if tmux_check_res.returncode == 0:
            return failure_result(
                tool="task_start",
                target=target,
                message=f"Task session '{session_name}' already exists and is active.",
                error_type="task_exists",
                recoverable=True,
            )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="task_start",
            target=target,
            message="Timeout checking tmux session status on remote host.",
            error_type="command_timeout",
            recoverable=True,
            diagnostics=[f"tmux has-session timed out for target '{target}'."],
        )
    except OSError as exc:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Failed to launch tmux status check over SSH: {exc}",
            error_type="remote_command_failed",
            recoverable=True,
            diagnostics=[str(exc)],
        )


    tasks_dir = f"{remote_path.rstrip('/')}/.nomad/tasks"
    script_path = f"{tasks_dir}/{session_name}.sh"
    log_path = f"{tasks_dir}/{session_name}.log"
    exit_path = f"{tasks_dir}/{session_name}.exit"

    try:
        script_content = generate_task_script(remote_path, env_vars, cmd, exit_path)
    except ValueError as exc:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Failed to generate task script: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    script_b64 = base64.b64encode(script_content.encode("utf-8")).decode("utf-8")

    write_remote_cmd = (
        f"mkdir -p {shlex.quote(tasks_dir)} && "
        f"echo {shlex.quote(script_b64)} | base64 -d > {shlex.quote(script_path)} && "
        f"chmod +x {shlex.quote(script_path)}"
    )

    try:
        write_argv = build_ssh_args(ssh_host, timeout=10, jump_host=jump_host) + [write_remote_cmd]
        write_res = subprocess.run(write_argv, capture_output=True, text=True, timeout=15)
        if write_res.returncode != 0:
            return failure_result(
                tool="task_start",
                target=target,
                message="Failed to write task script to remote host.",
                error_type="remote_command_failed",
                recoverable=True,
                diagnostics=[write_res.stderr.strip() or f"SSH exit code {write_res.returncode}"],
            )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="task_start",
            target=target,
            message="Timeout writing task script to remote host.",
            error_type="command_timeout",
            recoverable=True,
        )
    except OSError as exc:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Failed to launch SSH while writing task script: {exc}",
            error_type="remote_command_failed",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    start_tmux_cmd = (
        f"tmux new-session -d -s {shlex.quote(session_name)} "
        f"\"exec bash {shlex.quote(script_path)} > {shlex.quote(log_path)} 2>&1\""
    )

    try:
        start_argv = build_ssh_args(ssh_host, timeout=10, jump_host=jump_host) + [start_tmux_cmd]
        start_res = subprocess.run(start_argv, capture_output=True, text=True, timeout=15)
        if start_res.returncode != 0:
            return failure_result(
                tool="task_start",
                target=target,
                message=f"Failed to start tmux session: {start_res.stderr.strip()}",
                error_type="remote_command_failed",
                recoverable=True,
                diagnostics=[start_res.stderr.strip() or f"tmux exit code {start_res.returncode}"],
            )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="task_start",
            target=target,
            message="Timeout starting tmux session on remote host.",
            error_type="command_timeout",
            recoverable=True,
        )
    except OSError as exc:
        return failure_result(
            tool="task_start",
            target=target,
            message=f"Failed to launch SSH while starting tmux session: {exc}",
            error_type="remote_command_failed",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    write_audit_log(
        project_name,
        "task_start",
        f"target={target_name} ssh_host={ssh_host} session={session_name} cmd={cmd[:50]}",
    )

    return success_result(
        tool="task_start",
        target=target,
        message=f"Task '{task_name}' started successfully in tmux session '{session_name}'.",
        data={
            "target": target_name,
            "session_name": session_name,
            "script_path": script_path,
            "log_path": log_path,
            "exit_path": exit_path,
        },
        diagnostics=diagnostics,
    )


def task_status(task_name: str, target: str = "default", tail_lines: int = 10) -> str:
    """Checks the status and reads log tail of a tmux long-running task."""
    if not isinstance(tail_lines, int) or isinstance(tail_lines, bool) or not (1 <= tail_lines <= 500):
        return failure_result(
            tool="task_status",
            target=target,
            message="Invalid tail_lines. It must be an integer between 1 and 500.",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[f"tail_lines={tail_lines} is not in [1, 500]."],
        )

    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="task_status",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="task_status",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="task_status",
            target=target,
            message="Remote tasks are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    try:
        target_name, target_cfg = resolve_target_with_name(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="task_status",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    project_name = config.get("project_name", "unnamed")
    ssh_host = target_cfg["ssh_host"]
    remote_path = target_cfg["remote_path"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")

    cwd_err = verify_local_cwd_safety()
    if cwd_err is not None:
        return failure_result(
            tool="task_status",
            target=target,
            message="Current working directory is unsafe for remote operations.",
            error_type=cwd_err,
            recoverable=False,
        )

    path_err = verify_remote_path_safety(remote_path)
    if path_err is not None:
        return failure_result(
            tool="task_status",
            target=target,
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Safety check failed for {remote_path}"],
        )

    try:
        session_name = get_session_name(project_name, target_name, task_name)
    except ValueError as exc:
        return failure_result(
            tool="task_status",
            target=target,
            message=f"Invalid task parameters: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    try:
        validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    except SshConfigError as exc:
        return failure_result(
            tool="task_status",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="task_status",
            target=target,
            message=f"SSH preflight failed for target '{target}'.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            diagnostics=preflight.get("diagnostics", []),
            next_action={"tool": "net_diagnose", "args": {"target": target}},
        )

    tasks_dir = f"{remote_path.rstrip('/')}/.nomad/tasks"
    exit_path = f"{tasks_dir}/{session_name}.exit"
    log_path = f"{tasks_dir}/{session_name}.log"

    remote_check_cmd = f"""
session_name={shlex.quote(session_name)}
exit_path={shlex.quote(exit_path)}
log_path={shlex.quote(log_path)}

if tmux has-session -t "$session_name" 2>/dev/null; then
    echo "running"
else
    if [ -f "$exit_path" ]; then
        exit_code=$(cat "$exit_path" 2>/dev/null | tr -d '[:space:]')
        if [ "$exit_code" = "0" ]; then
            echo "finished_success"
        else
            echo "finished_error:$exit_code"
        fi
    elif [ -f "$log_path" ]; then
        echo "unknown:log_exists_without_exit"
    else
        echo "missing"
    fi
fi

if [ -f "$log_path" ]; then
    echo "---LOG_START---"
    tail -n {int(tail_lines)} "$log_path" 2>/dev/null
fi
"""

    status = "unknown"
    exit_code = None
    output = ""
    diagnostics = []

    try:
        argv = build_ssh_args(ssh_host, timeout=5, jump_host=jump_host) + [remote_check_cmd]
        res = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            lines = res.stdout.splitlines()
            if lines:
                first_line = lines[0].strip()
                if first_line == "running":
                    status = "running"
                elif first_line == "finished_success":
                    status = "finished_success"
                    exit_code = 0
                elif first_line.startswith("finished_error:"):
                    status = "finished_error"
                    try:
                        exit_code = int(first_line.split(":")[1])
                    except Exception:
                        exit_code = -1
                elif first_line == "missing":
                    status = "missing"
                elif first_line.startswith("unknown:"):
                    status = "unknown"
                    diagnostics.append(f"Inconsistent task state: {first_line}")
                else:
                    status = "unknown"
                    diagnostics.append(f"Unrecognized status output: {first_line}")
            
            if "---LOG_START---" in res.stdout:
                parts = res.stdout.split("---LOG_START---")
                output = parts[1].strip()
        else:
            diagnostics.append(res.stderr.strip() or f"SSH exited with code {res.returncode}")
    except subprocess.TimeoutExpired:
        diagnostics.append("Status check timeout.")
    except OSError as exc:
        diagnostics.append(f"Failed to launch status check over SSH: {exc}")
    except Exception as exc:
        diagnostics.append(str(exc))

    from nomad.truncate import safe_truncate
    output_truncated = safe_truncate(output)

    return success_result(
        tool="task_status",
        target=target,
        message=f"Task '{task_name}' status on target '{target_name}': {status}",
        data={
            "target": target_name,
            "task_name": task_name,
            "session_name": session_name,
            "status": status,
            "exit_code": exit_code,
            "output": output_truncated,
        },
        diagnostics=diagnostics,
    )


def task_list(target: str | None = None) -> str:
    """Lists long-running tasks for the given target, or all targets if None."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="task_list",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="task_list",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="task_list",
            target=target,
            message="Remote tasks are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    cwd_err = verify_local_cwd_safety()
    if cwd_err is not None:
        return failure_result(
            tool="task_list",
            target=target,
            message="Current working directory is unsafe for remote operations.",
            error_type=cwd_err,
            recoverable=False,
        )

    project_name = config.get("project_name", "unnamed")
    
    targets_to_probe = []
    if target is not None:
        try:
            target_name, target_cfg = resolve_target_with_name(config, target)
            targets_to_probe.append((target_name, target_cfg))
        except ConfigError as exc:
            return failure_result(
                tool="task_list",
                target=target,
                message=f"Target '{target}' not found: {exc}",
                error_type="target_not_found",
                recoverable=True,
                diagnostics=[str(exc)],
            )
    else:
        for t_name, t_cfg in (config.get("targets") or {}).items():
            targets_to_probe.append((t_name, t_cfg))

    all_tasks = []
    diagnostics = []

    for t_name, t_cfg in targets_to_probe:
        ssh_host = t_cfg["ssh_host"]
        remote_path = t_cfg["remote_path"]
        jump_host = (t_cfg.get("network") or {}).get("jump_host")

        path_err = verify_remote_path_safety(remote_path)
        if path_err is not None:
            if target is not None:
                return failure_result(
                    tool="task_list",
                    target=target,
                    message=f"Remote path '{remote_path}' is unsafe or invalid.",
                    error_type="unsafe_remote_path",
                    recoverable=True,
                    diagnostics=[f"Safety check failed for {remote_path}"],
                )
            diagnostics.append(f"Target '{t_name}' remote path '{remote_path}' is unsafe or invalid.")
            continue

        try:
            validate_ssh_endpoint(ssh_host, jump_host=jump_host)
        except SshConfigError as exc:
            if target is not None:
                return failure_result(
                    tool="task_list",
                    target=target,
                    message=f"Invalid SSH configuration for target '{target}': {exc}",
                    error_type="invalid_config",
                    recoverable=True,
                    diagnostics=[str(exc)],
                )
            diagnostics.append(f"Target '{t_name}' SSH config invalid: {exc}")
            continue

        preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
        if not preflight["ok"]:
            if target is not None:
                return failure_result(
                    tool="task_list",
                    target=target,
                    message=f"SSH preflight failed for target '{target}'.",
                    error_type=preflight.get("error_type", "ssh_unknown_failure"),
                    recoverable=preflight.get("recoverable", True),
                    diagnostics=preflight.get("diagnostics", []),
                    next_action={"tool": "net_diagnose", "args": {"target": target}},
                )
            diagnostics.append(f"Target '{t_name}' unreachable: {preflight.get('message')}")
            continue

        tasks_dir = f"{remote_path.rstrip('/')}/.nomad/tasks"
        prefix = f"{project_name}_{t_name}_"

        scan_cmd = f"""
prefix={shlex.quote(prefix)}
tasks_dir={shlex.quote(tasks_dir)}

if command -v tmux >/dev/null 2>&1; then
    tmux list-sessions -F '#S' 2>/dev/null | grep "^$prefix" || true
fi
echo "---FILES_START---"
if [ -d "$tasks_dir" ]; then
    ls -1 "$tasks_dir" 2>/dev/null | grep "^$prefix" || true
fi
"""

        try:
            argv = build_ssh_args(ssh_host, timeout=5, jump_host=jump_host) + [scan_cmd]
            res = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                stdout_parts = res.stdout.split("---FILES_START---")
                running_sessions = set()
                if stdout_parts[0].strip():
                    for line in stdout_parts[0].splitlines():
                        if line.strip():
                            running_sessions.add(line.strip())

                found_task_names = set()

                if len(stdout_parts) > 1 and stdout_parts[1].strip():
                    for file_name in stdout_parts[1].splitlines():
                        file_name = file_name.strip()
                        if not file_name:
                            continue
                        if file_name.startswith(prefix):
                            rest = file_name[len(prefix):]
                            if rest.endswith(".sh"):
                                t_task = rest[:-3]
                            elif rest.endswith(".exit"):
                                t_task = rest[:-5]
                            elif rest.endswith(".log"):
                                t_task = rest[:-4]
                            else:
                                continue
                            if t_task:
                                found_task_names.add(t_task)

                for s_name in running_sessions:
                    t_task = s_name[len(prefix):]
                    if t_task:
                        found_task_names.add(t_task)

                for t_task in sorted(found_task_names):
                    s_name = f"{prefix}{t_task}"
                    if s_name in running_sessions:
                        status = "running"
                    else:
                        status = "finished"
                    all_tasks.append({
                        "task_name": t_task,
                        "target": t_name,
                        "session_name": s_name,
                        "status": status,
                    })
            else:
                diagnostics.append(f"Scan failed on target '{t_name}': {res.stderr.strip()}")
        except subprocess.TimeoutExpired:
            diagnostics.append(f"Scan timed out on target '{t_name}'.")
        except OSError as exc:
            diagnostics.append(f"Failed to launch scan on target '{t_name}': {exc}")
        except Exception as exc:
            diagnostics.append(f"Scan failed on target '{t_name}': {exc}")

    return success_result(
        tool="task_list",
        target=target,
        message="Listed tasks successfully.",
        data={
            "tasks": all_tasks,
        },
        diagnostics=diagnostics,
    )


def task_kill(task_name: str, target: str = "default") -> str:
    """Terminates an active tmux session for a task without removing logs/exits or stopping tunnel."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="task_kill",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="task_kill",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="task_kill",
            target=target,
            message="Remote tasks are disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    try:
        target_name, target_cfg = resolve_target_with_name(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="task_kill",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    project_name = config.get("project_name", "unnamed")
    ssh_host = target_cfg["ssh_host"]
    remote_path = target_cfg["remote_path"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")

    cwd_err = verify_local_cwd_safety()
    if cwd_err is not None:
        return failure_result(
            tool="task_kill",
            target=target,
            message="Current working directory is unsafe for remote operations.",
            error_type=cwd_err,
            recoverable=False,
        )

    path_err = verify_remote_path_safety(remote_path)
    if path_err is not None:
        return failure_result(
            tool="task_kill",
            target=target,
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Safety check failed for {remote_path}"],
        )

    try:
        session_name = get_session_name(project_name, target_name, task_name)
    except ValueError as exc:
        return failure_result(
            tool="task_kill",
            target=target,
            message=f"Invalid task parameters: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    try:
        validate_ssh_endpoint(ssh_host, jump_host=jump_host)
    except SshConfigError as exc:
        return failure_result(
            tool="task_kill",
            target=target,
            message=f"Invalid SSH configuration for target '{target}': {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="task_kill",
            target=target,
            message=f"SSH preflight failed for target '{target}'.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            diagnostics=preflight.get("diagnostics", []),
            next_action={"tool": "net_diagnose", "args": {"target": target}},
        )

    kill_cmd = f"tmux kill-session -t {shlex.quote(session_name)} 2>/dev/null || true"

    try:
        argv = build_ssh_args(ssh_host, timeout=10, jump_host=jump_host) + [kill_cmd]
        res = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            return failure_result(
                tool="task_kill",
                target=target,
                message=f"Failed to kill tmux session: {res.stderr.strip()}",
                error_type="remote_command_failed",
                recoverable=True,
                diagnostics=[res.stderr.strip() or f"exit code {res.returncode}"],
            )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="task_kill",
            target=target,
            message="Timeout killing tmux session on remote host.",
            error_type="command_timeout",
            recoverable=True,
        )
    except OSError as exc:
        return failure_result(
            tool="task_kill",
            target=target,
            message=f"Failed to launch SSH while killing tmux session: {exc}",
            error_type="remote_command_failed",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    write_audit_log(
        project_name,
        "task_kill",
        f"target={target_name} ssh_host={ssh_host} session={session_name}",
    )

    return success_result(
        tool="task_kill",
        target=target,
        message=f"Terminated tmux session '{session_name}' successfully.",
        data={
            "target": target_name,
            "session_name": session_name,
        },
    )
