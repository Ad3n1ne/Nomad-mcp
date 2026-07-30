"""Daemon, credential environment, and transaction-lock support for Codex."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import ipaddress
import os
import stat
import subprocess
import sys

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nomad import __version__
from nomad.codex_config import CodexConfigError, _error


TOKEN_ENV_PREFIX = "NOMAD_MCP_BEARER_TOKEN_"
HEALTH_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class _ValidatedDaemon:
    pid: int
    url: str
    version: str
    token_env_var: str


def _resolve_project(
    project: str | os.PathLike[str] | None,
) -> Path:
    candidate = Path.cwd() if project is None else Path(project).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        raise _error(
            "invalid_project",
            path=str(candidate),
            message="Project directory does not exist.",
        ) from None
    if not resolved.is_dir():
        raise _error(
            "invalid_project",
            path=str(resolved),
            message="Project path is not a directory.",
        )
    return resolved


def _project_hash(project_root: Path) -> str:
    return hashlib.sha256(os.fsencode(str(project_root))).hexdigest()


def _project_token_env_var(project_root: Path) -> str:
    return f"{TOKEN_ENV_PREFIX}{_project_hash(project_root)[:16].upper()}"


def _daemon_module() -> Any:
    from nomad import daemon

    return daemon


def _daemon_status(
    daemon_module: Any,
    project_root: Path,
) -> Mapping[str, Any]:
    return _daemon_call(
        "daemon_status_failed",
        daemon_module.status_daemon,
        project=project_root,
    )


def _diagnostic_daemon_status(
    daemon_module: Any,
    project_root: Path,
) -> tuple[Mapping[str, Any], str | None]:
    try:
        result = daemon_module.status_daemon(project=project_root)
    except Exception:
        return {"status": "error", "running": False}, "daemon_status_failed"
    if not isinstance(result, Mapping):
        return {"status": "error", "running": False}, "invalid_daemon_state"
    return result, None


def _daemon_call(
    error_type: str,
    function: Any,
    **kwargs: Any,
) -> Mapping[str, Any]:
    try:
        result = function(**kwargs)
    except Exception:
        raise _error(
            error_type,
            message="Daemon lifecycle operation failed.",
        ) from None
    if not isinstance(result, Mapping):
        raise _error(
            "invalid_daemon_state",
            message="Daemon lifecycle operation returned invalid state.",
        )
    return result


def _prepare_daemon(
    state: Mapping[str, Any],
    *,
    project_root: Path,
    repair: bool,
    daemon_module: Any,
) -> tuple[Mapping[str, Any], str, str | None]:
    lifecycle = str(state.get("status", "unknown"))
    if lifecycle == "ownership_mismatch":
        raise _error(
            "daemon_ownership_mismatch",
            project_root=str(project_root),
            message="Recorded daemon ownership does not match its live process.",
        )
    if lifecycle == "stopped":
        started = _daemon_call(
            "daemon_start_failed",
            daemon_module.start_daemon,
            project=project_root,
        )
        newly_started = started.get("already_running") is not True
        instance_id = (
            str(started.get("instance_id"))
            if newly_started and started.get("instance_id")
            else None
        )
        return started, "started" if newly_started else "reused", instance_id
    if lifecycle == "starting":
        if not repair:
            raise _error(
                "daemon_starting",
                project_root=str(project_root),
                message="Daemon is still starting; retry setup later or run repair.",
            )
        restarted = _daemon_call(
            "daemon_restart_failed",
            daemon_module.restart_daemon,
            project=project_root,
        )
        return restarted, "restarted", None
    if lifecycle != "running" or state.get("running") is not True:
        raise _error(
            "daemon_not_ready",
            project_root=str(project_root),
            status=lifecycle,
            message="The project daemon is not in a configurable lifecycle state.",
        )
    return state, "reused", None


def _version_mismatch(
    project_root: Path,
    *,
    repair: bool,
) -> CodexConfigError:
    message = (
        "Repair restarted the daemon but its version still does not match Nomad."
        if repair
        else "Daemon version does not match Nomad; run repair before setup."
    )
    return _error(
        "daemon_version_mismatch",
        project_root=str(project_root),
        expected_version=__version__,
        action="repair",
        message=message,
    )


def _validate_daemon_state(
    state: Mapping[str, Any],
    *,
    project_root: Path,
    daemon_module: Any,
) -> _ValidatedDaemon:
    pid = state.get("pid")
    instance_id = state.get("instance_id")
    host = state.get("host")
    port = state.get("port")
    path = state.get("path")
    url = state.get("url")
    version = state.get("version")
    token_env_var = state.get("token_env_var")
    expected_env_var = _project_token_env_var(project_root)
    valid_scalars = (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 0
        and isinstance(instance_id, str)
        and bool(instance_id)
        and isinstance(host, str)
        and bool(host)
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
        and isinstance(path, str)
        and path.startswith("/")
        and isinstance(url, str)
        and bool(url)
        and isinstance(version, str)
        and bool(version)
        and state.get("status") == "running"
        and state.get("running") is True
        and state.get("auth") is True
        and state.get("allow_remote") is False
        and state.get("project_root") == str(project_root)
        and token_env_var == expected_env_var
    )
    if not valid_scalars or not daemon_module.is_loopback_host(host):
        raise _error(
            "invalid_daemon_state",
            project_root=str(project_root),
            message="Daemon state failed canonical project or endpoint validation.",
        )

    parsed = urlsplit(url)
    try:
        parsed_port = parsed.port
    except ValueError:
        parsed_port = None
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
        or parsed.hostname.lower() != host.lower()
        or parsed_port != port
        or parsed.path != path
        or parsed.query
        or parsed.fragment
        or url != _build_url(host, port, path)
    ):
        raise _error(
            "invalid_daemon_state",
            project_root=str(project_root),
            message="Daemon URL does not match its validated host, port, and path.",
        )
    return _ValidatedDaemon(
        pid=pid,
        url=url,
        version=version,
        token_env_var=token_env_var,
    )


def _build_url(host: str, port: int, path: str) -> str:
    rendered_host = host
    try:
        if ipaddress.ip_address(host).version == 6:
            rendered_host = f"[{host}]"
    except ValueError:
        pass
    return f"http://{rendered_host}:{port}{path}"


def _authenticated_health(
    daemon_state: _ValidatedDaemon,
    *,
    project_root: Path,
    daemon_module: Any,
) -> None:
    health_daemon = getattr(daemon_module, "health_daemon", None)
    if callable(health_daemon):
        try:
            health = health_daemon(
                project=project_root,
                timeout=HEALTH_TIMEOUT_SECONDS,
            )
        except Exception:
            raise _health_failed(project_root) from None
        if (
            not isinstance(health, Mapping)
            or health.get("ok") is not True
            or health.get("pid") != daemon_state.pid
            or health.get("url") != daemon_state.url
            or health.get("version") != daemon_state.version
        ):
            raise _error(
                "daemon_health_pid_mismatch",
                project_root=str(project_root),
                message="Authenticated health identity did not match lifecycle state.",
            )
        return

    try:
        bearer_token = daemon_module.read_daemon_token(project=project_root)
    except Exception:
        raise _error(
            "daemon_token_unavailable",
            project_root=str(project_root),
            message="Daemon authentication token could not be read.",
        ) from None
    try:
        health = daemon_module._mcp_health_data(
            daemon_state.url,
            bearer_token,
            timeout=HEALTH_TIMEOUT_SECONDS,
        )
    except Exception:
        raise _health_failed(project_root) from None
    if not isinstance(health, Mapping) or health.get("pid") != daemon_state.pid:
        raise _error(
            "daemon_health_pid_mismatch",
            project_root=str(project_root),
            message="Authenticated daemon health PID did not match lifecycle state.",
        )


def _health_failed(project_root: Path) -> CodexConfigError:
    return _error(
        "daemon_health_failed",
        project_root=str(project_root),
        message="Authenticated daemon health check failed.",
    )


def _health_error_or_none(
    validated: _ValidatedDaemon,
    *,
    project_root: Path,
    daemon_module: Any,
) -> CodexConfigError | None:
    try:
        _authenticated_health(
            validated,
            project_root=project_root,
            daemon_module=daemon_module,
        )
    except CodexConfigError as exc:
        return exc
    return None


def _rollback_started_daemon(
    *,
    project_root: Path,
    instance_id: str,
    daemon_module: Any,
) -> None:
    try:
        current = daemon_module.status_daemon(project=project_root)
    except Exception:
        return
    if current.get("instance_id") != instance_id:
        return
    try:
        daemon_module.stop_daemon(
            project=project_root,
            expected_instance_id=instance_id,
        )
    except Exception:
        pass


def _token_environment_status(
    token_env_var: str,
    *,
    project_root: Path,
    daemon_module: Any,
) -> dict[str, Any]:
    try:
        bearer_token = daemon_module.read_daemon_token(project=project_root)
    except Exception:
        return _credential_environment_report(
            source_kind="unknown",
            source_status="unknown",
            process_status="unknown",
            login_status="unknown",
            action="manual_host_environment_required",
        )
    return _inspect_credential_source(token_env_var, bearer_token)


def _launchctl_token_status(token_env_var: str, bearer_token: str) -> str:
    try:
        completed = subprocess.run(
            ["launchctl", "getenv", token_env_var],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
        return "unknown"
    value = completed.stdout.rstrip(b"\r\n")
    if not value:
        return "missing"
    try:
        expected = bearer_token.encode("utf-8")
    except UnicodeEncodeError:
        return "unknown"
    return "present" if hmac.compare_digest(value, expected) else "mismatch"


def _configure_token_environment(
    token_env_var: str,
    *,
    project_root: Path,
    daemon_module: Any,
) -> dict[str, Any]:
    try:
        bearer_token = daemon_module.read_daemon_token(project=project_root)
    except Exception:
        raise _error(
            "daemon_token_unavailable",
            project_root=str(project_root),
            config_committed=True,
            message="Daemon authentication token could not be read.",
        ) from None

    return _inspect_credential_source(token_env_var, bearer_token)


def _inspect_credential_source(
    token_env_var: str,
    bearer_token: str,
) -> dict[str, Any]:
    current = os.environ.get(token_env_var)
    if current is None:
        process_status = "missing"
    elif hmac.compare_digest(current, bearer_token):
        process_status = "present"
    else:
        process_status = "mismatch"

    if sys.platform != "darwin":
        return _credential_environment_report(
            source_kind="process_environment",
            source_status=process_status,
            process_status=process_status,
            login_status="not_applicable",
            action=(
                "new_connection_required"
                if process_status == "present"
                else "manual_export_required"
            ),
        )

    login_status = _launchctl_token_status(token_env_var, bearer_token)
    if process_status == "present":
        source_kind = "process_environment"
        source_status = "present"
    elif login_status == "present":
        source_kind = "launchctl_login_environment"
        source_status = "present"
    else:
        source_kind = "none"
        source_status = _unavailable_source_status(
            process_status,
            login_status,
        )
    return _credential_environment_report(
        source_kind=source_kind,
        source_status=source_status,
        process_status=process_status,
        login_status=login_status,
        action=(
            "new_connection_required"
            if source_status == "present"
            else "manual_credential_source_required"
        ),
    )


def _unavailable_source_status(
    process_status: str,
    login_status: str,
) -> str:
    statuses = {process_status, login_status}
    if "mismatch" in statuses:
        return "mismatch"
    if "unknown" in statuses:
        return "unknown"
    return "missing"


def _credential_environment_report(
    *,
    source_kind: str,
    source_status: str,
    process_status: str,
    login_status: str,
    action: str,
) -> dict[str, Any]:
    return {
        "status": source_status,
        "source_kind": source_kind,
        "source_status": source_status,
        "source_ready": source_status == "present",
        "process_environment_status": process_status,
        "login_environment_status": login_status,
        "host_environment_status": "unverified",
        "connection_verified": False,
        "action": action,
        "codex_restart_required": True,
    }


@contextmanager
def _codex_transaction_lock(
    project_root: Path,
    daemon_module: Any,
) -> Iterator[None]:
    daemon_dir = Path(daemon_module.DEFAULT_DAEMONS_DIR)
    _prepare_lock_directory(daemon_dir)
    directory_fd = _open_lock_directory(daemon_dir)
    lock_name = f"{_project_hash(project_root)}.codex.lock"
    try:
        lock_fd = _open_lock_file(directory_fd, lock_name, daemon_dir)
    except BaseException:
        os.close(directory_fd)
        raise
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(lock_fd)
        os.close(directory_fd)


def _prepare_lock_directory(daemon_dir: Path) -> None:
    try:
        daemon_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = daemon_dir.lstat()
    except OSError:
        raise _unsafe_lock(
            daemon_dir,
            "Codex transaction lock directory is unavailable.",
        ) from None
    unsafe_mode = stat.S_IMODE(metadata.st_mode) & 0o022
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or unsafe_mode
    ):
        raise _unsafe_lock(
            daemon_dir,
            "Codex transaction lock directory is not owned and safe.",
        )


def _open_lock_directory(daemon_dir: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        directory_fd = os.open(daemon_dir, flags)
        metadata = os.fstat(directory_fd)
    except OSError:
        raise _unsafe_lock(
            daemon_dir,
            "Codex transaction lock directory could not be opened safely.",
        ) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        os.close(directory_fd)
        raise _unsafe_lock(
            daemon_dir,
            "Codex transaction lock directory changed or is unsafe.",
        )
    return directory_fd


def _open_lock_file(
    directory_fd: int,
    lock_name: str,
    daemon_dir: Path,
) -> int:
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        lock_fd = _open_or_create_lock(directory_fd, lock_name, flags)
        metadata = os.fstat(lock_fd)
    except OSError as exc:
        raise _error(
            "unsafe_lock_path",
            path=str(daemon_dir / lock_name),
            errno=exc.errno,
            message="Codex transaction lock could not be acquired safely.",
        ) from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        os.close(lock_fd)
        raise _unsafe_lock(
            daemon_dir / lock_name,
            "Codex transaction lock file is not owned and private.",
        )
    try:
        os.fchmod(lock_fd, 0o600)
    except OSError:
        os.close(lock_fd)
        raise _unsafe_lock(
            daemon_dir / lock_name,
            "Codex transaction lock permissions could not be secured.",
        ) from None
    return lock_fd


def _open_or_create_lock(
    directory_fd: int,
    lock_name: str,
    flags: int,
) -> int:
    try:
        return os.open(lock_name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        try:
            return os.open(
                lock_name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            return os.open(lock_name, flags, dir_fd=directory_fd)


def _unsafe_lock(path: Path, message: str) -> CodexConfigError:
    return _error(
        "unsafe_lock_path",
        path=str(path),
        message=message,
    )
