"""TOML snapshots and safe project-scoped Codex configuration writes."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit


STDIO_FIELDS = frozenset({"command", "args", "env", "env_vars", "cwd"})

class CodexConfigError(RuntimeError):
    """Raised when Codex configuration cannot be changed safely."""

    def __init__(self, error_type: str, details: Mapping[str, Any]) -> None:
        self.error_type = error_type
        self.details = json.loads(json.dumps(dict(details), sort_keys=True))
        message = self.details.get("message")
        super().__init__(message if isinstance(message, str) else error_type)


@dataclass(frozen=True)
class _ConfigSnapshot:
    path: Path
    exists: bool
    digest: str | None
    mode: int | None
    document: Any
    resolved_path: Path | None = None
    file_identity: tuple[int, int] | None = None
    project_root: Path | None = None
    root_identity: tuple[int, int] | None = None
    directory_identity: tuple[int, int] | None = None


def _global_config_path() -> Path:
    configured_home = os.environ.get("CODEX_HOME")
    base = (
        Path(configured_home).expanduser()
        if configured_home
        else Path.home() / ".codex"
    )
    return Path(os.path.abspath(os.fspath(base / "config.toml")))


def _read_project_config(project_root: Path) -> _ConfigSnapshot:
    path = project_root / ".codex" / "config.toml"
    root_fd, root_identity = _open_project_root(project_root)
    directory_fd = -1
    try:
        try:
            directory_fd = _open_directory_at(root_fd, ".codex")
        except FileNotFoundError:
            return _empty_project_snapshot(
                path,
                project_root=project_root,
                root_identity=root_identity,
                directory_identity=None,
            )
        except OSError:
            raise _error(
                "unsafe_config_directory",
                path=str(path.parent),
                message=(
                    "Project Codex directory must be a real, non-symlink directory."
                ),
            ) from None
        directory_identity = _identity(os.fstat(directory_fd))
        try:
            fd = _open_regular_at(directory_fd, "config.toml")
        except FileNotFoundError:
            return _empty_project_snapshot(
                path,
                project_root=project_root,
                root_identity=root_identity,
                directory_identity=directory_identity,
            )
        except OSError:
            raise _error(
                "unsafe_config_path",
                path=str(path),
                message="Project configuration must be a regular, non-symlink file.",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            raw = handle.read()
        return _parsed_snapshot(
            path=path,
            raw=raw,
            mode=stat.S_IMODE(metadata.st_mode),
            resolved_path=path,
            file_identity=_identity(metadata),
            project_root=project_root,
            root_identity=root_identity,
            directory_identity=directory_identity,
        )
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(root_fd)


def _empty_project_snapshot(
    path: Path,
    *,
    project_root: Path,
    root_identity: tuple[int, int],
    directory_identity: tuple[int, int] | None,
) -> _ConfigSnapshot:
    return _ConfigSnapshot(
        path=path,
        exists=False,
        digest=None,
        mode=None,
        document=tomlkit.document(),
        project_root=project_root,
        root_identity=root_identity,
        directory_identity=directory_identity,
    )


def _read_global_config(path: Path) -> _ConfigSnapshot:
    try:
        path.lstat()
    except FileNotFoundError:
        return _empty_global_snapshot(path)
    except OSError:
        raise _unsafe_global_path(path)
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        raise _unsafe_global_path(path) from None
    if not stat.S_ISREG(metadata.st_mode):
        raise _unsafe_global_path(path)

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(resolved, flags)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise OSError(errno.EINVAL, "global config is not regular")
            raw = handle.read()
    except OSError:
        raise _unsafe_global_path(path) from None
    return _parsed_snapshot(
        path=path,
        raw=raw,
        mode=stat.S_IMODE(opened.st_mode),
        resolved_path=resolved,
        file_identity=_identity(opened),
    )


def _empty_global_snapshot(path: Path) -> _ConfigSnapshot:
    return _ConfigSnapshot(
        path=path,
        exists=False,
        digest=None,
        mode=None,
        document=tomlkit.document(),
    )


def _unsafe_global_path(path: Path) -> CodexConfigError:
    return _error(
        "unsafe_global_config_path",
        path=str(path),
        message="User-level Codex configuration must resolve to a regular file.",
    )


def _parsed_snapshot(
    *,
    path: Path,
    raw: bytes,
    mode: int,
    resolved_path: Path,
    file_identity: tuple[int, int],
    project_root: Path | None = None,
    root_identity: tuple[int, int] | None = None,
    directory_identity: tuple[int, int] | None = None,
) -> _ConfigSnapshot:
    try:
        document = tomlkit.parse(raw.decode("utf-8"))
    except Exception:
        raise _error(
            "malformed_toml",
            path=str(path),
            message="Configuration contains malformed TOML and was not overwritten.",
        ) from None
    return _ConfigSnapshot(
        path=path,
        exists=True,
        digest=hashlib.sha256(raw).hexdigest(),
        mode=mode,
        document=document,
        resolved_path=resolved_path,
        file_identity=file_identity,
        project_root=project_root,
        root_identity=root_identity,
        directory_identity=directory_identity,
    )


def _open_project_root(
    project_root: Path,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(project_root, flags)
        metadata = os.fstat(fd)
    except OSError:
        raise _error(
            "unsafe_project_path",
            path=str(project_root),
            message="Canonical project root changed during configuration.",
        ) from None
    identity = _identity(metadata)
    if not stat.S_ISDIR(metadata.st_mode) or (
        expected_identity is not None and identity != expected_identity
    ):
        os.close(fd)
        raise _error(
            "unsafe_project_path",
            path=str(project_root),
            message="Canonical project root changed during configuration.",
        )
    return fd, identity


def _open_directory_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=parent_fd)


def _open_regular_at(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(name, flags, dir_fd=parent_fd)
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "not a regular file")
    return fd


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _diagnostic_read_project(
    project_root: Path,
) -> tuple[_ConfigSnapshot | None, CodexConfigError | None]:
    try:
        return _read_project_config(project_root), None
    except CodexConfigError as exc:
        return None, exc


def _diagnostic_read_global(
    path: Path,
) -> tuple[_ConfigSnapshot | None, CodexConfigError | None]:
    try:
        return _read_global_config(path), None
    except CodexConfigError as exc:
        return None, exc


def _servers(
    document: Any,
    *,
    path: Path,
) -> MutableMapping[str, Any] | None:
    servers = document.get("mcp_servers")
    if servers is None:
        return None
    if not isinstance(servers, MutableMapping):
        raise _error(
            "invalid_config",
            path=str(path),
            message="mcp_servers must be a TOML table.",
        )
    return servers


def _server_entry(
    document: Any,
    *,
    path: Path,
    name: str,
) -> MutableMapping[str, Any] | None:
    servers = _servers(document, path=path)
    if servers is None or name not in servers:
        return None
    entry = servers[name]
    if not isinstance(entry, MutableMapping):
        raise _error(
            "invalid_config",
            path=str(path),
            name=name,
            message="Named MCP server configuration must be a TOML table.",
        )
    return entry


def _analyze_global(
    snapshot: _ConfigSnapshot,
    *,
    project_root: Path,
    name: str,
    url: str | None,
    token_env_var: str,
) -> dict[str, Any]:
    servers = _servers(snapshot.document, path=snapshot.path)
    same_name = bool(servers is not None and name in servers)
    owned: list[str] = []
    stale: list[str] = []
    if servers is not None:
        for server_name, value in servers.items():
            if not isinstance(value, MutableMapping):
                continue
            if value.get("bearer_token_env_var") != token_env_var:
                continue
            if url is not None and value.get("url") == url:
                owned.append(str(server_name))
            else:
                stale.append(str(server_name))
    owned.sort()
    stale.sort()
    trust_status = _global_trust_status(
        snapshot.document,
        path=snapshot.path,
        project_root=project_root,
    )
    stale_global = bool(owned or stale)
    message = _global_message(
        snapshot.path,
        conflict=same_name,
        stale_global=stale_global,
        trust_status=trust_status,
    )
    return {
        "path": str(snapshot.path),
        "exists": snapshot.exists,
        "read_only": True,
        "conflict": same_name,
        "stale_global": stale_global,
        "same_name": same_name,
        "owned": owned,
        "stale": stale,
        "trust_status": trust_status,
        "message": message,
    }


def _global_message(
    path: Path,
    *,
    conflict: bool,
    stale_global: bool,
    trust_status: str,
) -> str | None:
    if conflict or stale_global:
        return (
            f"Remove or rename conflicting entries in {path}; "
            "user-level Codex configuration is read-only to Nomad."
        )
    if trust_status == "untrusted":
        return (
            f"Project is explicitly untrusted in {path}; "
            "change trust manually before configuring Nomad."
        )
    if trust_status == "unknown":
        return (
            "Project trust is not explicitly trusted; review it in the "
            "user-level Codex configuration."
        )
    return None


def _global_trust_status(
    document: Any,
    *,
    path: Path,
    project_root: Path,
) -> str:
    projects = document.get("projects")
    if projects is None:
        return "unknown"
    if not isinstance(projects, MutableMapping):
        raise _error(
            "invalid_config",
            path=str(path),
            message="projects must be a TOML table.",
        )
    project_config = projects.get(str(project_root))
    if project_config is None:
        return "unknown"
    if not isinstance(project_config, MutableMapping):
        raise _error(
            "invalid_config",
            path=str(path),
            message="Canonical project trust configuration must be a TOML table.",
        )
    trust_level = project_config.get("trust_level")
    if trust_level == "trusted":
        return "trusted"
    if trust_level == "untrusted":
        return "untrusted"
    return "unknown"


def _raise_for_global_blocker(report: Mapping[str, Any]) -> None:
    if report.get("trust_status") == "untrusted":
        raise _error(
            "project_explicitly_untrusted",
            path=str(report["path"]),
            trust_status="untrusted",
            message=str(report["message"]),
        )
    if not report.get("conflict") and not report.get("stale_global"):
        return
    raise _error(
        "global_config_conflict",
        path=str(report["path"]),
        conflict=bool(report.get("conflict")),
        stale_global=bool(report.get("stale_global")),
        owned=list(report.get("owned", [])),
        stale=list(report.get("stale", [])),
        message=str(report["message"]),
    )


def _raise_for_postcommit_global_blocker(
    report: Mapping[str, Any],
) -> None:
    if (
        not report.get("conflict")
        and not report.get("stale_global")
        and report.get("trust_status") != "untrusted"
    ):
        return
    raise _error(
        "global_conflict_after_project_commit",
        path=str(report["path"]),
        config_committed=True,
        token_env_configured=False,
        conflict=bool(report.get("conflict")),
        stale_global=bool(report.get("stale_global")),
        trust_status=str(report.get("trust_status")),
        message=(
            "Project configuration was committed, but user-level Codex "
            "configuration changed to a blocking state; resolve it manually."
        ),
    )


def _postcommit_global_report(
    *,
    global_path: Path,
    project_root: Path,
    name: str,
    url: str,
    token_env_var: str,
) -> dict[str, Any]:
    try:
        snapshot = _read_global_config(global_path)
        return _analyze_global(
            snapshot,
            project_root=project_root,
            name=name,
            url=url,
            token_env_var=token_env_var,
        )
    except CodexConfigError:
        raise _error(
            "global_config_invalid_after_project_commit",
            path=str(global_path),
            config_committed=True,
            token_env_configured=False,
            message=(
                "Project configuration was committed, but user-level Codex "
                "configuration became unreadable or invalid; resolve it manually."
            ),
        ) from None


def _update_project_config(
    snapshot: _ConfigSnapshot,
    *,
    name: str,
    url: str,
    token_env_var: str,
) -> bool:
    document = snapshot.document
    servers = _servers(document, path=snapshot.path)
    if servers is None:
        servers = tomlkit.table()
        document["mcp_servers"] = servers
    entry = _server_entry(document, path=snapshot.path, name=name)
    if entry is None:
        entry = tomlkit.table()
        servers[name] = entry

    changed = False
    for field in STDIO_FIELDS:
        if field in entry:
            del entry[field]
            changed = True
    if entry.get("url") != url:
        entry["url"] = url
        changed = True
    if entry.get("bearer_token_env_var") != token_env_var:
        entry["bearer_token_env_var"] = token_env_var
        changed = True
    if not changed:
        _assert_project_snapshot_current(snapshot)
        return False
    _atomic_project_write(
        snapshot,
        tomlkit.dumps(document).encode("utf-8"),
    )
    return True


def _assert_project_snapshot_current(snapshot: _ConfigSnapshot) -> None:
    if snapshot.project_root is None:
        raise AssertionError("project snapshot is required")
    current = _read_project_config(snapshot.project_root)
    if (
        current.exists != snapshot.exists
        or current.digest != snapshot.digest
        or current.file_identity != snapshot.file_identity
        or current.root_identity != snapshot.root_identity
        or current.directory_identity != snapshot.directory_identity
    ):
        raise _concurrent_project_error(snapshot.path)


def _assert_global_snapshot_current(snapshot: _ConfigSnapshot) -> None:
    try:
        current = _read_global_config(snapshot.path)
    except CodexConfigError:
        raise _concurrent_global_error(snapshot.path) from None
    if (
        current.exists != snapshot.exists
        or current.digest != snapshot.digest
        or current.resolved_path != snapshot.resolved_path
        or current.file_identity != snapshot.file_identity
    ):
        raise _concurrent_global_error(snapshot.path)


def _concurrent_global_error(path: Path) -> CodexConfigError:
    return _error(
        "concurrent_global_config_change",
        path=str(path),
        message="User-level Codex configuration changed before project commit.",
    )


def _atomic_project_write(
    snapshot: _ConfigSnapshot,
    content: bytes,
) -> None:
    from .codex_atomic import atomic_project_write

    atomic_project_write(snapshot, content)


def _concurrent_project_error(
    path: Path,
    *,
    proposed_config_path: str | None = None,
    concurrent_config_paths: list[str] | None = None,
) -> CodexConfigError:
    details: dict[str, Any] = {
        "path": str(path),
        "config_committed": False,
        "message": (
            "Configuration changed after it was read; the concurrent version "
            "remains active."
        ),
    }
    if proposed_config_path is not None:
        details["preserved_config_path"] = proposed_config_path
    if concurrent_config_paths:
        details["preserved_concurrent_config_paths"] = concurrent_config_paths
    return _error(
        "concurrent_config_change",
        **details,
    )


def _project_diagnostic(
    snapshot: _ConfigSnapshot | None,
    error: CodexConfigError | None,
    *,
    name: str,
    url: str | None,
    token_env_var: str,
) -> dict[str, Any]:
    if error is not None or snapshot is None:
        return {
            "exists": snapshot.exists if snapshot is not None else True,
            "status": "error",
            "match": False,
            "error_type": error.error_type if error is not None else "invalid_config",
        }
    if not snapshot.exists:
        return {"exists": False, "status": "missing", "match": False}
    try:
        entry = _server_entry(snapshot.document, path=snapshot.path, name=name)
    except CodexConfigError as exc:
        return {
            "exists": True,
            "status": "error",
            "match": False,
            "error_type": exc.error_type,
        }
    match = bool(
        entry is not None
        and url is not None
        and entry.get("url") == url
        and entry.get("bearer_token_env_var") == token_env_var
        and not any(field in entry for field in STDIO_FIELDS)
    )
    return {
        "exists": True,
        "status": "match" if match else "mismatch",
        "match": match,
    }


def _global_diagnostic(
    snapshot: _ConfigSnapshot | None,
    error: CodexConfigError | None,
    *,
    project_root: Path,
    name: str,
    url: str | None,
    token_env_var: str,
) -> dict[str, Any]:
    if error is not None or snapshot is None:
        return _global_diagnostic_error(snapshot, error)
    try:
        report = _analyze_global(
            snapshot,
            project_root=project_root,
            name=name,
            url=url,
            token_env_var=token_env_var,
        )
    except CodexConfigError as exc:
        return _global_diagnostic_error(snapshot, exc)
    if report["conflict"]:
        report["status"] = "conflict"
    elif report["stale_global"]:
        report["status"] = "stale_global"
    elif report["trust_status"] == "untrusted":
        report["status"] = "untrusted"
    else:
        report["status"] = "clear"
    return report


def _global_diagnostic_error(
    snapshot: _ConfigSnapshot | None,
    error: CodexConfigError | None,
) -> dict[str, Any]:
    return {
        "path": (
            str(snapshot.path)
            if snapshot is not None
            else str(_global_config_path())
        ),
        "exists": snapshot.exists if snapshot is not None else True,
        "read_only": True,
        "status": "error",
        "error_type": error.error_type if error is not None else "invalid_config",
        "conflict": True,
        "stale_global": False,
        "owned": [],
        "stale": [],
        "trust_status": "unknown",
        "message": "Fix the user-level Codex configuration before setup or repair.",
    }


def _error(error_type: str, **details: Any) -> CodexConfigError:
    return CodexConfigError(error_type, details)
