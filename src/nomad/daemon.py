"""Project-scoped lifecycle management for persistent Nomad MCP servers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Iterator, Mapping

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from nomad import __version__


SCHEMA_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_DAEMONS_DIR = Path.home() / ".nomad" / "daemons"
PROJECT_PORT_MIN = 49152
PROJECT_PORT_MAX = 65535
BEARER_TOKEN_ENV_VAR = "NOMAD_MCP_BEARER_TOKEN"
START_TIMEOUT_SECONDS = 10.0
LAUNCH_CLAIM_TIMEOUT_SECONDS = 2.0
STARTING_RECOVERY_TIMEOUT_SECONDS = 2.0
STOP_TIMEOUT_SECONDS = 5.0
POLL_INTERVAL_SECONDS = 0.1


class DaemonError(RuntimeError):
    """Raised when a daemon lifecycle operation cannot complete safely."""


class DaemonOwnershipError(DaemonError):
    """Raised when a state PID does not belong to the recorded daemon instance."""


def start_daemon(
    *,
    project: str | os.PathLike[str] | None = None,
    host: str = DEFAULT_HOST,
    port: int | None = None,
    path: str = DEFAULT_PATH,
    allow_remote: bool = False,
) -> dict[str, Any]:
    """Starts one persistent Nomad daemon for a project, idempotently."""
    if allow_remote:
        raise DaemonError(
            "--allow-remote is not supported; Nomad 0.2.0 HTTP daemons are "
            "restricted to loopback until TLS support is available"
        )
    project_root = resolve_project(project)
    _validate_endpoint(host, DEFAULT_PORT if port is None else port, path)
    host = host.strip()
    if not is_loopback_host(host):
        raise DaemonError(
            f"refusing non-loopback host {host!r}; Nomad 0.2.0 HTTP daemons "
            "are restricted to loopback until TLS support is available"
        )

    paths = _project_paths(project_root)
    with _project_lock(paths["lock"]):
        return _start_locked(
            project_root=project_root,
            paths=paths,
            host=host,
            port=port,
            path=path,
        )


def status_daemon(
    *, project: str | os.PathLike[str] | None = None
) -> dict[str, Any]:
    """Returns the current project daemon state and cleans dead stale state."""
    project_root = resolve_project(project)
    paths = _project_paths(project_root)
    with _project_lock(paths["lock"]):
        state = _read_state(paths["state"], project_root)
        if state is None:
            return _stopped_result(project_root)

        if state.get("lifecycle") == "launching":
            state = _recover_launching_state(state, paths, project_root)
            if state is None:
                return _stopped_result(project_root, stale_state_removed=True)

        pid = int(state["pid"])
        if not _pid_is_alive(pid):
            _remove_state(paths["state"])
            return _stopped_result(project_root, stale_state_removed=True)

        if not _process_owns_instance(pid, str(state["instance_id"])):
            return {
                "status": "ownership_mismatch",
                "running": False,
                "project_root": str(project_root),
                "pid": pid,
                "instance_id": state["instance_id"],
                "message": "Recorded PID is alive but is not the recorded Nomad daemon instance.",
            }

        state = _recover_starting_state(state, paths)
        return _state_result(state, already_running=True)


def stop_daemon(
    *,
    project: str | os.PathLike[str] | None = None,
    timeout: float = STOP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Stops the project daemon after validating process ownership."""
    project_root = resolve_project(project)
    paths = _project_paths(project_root)
    with _project_lock(paths["lock"]):
        return _stop_locked(project_root=project_root, paths=paths, timeout=timeout)


