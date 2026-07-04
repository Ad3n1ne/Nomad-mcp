"""
Initialization MCP tools.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import subprocess
from datetime import datetime, timezone
import shlex

import json

from nomad.config import (
    ConfigError,
    _normalize_config,
    load_config,
    resolve_target_with_name,
    save_config_file,
    validate_config,
)
from nomad.result import failure_result, success_result
from nomad.schema import get_config_schema_hints
from nomad.security import verify_remote_path_safety
from nomad.ssh import execute_remote_cmd_sync, probe_ssh_connectivity_result


def init_save_config(config_json: str) -> str:
    """Validates configuration parameters and saves them to .nomad.json."""
    try:
        raw_config = json.loads(config_json)
    except (json.JSONDecodeError, TypeError) as exc:
        return failure_result(
            tool="init_save_config",
            message="Invalid JSON string format.",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    try:
        normalized = _normalize_config(raw_config)
        validate_config(normalized)
    except ConfigError as exc:
        return failure_result(
            tool="init_save_config",
            message=f"Configuration validation failed: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    if normalized.get("mode") == "remote":
        targets = normalized.get("targets") or {}
        for t_name, t_cfg in targets.items():
            if t_cfg.get("auto_create_remote_path") is True:
                ssh_host = t_cfg.get("ssh_host")
                remote_path = t_cfg.get("remote_path")
                jump_host = (t_cfg.get("network") or {}).get("jump_host")
                if ssh_host and remote_path:
                    try:
                        ret, stdout, stderr = execute_remote_cmd_sync(
                            ssh_host, f"mkdir -p {shlex.quote(remote_path)}", jump_host=jump_host
                        )
                        if ret != 0:
                            return failure_result(
                                tool="init_save_config",
                                message=f"Failed to auto-create remote directory on target '{t_name}'.",
                                error_type="ssh_unknown_failure",
                                recoverable=True,
                                diagnostics=[stderr.strip() or f"mkdir exit code {ret}"],
                            )
                    except Exception as exc:
                        return failure_result(
                            tool="init_save_config",
                            message=f"SSH error creating remote directory on target '{t_name}': {exc}",
                            error_type="ssh_unknown_failure",
                            recoverable=True,
                            diagnostics=[str(exc)],
                        )

    try:
        saved_path = save_config_file(normalized)
    except ConfigError as exc:
        return failure_result(
            tool="init_save_config",
            message=f"Failed to save configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    return success_result(
        tool="init_save_config",
        message="Configuration saved successfully to .nomad.json",
        data=_build_config_summary(normalized, saved_path),
    )


def _build_config_summary(config: dict[str, Any], saved_path: Path) -> dict[str, Any]:
    targets_summary = {}
    for name, target in (config.get("targets") or {}).items():
        targets_summary[name] = {
            "ssh_host": target.get("ssh_host"),
            "remote_path": target.get("remote_path"),
            "description": target.get("description", ""),
            "local_subpath": target.get("local_subpath"),
        }

    return {
        "path": str(saved_path),
        "mode": config.get("mode"),
        "project_name": config.get("project_name"),
        "default_target": config.get("default_target"),
        "targets": targets_summary,
    }



def init_probe_target(target: str = "default") -> str:
    """Refreshes hardware and runtime snapshot for the specified target."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="init_probe_target",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    mode = config.get("mode")
    if mode == "unconfigured":
        return failure_result(
            tool="init_probe_target",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if mode == "local":
        return failure_result(
            tool="init_probe_target",
            target=target,
            message="Hardware probing is disabled in local mode.",
            error_type="local_mode",
            recoverable=True,
        )

    try:
        target_key, resolved_target = resolve_target_with_name(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="init_probe_target",
            target=target,
            message=f"Target '{target}' not found.",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    ssh_host = resolved_target["ssh_host"]
    remote_path = resolved_target["remote_path"]
    jump_host = (resolved_target.get("network") or {}).get("jump_host")

    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="init_probe_target",
            target=target,
            message=f"SSH preflight connection failed for target '{target_key}'.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            diagnostics=preflight.get("diagnostics", []),
        )

    path_safety_err = verify_remote_path_safety(remote_path)
    if path_safety_err is not None:
        return failure_result(
            tool="init_probe_target",
            target=target,
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Remote path safety check failed for {remote_path}"],
        )

    quoted_path = shlex.quote(remote_path)
    delim = "___NOMAD_PROBE_DELIM___"
    script = f"""echo '{delim}'
uname -srom 2>/dev/null || echo '__unknown__'
echo '{delim}'
nproc 2>/dev/null || echo '1'
echo '{delim}'
free -h 2>/dev/null | grep Mem | awk '{{print $2}}' || echo '__unknown__'
echo '{delim}'
df -h {quoted_path} 2>/dev/null | tail -1 | awk '{{print $4}}' || echo 'path_not_exist'
echo '{delim}'
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo '__no_gpu__'
echo '{delim}'
python3 --version 2>/dev/null || echo '__no_python__'
which python3 2>/dev/null || echo '__not_found__'
echo '{delim}'
conda env list 2>/dev/null | grep -v '^#' || echo '__no_conda__'
echo '{delim}'
find {quoted_path} ~ -maxdepth 4 -name "pyvenv.cfg" 2>/dev/null | head -10 || echo '__no_venv__'
echo '{delim}'
node --version 2>/dev/null || echo '__no_node__'
which node 2>/dev/null || echo '__not_found__'
echo '{delim}'
ls ~/.nvm/versions/node 2>/dev/null || echo '__no_nvm__'
echo '{delim}'
go version 2>/dev/null || echo '__no_go__'
which go 2>/dev/null || echo '__not_found__'
echo '{delim}'
ruby --version 2>/dev/null || echo '__no_ruby__'
which ruby 2>/dev/null || echo '__not_found__'
echo '{delim}'
"""

    try:
        returncode, stdout, stderr = execute_remote_cmd_sync(
            ssh_host, script, timeout=15, jump_host=jump_host
        )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="init_probe_target",
            target=target,
            message=f"Probe script timed out on {ssh_host}.",
            error_type="command_timeout",
            recoverable=True,
            diagnostics=["SSH probe command timed out after 15 seconds."],
        )

    if returncode != 0 and delim not in stdout:
        return failure_result(
            tool="init_probe_target",
            target=target,
            message=f"Failed to execute probe script on {ssh_host}.",
            error_type="ssh_unknown_failure",
            recoverable=True,
            diagnostics=[stderr.strip() or f"Probe script exited with code {returncode}."],
        )

    hardware = _parse_probe_output(stdout, remote_path)
    config["targets"][target_key]["hardware"] = hardware

    save_config_file(config)

    return success_result(
        tool="init_probe_target",
        target=target,
        message=f"Hardware and runtimes refreshed for target '{target_key}'.",
        data={"target": target_key, "hardware": hardware},
    )


