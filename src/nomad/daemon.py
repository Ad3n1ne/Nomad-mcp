"""Project-scoped lifecycle management for persistent Nomad MCP servers."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import signal
import socket
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

from nomad import __version__


SCHEMA_VERSION = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
DEFAULT_DAEMONS_DIR = Path.home() / ".nomad" / "daemons"
START_TIMEOUT_SECONDS = 10.0
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
    port: int = DEFAULT_PORT,
    path: str = DEFAULT_PATH,
    allow_remote: bool = False,
) -> dict[str, Any]:
    """Starts one persistent Nomad daemon for a project, idempotently."""
    project_root = resolve_project(project)
    _validate_endpoint(host, port, path)
    host = host.strip()
    if not is_loopback_host(host):
        if not allow_remote:
            raise DaemonError(
                f"refusing non-loopback host {host!r}; pass --allow-remote explicitly"
            )
        print(
            f"warning: Nomad daemon will listen on non-loopback host {host!r}",
            file=sys.stderr,
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

        return _running_result(state, already_running=True)


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
        endpoint = {
            "host": state.get("host", DEFAULT_HOST) if state else DEFAULT_HOST,
            "port": state.get("port", DEFAULT_PORT) if state else DEFAULT_PORT,
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


def resolve_project(project: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path.cwd() if project is None else Path(project).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DaemonError(f"project directory does not exist: {candidate}") from exc
    if not resolved.is_dir():
        raise DaemonError(f"project path is not a directory: {resolved}")
    return resolved


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
    port: int,
    path: str,
) -> dict[str, Any]:
    existing = _read_state(paths["state"], project_root)
    if existing is not None:
        pid = int(existing["pid"])
        if _pid_is_alive(pid) and _process_owns_instance(
            pid, str(existing["instance_id"])
        ):
            return _running_result(existing, already_running=True)
        _remove_state(paths["state"])

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
    ]
    env = os.environ.copy()
    env["NOMAD_MCP_LOG_PATH"] = str(paths["log"])

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

    try:
        _wait_until_ready(process, probe_host, port)
        if not _process_owns_instance(process.pid, instance_id):
            raise DaemonError("started process failed daemon ownership verification")
    except DaemonError:
        _terminate_failed_start(process)
        _remove_state(paths["state"])
        raise

    state = {
        "schema_version": SCHEMA_VERSION,
        "pid": process.pid,
        "project_root": str(project_root),
        "host": host,
        "port": port,
        "path": path,
        "url": _build_url(host, port, path),
        "version": __version__,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "instance_id": instance_id,
        "log_path": str(paths["log"]),
    }
    _write_state(paths["state"], state)
    return _running_result(state, already_running=False)


def _stop_locked(
    *,
    project_root: Path,
    paths: Mapping[str, Path],
    timeout: float,
) -> dict[str, Any]:
    state = _read_state(paths["state"], project_root)
    if state is None:
        return _stopped_result(project_root, already_stopped=True)

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
    project_hash = hashlib.sha256(os.fsencode(str(project_root))).hexdigest()
    return {
        "state": daemon_dir / f"{project_hash}.json",
        "lock": daemon_dir / f"{project_hash}.lock",
        "log": daemon_dir / f"{project_hash}.log",
    }


def _ensure_daemon_dir() -> Path:
    DEFAULT_DAEMONS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DEFAULT_DAEMONS_DIR, 0o700)
    return DEFAULT_DAEMONS_DIR


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
    if payload["pid"] <= 0:
        return False
    if not 1 <= payload["port"] <= 65535:
        return False
    if not payload["path"].strip():
        return False
    if not payload["instance_id"].strip():
        return False
    return True


def _remove_state(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


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
    timeout: float = START_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout
    consecutive_connections = 0
    while time.monotonic() < deadline:
        returncode = process.poll()
        if returncode is not None:
            raise DaemonError(
                f"Nomad daemon exited during startup with status {returncode}"
            )
        if _can_connect(host, port):
            consecutive_connections += 1
            if consecutive_connections >= 2:
                return
        else:
            consecutive_connections = 0
        time.sleep(POLL_INTERVAL_SECONDS)
    raise DaemonError(
        f"Nomad daemon did not become reachable at {host}:{port} "
        f"within {timeout:g} seconds"
    )


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


def _running_result(
    state: Mapping[str, Any], *, already_running: bool
) -> dict[str, Any]:
    return {
        "status": "running",
        "running": True,
        "already_running": already_running,
        **dict(state),
    }


def _stopped_result(project_root: Path, **details: Any) -> dict[str, Any]:
    return {
        "status": "stopped",
        "running": False,
        "project_root": str(project_root),
        **details,
    }