def restart_daemon(
    *,
    project: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Restarts a project daemon, preserving its recorded endpoint."""
    project_root = resolve_project(project)
    paths = _project_paths(project_root)
    with _project_lock(paths["lock"]):
        state = _read_state(paths["state"], project_root)
        recorded_host = state.get("host", DEFAULT_HOST) if state else DEFAULT_HOST
        endpoint = {
            "host": (
                recorded_host
                if isinstance(recorded_host, str) and is_loopback_host(recorded_host)
                else DEFAULT_HOST
            ),
            "port": state.get("port") if state else None,
            "path": state.get("path", DEFAULT_PATH) if state else DEFAULT_PATH,
        }
        if state is not None:
            _stop_locked(
                project_root=project_root,
                paths=paths,
                timeout=STOP_TIMEOUT_SECONDS,
            )
        result = _start_locked(project_root=project_root, paths=paths, **endpoint)
        result["restarted"] = state is not None
        return result


def read_daemon_token(
    *, project: str | os.PathLike[str] | None = None
) -> str:
    """Reads the project daemon bearer token without exposing its path."""
    project_root = resolve_project(project)
    paths = _project_paths(project_root)
    with _project_lock(paths["lock"]):
        if not paths["token"].exists():
            raise DaemonError(
                "daemon authentication token is not initialized; "
                "run 'nomad daemon start' for this project first"
            )
        return _read_token(paths["token"])


def resolve_project(project: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path.cwd() if project is None else Path(project).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DaemonError(f"project directory does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise DaemonError(f"project path is not a directory: {resolved}")
    return resolved


def claim_daemon_state(
    state_path: str | os.PathLike[str],
    instance_id: str,
) -> dict[str, Any]:
    """Claims a parent-created launching state from the daemon child process."""
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise DaemonError("daemon instance id is invalid")

    project_root = resolve_project()
    paths = _project_paths(project_root)
    expected_path = paths["state"]
    raw_state_path = Path(state_path).expanduser()
    if not raw_state_path.is_absolute():
        raise DaemonError("daemon state path must be absolute")
    supplied_path = Path(os.path.abspath(os.fspath(raw_state_path)))
    if supplied_path != expected_path:
        raise DaemonError("daemon state path does not match the current project")

    with _project_lock(paths["claim_lock"]):
        state = _read_claimable_state(expected_path, project_root)
        if state.get("instance_id") != instance_id:
            raise DaemonError("daemon state instance id does not match")
        if state.get("lifecycle") != "launching" or state.get("pid") != 0:
            raise DaemonError("daemon state is not claimable")

        claimed = dict(state)
        claimed["pid"] = os.getpid()
        claimed["lifecycle"] = "starting"
        _write_state(expected_path, claimed)
        return claimed


def project_default_port(project: str | os.PathLike[str] | Path) -> int:
    """Maps a resolved project path to a stable private-use high port."""
    project_root = Path(project).expanduser().resolve(strict=True)
    digest = hashlib.sha256(os.fsencode(str(project_root))).digest()
    port_count = PROJECT_PORT_MAX - PROJECT_PORT_MIN + 1
    return PROJECT_PORT_MIN + int.from_bytes(digest[:8], "big") % port_count


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_endpoint(host: str, port: int, path: str) -> None:
    if not isinstance(host, str) or not host.strip() or "\x00" in host:
        raise DaemonError("host must be a non-empty hostname or IP address")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DaemonError("port must be an integer between 1 and 65535")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or any(character in path for character in ("\x00", "\r", "\n"))
    ):
        raise DaemonError("path must start with '/' and contain no control line breaks")


def _start_locked(
    *,
    project_root: Path,
    paths: Mapping[str, Path],
    host: str,
    port: int | None,
    path: str,
) -> dict[str, Any]:
    if not is_loopback_host(host):
        raise DaemonError(
            "Nomad 0.2.0 HTTP daemons are restricted to loopback until "
            "TLS support is available"
        )
    existing = _read_state(paths["state"], project_root)
    if existing is not None:
        if existing.get("lifecycle") == "launching":
            existing = _recover_launching_state(existing, paths, project_root)
        if existing is not None:
            pid = int(existing["pid"])
            if _pid_is_alive(pid) and _process_owns_instance(
                pid, str(existing["instance_id"])
            ):
                existing = _recover_starting_state(existing, paths)
                if existing.get("lifecycle", "running") == "running":
                    _write_port_profile(paths["port"], int(existing["port"]))
                return _state_result(existing, already_running=True)
            _remove_state(paths["state"])

    with _port_allocation_lock():
        selected_port = _select_start_port(
            project_root=project_root,
            paths=paths,
            host=host,
            requested_port=port,
        )
        return _launch_daemon_locked(
            project_root=project_root,
            paths=paths,
            host=host,
            port=selected_port,
            path=path,
        )


def _launch_daemon_locked(
    *,
    project_root: Path,
    paths: Mapping[str, Path],
    host: str,
    port: int,
    path: str,
) -> dict[str, Any]:
    probe_host = _probe_host(host)
    if _can_connect(probe_host, port):
        raise DaemonError(f"cannot start daemon: {host}:{port} is already in use")

    instance_id = str(uuid.uuid4())
    command = [
        sys.executable,
        "-m",
        "nomad.cli",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
        "--path",
        path,
        "--daemon-id",
        instance_id,
        "--daemon-state",
        str(paths["state"]),
    ]
    state = {
        "schema_version": SCHEMA_VERSION,
        "pid": 0,
        "project_root": str(project_root),
        "host": host,
        "port": port,
        "path": path,
        "url": _build_url(host, port, path),
        "version": __version__,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "instance_id": instance_id,
        "log_path": str(paths["log"]),
        "allow_remote": False,
        "auth": True,
        "token_env_var": _project_token_env_var(project_root),
        "lifecycle": "launching",
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        _write_state(paths["state"], state)
        env = os.environ.copy()
        env["NOMAD_MCP_LOG_PATH"] = str(paths["log"])
        bearer_token = _read_or_create_token(paths["token"])
        env[BEARER_TOKEN_ENV_VAR] = bearer_token

        log_handle = _open_secure_append(paths["log"])
        try:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        except OSError as exc:
            raise DaemonError(f"failed to launch Nomad daemon: {exc}") from exc
        finally:
            log_handle.close()

        state = _wait_for_child_claim(
            process,
            paths,
            project_root,
            instance_id,
        )
        _wait_until_ready(
            process,
            probe_host,
            port,
            path,
            bearer_token,
        )
        if not _process_owns_instance(process.pid, instance_id):
            raise DaemonError("started process failed daemon ownership verification")
        state = _read_state(paths["state"], project_root)
        if (
            state is None
            or state.get("instance_id") != instance_id
            or state.get("lifecycle") != "starting"
            or state.get("pid") != process.pid
        ):
            raise DaemonError("daemon lifecycle state changed during startup")
        state["lifecycle"] = "running"
        state["ready_at"] = datetime.now(timezone.utc).isoformat()
        _write_port_profile(paths["port"], port)
        _write_state(paths["state"], state)
    except BaseException:
        if process is not None:
            _terminate_failed_start(process)
        _remove_state(paths["state"])
        raise

    return _state_result(state, already_running=False)


def _stop_locked(
    *,
    project_root: Path,
    paths: Mapping[str, Path],
    timeout: float,
) -> dict[str, Any]:
    state = _read_state(paths["state"], project_root)
    if state is None:
        return _stopped_result(project_root, already_stopped=True)

    if state.get("lifecycle") == "launching":
        state = _recover_launching_state(state, paths, project_root)
        if state is None:
            return _stopped_result(
                project_root,
                already_stopped=True,
                stale_state_removed=True,
            )

    pid = int(state["pid"])
    if not _pid_is_alive(pid):
        _remove_state(paths["state"])
        return _stopped_result(
            project_root,
            already_stopped=True,
            stale_state_removed=True,
        )

    instance_id = str(state["instance_id"])
    if not _process_owns_instance(pid, instance_id):
        if not _pid_is_alive(pid):
            _remove_state(paths["state"])
            return _stopped_result(
                project_root,
                already_stopped=True,
                stale_state_removed=True,
            )
        raise DaemonOwnershipError(
            f"refusing to stop pid {pid}: it is not daemon instance {instance_id}"
        )

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_state(paths["state"])
        return _stopped_result(project_root, already_stopped=True)
    except PermissionError as exc:
        raise DaemonError(f"permission denied stopping daemon pid {pid}") from exc

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            _remove_state(paths["state"])
            return _stopped_result(project_root, pid=pid)
        time.sleep(POLL_INTERVAL_SECONDS)

    if not _pid_is_alive(pid):
        _remove_state(paths["state"])
        return _stopped_result(project_root, pid=pid)
    raise DaemonError(
        f"daemon pid {pid} did not exit after SIGTERM within {timeout:g} seconds"
    )


def _project_paths(project_root: Path) -> dict[str, Path]:
    daemon_dir = _ensure_daemon_dir()
    project_hash = _project_hash(project_root)
    return {
        "state": daemon_dir / f"{project_hash}.json",
        "lock": daemon_dir / f"{project_hash}.lock",
        "claim_lock": daemon_dir / f"{project_hash}.claim.lock",
        "log": daemon_dir / f"{project_hash}.log",
        "token": daemon_dir / f"{project_hash}.token",
        "port": daemon_dir / f"{project_hash}.port",
    }


def _project_hash(project_root: Path) -> str:
    return hashlib.sha256(os.fsencode(str(project_root))).hexdigest()


def _project_token_env_var(project_root: Path) -> str:
    return f"{BEARER_TOKEN_ENV_VAR}_{_project_hash(project_root)[:16].upper()}"


def _ensure_daemon_dir() -> Path:
    DEFAULT_DAEMONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DEFAULT_DAEMONS_DIR, 0o700)
    return DEFAULT_DAEMONS_DIR


@contextmanager
def _port_allocation_lock() -> Iterator[None]:
    with _project_lock(_ensure_daemon_dir() / ".ports.lock"):
        yield


@contextmanager
def _project_lock(lock_path: Path) -> Iterator[None]:
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    os.chmod(lock_path, 0o600)
    with os.fdopen(fd, "a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _open_secure_append(path: Path):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


def _read_or_create_token(path: Path) -> str:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_token(path)

    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(token)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return token


def _read_token(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DaemonError("cannot read daemon authentication token") from exc
    try:
        with os.fdopen(fd, "r", encoding="ascii") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise DaemonError(
                    "daemon authentication token is not a regular file"
                )
            os.fchmod(handle.fileno(), 0o600)
            token = handle.read().strip()
    except (OSError, UnicodeError) as exc:
        raise DaemonError("cannot read daemon authentication token") from exc
    if not token or any(character.isspace() for character in token):
        raise DaemonError("daemon authentication token file is invalid")
    return token


def _select_start_port(
    *,
    project_root: Path,
    paths: Mapping[str, Path],
    host: str,
    requested_port: int | None,
) -> int:
    probe_host = _probe_host(host)
    reserved_ports = _reserved_profile_ports(paths["port"])

    if requested_port is not None:
        if requested_port in reserved_ports:
            raise DaemonError(
                f"cannot use explicit port {requested_port}: it is reserved by "
                "another Nomad project"
            )
        if _can_connect(probe_host, requested_port):
            raise DaemonError(
                f"cannot start daemon: {host}:{requested_port} is already in use"
            )
        return requested_port

    persisted_port = _read_port_profile(paths["port"])
    if persisted_port is not None:
        if _can_connect(probe_host, persisted_port):
            raise DaemonError(
                f"persisted daemon port {persisted_port} is already in use; "
                "choose a free port with --port"
            )
        return persisted_port

    first_port = project_default_port(project_root)
    port_count = PROJECT_PORT_MAX - PROJECT_PORT_MIN + 1
    first_offset = first_port - PROJECT_PORT_MIN
    for offset in range(port_count):
        candidate = PROJECT_PORT_MIN + (first_offset + offset) % port_count
        if candidate in reserved_ports:
            continue
        if not _can_connect(probe_host, candidate):
            return candidate
    raise DaemonError("cannot start daemon: no free project daemon port is available")


def _reserved_profile_ports(current_profile: Path) -> set[int]:
    reserved: set[int] = set()
    for candidate in current_profile.parent.glob("*.port"):
        if candidate == current_profile:
            continue
        try:
            port = _read_port_profile(candidate)
        except DaemonError:
            continue
        if port is not None:
            reserved.add(port)
    return reserved


def _read_port_profile(path: Path) -> int | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DaemonError("cannot read daemon port profile") from exc

    try:
        with os.fdopen(fd, "r", encoding="ascii") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise DaemonError("daemon port profile is not a regular file")
            os.fchmod(handle.fileno(), 0o600)
            value = handle.read().strip()
    except DaemonError:
        raise
    except (OSError, UnicodeError) as exc:
        raise DaemonError("cannot read daemon port profile") from exc

    try:
        port = int(value)
    except ValueError as exc:
        raise DaemonError("daemon port profile is invalid") from exc
    if not value.isascii() or not value.isdigit() or not 1 <= port <= 65535:
        raise DaemonError("daemon port profile is invalid")
    return port


def _write_port_profile(path: Path, port: int) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise DaemonError("cannot persist invalid daemon port")

    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(f"{port}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _read_state(path: Path, project_root: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        _remove_state(path)
        return None

    if not _valid_state(payload, project_root):
        _remove_state(path)
        return None
    return payload


def _read_claimable_state(path: Path, project_root: Path) -> dict[str, Any]:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DaemonError("cannot read daemon lifecycle state") from exc

    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode):
                raise DaemonError("daemon lifecycle state is not a regular file")
            payload = json.load(handle)
    except DaemonError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DaemonError("cannot read daemon lifecycle state") from exc

    if not _valid_state(payload, project_root):
        raise DaemonError("daemon lifecycle state is invalid")
    return payload


def _valid_state(payload: Any, project_root: Path) -> bool:
    if not isinstance(payload, dict):
        return False
    required_types = {
        "schema_version": int,
        "pid": int,
        "project_root": str,
        "host": str,
        "port": int,
        "path": str,
        "url": str,
        "version": str,
        "started_at": str,
        "instance_id": str,
        "log_path": str,
    }
    if payload.get("schema_version") != SCHEMA_VERSION:
        return False
    if payload.get("project_root") != str(project_root):
        return False
    if not all(
        isinstance(payload.get(key), expected_type)
        and not (
            expected_type is int and isinstance(payload.get(key), bool)
        )
        for key, expected_type in required_types.items()
    ):
        return False
    lifecycle = payload.get("lifecycle", "running")
    if lifecycle not in {"launching", "starting", "running"}:
        return False
    if lifecycle == "launching":
        if payload["pid"] != 0:
            return False
    elif payload["pid"] <= 0:
        return False
    if not 1 <= payload["port"] <= 65535:
        return False
    if not payload["path"].strip():
        return False
    if not payload["instance_id"].strip():
        return False
    for optional_key, expected_type in {
        "allow_remote": bool,
        "auth": bool,
        "token_env_var": str,
        "ready_at": str,
    }.items():
        if optional_key in payload and not isinstance(
            payload[optional_key], expected_type
        ):
            return False
    return True


def _remove_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _recover_starting_state(
    state: Mapping[str, Any],
    paths: Mapping[str, Path],
) -> dict[str, Any]:
    recovered = dict(state)
    if recovered.get("lifecycle", "running") != "starting":
        return recovered

    try:
        bearer_token = _read_token(paths["token"])
        health_data = _mcp_health_data(
            str(recovered["url"]),
            bearer_token,
            timeout=STARTING_RECOVERY_TIMEOUT_SECONDS,
        )
    except Exception:
        return recovered

    if health_data.get("pid") != int(recovered["pid"]):
        return recovered

    recovered["lifecycle"] = "running"
    recovered["ready_at"] = datetime.now(timezone.utc).isoformat()
    _write_port_profile(paths["port"], int(recovered["port"]))
    _write_state(paths["state"], recovered)
    return recovered


def _recover_launching_state(
    state: Mapping[str, Any],
    paths: Mapping[str, Path],
    project_root: Path,
    *,
    timeout: float | None = None,
) -> dict[str, Any] | None:
    current = dict(state)
    if current.get("lifecycle") != "launching":
        return current

    if timeout is None:
        timeout = LAUNCH_CLAIM_TIMEOUT_SECONDS
    instance_id = str(current["instance_id"])
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        current = _read_state(paths["state"], project_root)
        if current is None:
            return None
        if current.get("instance_id") != instance_id:
            return current
        if current.get("lifecycle") != "launching":
            return current
        time.sleep(POLL_INTERVAL_SECONDS)

    with _project_lock(paths["claim_lock"]):
        current = _read_state(paths["state"], project_root)
        if current is None:
            return None
        if (
            current.get("instance_id") == instance_id
            and current.get("lifecycle") == "launching"
            and current.get("pid") == 0
        ):
            _remove_state(paths["state"])
            return None
        return current


def _wait_for_child_claim(
    process: subprocess.Popen[bytes],
    paths: Mapping[str, Path],
    project_root: Path,
    instance_id: str,
    timeout: float | None = None,
) -> dict[str, Any]:
    if timeout is None:
        timeout = START_TIMEOUT_SECONDS
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise DaemonError(
                f"Nomad daemon exited before claiming lifecycle state with status {returncode}"
            )
        state = _read_state(paths["state"], project_root)
        if state is None:
            raise DaemonError("daemon lifecycle state disappeared before child claim")
        if state.get("instance_id") != instance_id:
            raise DaemonError("daemon lifecycle state instance changed before child claim")
        if (
            state.get("lifecycle") == "starting"
            and state.get("pid") == process.pid
        ):
            return state
        if state.get("lifecycle") != "launching" or state.get("pid") != 0:
            raise DaemonError("daemon child wrote an invalid lifecycle claim")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise DaemonError(
        f"Nomad daemon did not claim lifecycle state within {timeout:g} seconds"
    )


def _process_owns_instance(pid: int, instance_id: str) -> bool:
    command = _process_command(pid)
    if not command:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if "serve" not in parts or "nomad.cli" not in parts:
        return False
    for index, part in enumerate(parts):
        if part == "--daemon-id" and index + 1 < len(parts):
            return parts[index + 1] == instance_id
        if part == f"--daemon-id={instance_id}":
            return True
    return False


def _process_command(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _pid_is_alive(pid: int) -> bool:
    if isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    state = _process_state(pid)
    if state == "":
        return False
    return state is None or not state.startswith("Z")


def _process_state(pid: int) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def _wait_until_ready(
    process: subprocess.Popen[bytes],
    host: str,
    port: int,
    path: str,
    bearer_token: str,
    timeout: float = START_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    last_health_error: str | None = None
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise DaemonError(
                f"Nomad daemon exited during startup with status {returncode}"
            )
        if _can_connect(host, port):
            remaining = max(0.1, deadline - time.monotonic())
            try:
                health_data = _mcp_health_data(
                    _build_url(host, port, path),
                    bearer_token,
                    timeout=min(1.0, remaining),
                )
            except Exception as exc:
                last_health_error = type(exc).__name__
            else:
                health_pid = health_data.get("pid")
                if health_pid == process.pid:
                    return
                last_health_error = "health_pid_mismatch"
        time.sleep(POLL_INTERVAL_SECONDS)
    detail = (
        f"; last health error: {last_health_error}"
        if last_health_error is not None
        else ""
    )
    raise DaemonError(
        f"Nomad daemon did not pass authenticated MCP health at {host}:{port} "
        f"within {timeout:g} seconds{detail}"
    )


def _mcp_health_data(
    url: str,
    bearer_token: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    async def call_health() -> dict[str, Any]:
        with anyio.fail_after(timeout):
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {bearer_token}"},
                timeout=httpx.Timeout(timeout),
            ) as http_client:
                async with streamable_http_client(
                    url,
                    http_client=http_client,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool("health")

        text_content = next(
            (
                content.text
                for content in result.content
                if hasattr(content, "text")
            ),
            None,
        )
        if text_content is None:
            raise DaemonError("MCP health returned no text content")
        try:
            payload = json.loads(text_content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DaemonError("MCP health returned invalid JSON") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or payload.get("tool") != "health"
        ):
            raise DaemonError("MCP health did not report success")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise DaemonError("MCP health returned invalid data")
        pid = data.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise DaemonError("MCP health returned an invalid pid")
        return data

    return anyio.run(call_health)


def _terminate_failed_start(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        pass
    else:
        try:
            process.wait(timeout=2)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass

    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _can_connect(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _probe_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host in {"::", "[::]"}:
        return "::1"
    return host


def _build_url(host: str, port: int, path: str) -> str:
    rendered_host = host
    try:
        if ip_address(host).version == 6:
            rendered_host = f"[{host}]"
    except ValueError:
        pass
    return f"http://{rendered_host}:{port}{path}"


_PUBLIC_STATE_FIELDS = (
    "schema_version",
    "pid",
    "project_root",
    "host",
    "port",
    "path",
    "url",
    "version",
    "started_at",
    "ready_at",
    "instance_id",
    "log_path",
    "allow_remote",
    "auth",
    "token_env_var",
    "lifecycle",
)


def _state_result(
    state: Mapping[str, Any], *, already_running: bool
) -> dict[str, Any]:
    lifecycle = str(state.get("lifecycle", "running"))
    public_state = {
        key: state[key] for key in _PUBLIC_STATE_FIELDS if key in state
    }
    public_state["lifecycle"] = lifecycle
    return {
        "status": lifecycle,
        "running": lifecycle == "running",
        "already_running": already_running,
        **public_state,
    }


def _stopped_result(project_root: Path, **details: Any) -> dict[str, Any]:
    return {
        "status": "stopped",
        "running": False,
        "project_root": str(project_root),
        **details,
    }