PROJECT_MARKERS = [
    ("requirements.txt", "python"),
    ("pyproject.toml", "python"),
    ("package.json", "node"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("Makefile", "make"),
]
PROXY_ENV_KEYS = [
    "ALL_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "http_proxy",
    "https_proxy",
]


def init_discover() -> str:
    """Pre-scan local workspace environment, .ssh/config aliases and local proxies."""
    cwd = Path.cwd()
    data = {
        "state": "discovered",
        "project_name": cwd.name,
        "project_types": _detect_project_types(cwd),
        "gitignore_exists": (cwd / ".gitignore").exists(),
        "ssh_hosts": _read_ssh_hosts(Path.home() / ".ssh" / "config"),
        "network": _detect_proxy(),
        "config_schema": get_config_schema_hints(cwd.name),
    }
    return success_result(
        tool="init_discover",
        message="Local workspace discovered.",
        data=data,
    )


def init_verify_and_probe(ssh_host: str, remote_path: str, jump_host: str = None) -> str:
    """Tests connection and probes target system specifications and runtimes."""
    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="init_verify_and_probe",
            message=f"SSH preflight connection failed for {ssh_host}.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            diagnostics=preflight.get("diagnostics", []),
        )

    path_safety_err = verify_remote_path_safety(remote_path)
    if path_safety_err is not None:
        return failure_result(
            tool="init_verify_and_probe",
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Remote path safety check failed for {remote_path}"],
        )

    quoted_path = shlex.quote(remote_path)
    delim = "___NOMAD_PROBE_DELIM___"
    script = f"""echo '{delim}'
uname -srom 2>/dev/null || echo '__unknown__'
echo '{delim}'
nproc 2>/dev/null || echo '1'
echo '{delim}'
free -h 2>/dev/null | grep Mem | awk '{{print $2}}' || echo '__unknown__'
echo '{delim}'
df -h {quoted_path} 2>/dev/null | tail -1 | awk '{{print $4}}' || echo 'path_not_exist'
echo '{delim}'
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo '__no_gpu__'
echo '{delim}'
python3 --version 2>/dev/null || echo '__no_python__'
which python3 2>/dev/null || echo '__not_found__'
echo '{delim}'
conda env list 2>/dev/null | grep -v '^#' || echo '__no_conda__'
echo '{delim}'
find {quoted_path} ~ -maxdepth 4 -name "pyvenv.cfg" 2>/dev/null | head -10 || echo '__no_venv__'
echo '{delim}'
node --version 2>/dev/null || echo '__no_node__'
which node 2>/dev/null || echo '__not_found__'
echo '{delim}'
ls ~/.nvm/versions/node 2>/dev/null || echo '__no_nvm__'
echo '{delim}'
go version 2>/dev/null || echo '__no_go__'
which go 2>/dev/null || echo '__not_found__'
echo '{delim}'
ruby --version 2>/dev/null || echo '__no_ruby__'
which ruby 2>/dev/null || echo '__not_found__'
echo '{delim}'
"""

    try:
        returncode, stdout, stderr = execute_remote_cmd_sync(
            ssh_host, script, timeout=15, jump_host=jump_host
        )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="init_verify_and_probe",
            message=f"Probe script timed out on {ssh_host}.",
            error_type="command_timeout",
            recoverable=True,
            diagnostics=["SSH probe command timed out after 15 seconds."],
        )

    if returncode != 0 and delim not in stdout:
        return failure_result(
            tool="init_verify_and_probe",
            message=f"Failed to execute probe script on {ssh_host}.",
            error_type="ssh_unknown_failure",
            recoverable=True,
            diagnostics=[stderr.strip() or f"Probe script exited with code {returncode}."],
        )

    hardware = _parse_probe_output(stdout, remote_path)
    return success_result(
        tool="init_verify_and_probe",
        message="Target verified and probed successfully.",
        data={
            "verified": True,
            "ssh_host": ssh_host,
            "remote_path": remote_path,
            "hardware": hardware,
        },
    )


def _parse_probe_output(stdout: str, remote_path: str) -> dict[str, object]:
    delim = "___NOMAD_PROBE_DELIM___"
    parts = stdout.split(delim)

    os_str = parts[1].strip() if len(parts) > 1 and parts[1].strip() != "__unknown__" else "Unknown OS"

    cpu_cores = 1
    if len(parts) > 2:
        try:
            cpu_cores = int(parts[2].strip())
        except ValueError:
            cpu_cores = 1

    memory_raw = parts[3].strip() if len(parts) > 3 else ""
    memory_total = memory_raw if memory_raw and memory_raw != "__unknown__" else "Unknown"

    disk_raw = parts[4].strip() if len(parts) > 4 else ""
    disk_available = disk_raw if disk_raw and disk_raw not in ("path_not_exist", "__unknown__") else "Unknown"


    gpus = _parse_gpus(parts[5]) if len(parts) > 5 else []

    runtimes = []
    if len(parts) > 6:
        _parse_python_system(parts[6], runtimes)
    if len(parts) > 7:
        _parse_conda_envs(parts[7], runtimes)
    if len(parts) > 8:
        _parse_venvs(parts[8], runtimes)
    if len(parts) > 9:
        _parse_node_system(parts[9], runtimes)
    if len(parts) > 10:
        _parse_nvm(parts[10], runtimes)
    if len(parts) > 11:
        _parse_go_system(parts[11], runtimes)
    if len(parts) > 12:
        _parse_ruby_system(parts[12], runtimes)

    probed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "os": os_str,
        "cpu_cores": cpu_cores,
        "memory_total": memory_total,
        "disk_available": disk_available,
        "gpu": gpus,
        "detected_runtimes": runtimes,
        "probed_at": probed_at,
    }


def _parse_gpus(gpu_raw: str) -> list[dict[str, str]]:
    gpus = []
    for line in gpu_raw.strip().splitlines():
        line = line.strip()
        if not line or "__no_gpu__" in line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            gpus.append({"name": parts[0], "memory_total": parts[1]})
        elif len(parts) == 1:
            gpus.append({"name": parts[0], "memory_total": "unknown"})
    return gpus


def _parse_python_system(raw: str, runtimes: list[dict[str, str]]) -> None:
    if "__no_python__" in raw:
        return
    version = "unknown"
    bin_path = None
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("Python "):
            version = line.split("Python ")[1].strip()
        elif line.startswith("/") or line.startswith("~"):
            bin_path = line
    if bin_path:
        runtimes.append({
            "lang": "python",
            "type": "system",
            "name": "system",
            "bin": bin_path,
            "version": version,
        })


def _parse_conda_envs(raw: str, runtimes: list[dict[str, str]]) -> None:
    if "__no_conda__" in raw:
        return
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = line.split()
        if not tokens:
            continue
        env_name = tokens[0]
        env_path = tokens[-1]
        if env_path.startswith("/") or env_path.startswith("~"):
            runtimes.append({
                "lang": "python",
                "type": "conda",
                "name": env_name,
                "bin": f"{env_path}/bin/python",
                "version": "unknown",
            })


def _parse_venvs(raw: str, runtimes: list[dict[str, str]]) -> None:
    if "__no_venv__" in raw:
        return
    for line in raw.strip().splitlines():
        cfg_path = line.strip()
        if cfg_path and "pyvenv.cfg" in cfg_path:
            venv_dir = Path(cfg_path).parent
            runtimes.append({
                "lang": "python",
                "type": "venv",
                "name": venv_dir.name,
                "bin": str(venv_dir / "bin" / "python"),
                "version": "unknown",
            })


def _parse_node_system(raw: str, runtimes: list[dict[str, str]]) -> None:
    if "__no_node__" in raw:
        return
    version = "unknown"
    bin_path = None
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("v") or ("." in line and not line.startswith("/")):
            version = line.lstrip("v")
        elif line.startswith("/") or line.startswith("~"):
            bin_path = line
    if bin_path:
        runtimes.append({
            "lang": "node",
            "type": "system",
            "name": "system",
            "bin": bin_path,
            "version": version,
        })


def _parse_nvm(raw: str, runtimes: list[dict[str, str]]) -> None:
    if "__no_nvm__" in raw:
        return
    for line in raw.strip().splitlines():
        ver = line.strip()
        if ver and (ver.startswith("v") or ver[0].isdigit()):
            clean_v = ver.lstrip("v")
            runtimes.append({
                "lang": "node",
                "type": "nvm",
                "name": f"nvm: {ver}",
                "bin": f"~/.nvm/versions/node/{ver}/bin/node",
                "version": clean_v,
            })


def _parse_go_system(raw: str, runtimes: list[dict[str, str]]) -> None:
    if "__no_go__" in raw:
        return
    version = "unknown"
    bin_path = None
    for line in raw.strip().splitlines():
        line = line.strip()
        if "go version" in line:
            parts = line.split()
            for p in parts:
                if p.startswith("go1.") or p.startswith("go2."):
                    version = p.lstrip("go")
        elif line.startswith("/") or line.startswith("~"):
            bin_path = line
    if bin_path:
        runtimes.append({
            "lang": "go",
            "type": "system",
            "name": "system",
            "bin": bin_path,
            "version": version,
        })


def _parse_ruby_system(raw: str, runtimes: list[dict[str, str]]) -> None:
    if "__no_ruby__" in raw:
        return
    version = "unknown"
    bin_path = None
    for line in raw.strip().splitlines():
        line = line.strip()
        if line.startswith("ruby "):
            version = line.split()[1]
        elif line.startswith("/") or line.startswith("~"):
            bin_path = line
    if bin_path:
        runtimes.append({
            "lang": "ruby",
            "type": "system",
            "name": "system",
            "bin": bin_path,
            "version": version,
        })



def _detect_project_types(cwd: Path) -> list[str]:
    project_types = []
    seen = set()
    for filename, project_type in PROJECT_MARKERS:
        if (cwd / filename).exists() and project_type not in seen:
            project_types.append(project_type)
            seen.add(project_type)
    return project_types


def _read_ssh_hosts(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []

    hosts = set()
    for raw_line in config_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if not parts or parts[0].lower() != "host":
            continue
        for alias in parts[1:]:
            if _is_concrete_ssh_alias(alias):
                hosts.add(alias)
    return sorted(hosts)


def _is_concrete_ssh_alias(alias: str) -> bool:
    return not (
        alias.startswith("!")
        or "*" in alias
        or "?" in alias
        or "[" in alias
        or "]" in alias
    )


def _detect_proxy() -> dict[str, object]:
    for key in PROXY_ENV_KEYS:
        proxy_url = os.environ.get(key)
        if not proxy_url:
            continue
        parsed = _parse_proxy(proxy_url)
        return {
            "proxy_detected": True,
            "proxy_env_key": key,
            "proxy_url": parsed["proxy_url"],
            "proxy_scheme": parsed["proxy_scheme"],
            "proxy_host": parsed["proxy_host"],
            "proxy_port": parsed["proxy_port"],
        }
    return {
        "proxy_detected": False,
        "proxy_env_key": None,
        "proxy_url": None,
        "proxy_scheme": None,
        "proxy_host": None,
        "proxy_port": None,
    }


def _parse_proxy(proxy_url: str) -> dict[str, object]:
    parsed = urlparse(proxy_url)
    if parsed.scheme and parsed.hostname:
        return {
            "proxy_url": _redact_proxy_url(parsed),
            "proxy_scheme": parsed.scheme,
            "proxy_host": parsed.hostname,
            "proxy_port": _parse_proxy_port(proxy_url),
        }
    return {
        "proxy_url": proxy_url,
        "proxy_scheme": None,
        "proxy_host": None,
        "proxy_port": _parse_proxy_port(proxy_url),
    }


def _redact_proxy_url(parsed) -> str:
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
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
