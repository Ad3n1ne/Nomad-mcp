"""Atomic project-scoped Codex configuration commits."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets

from pathlib import Path

from .codex_config import (
    CodexConfigError,
    _ConfigSnapshot,
    _concurrent_project_error,
    _error,
    _identity,
    _open_directory_at,
    _open_project_root,
    _open_regular_at,
)


_LINUX_RENAME_NOREPLACE = 1
_LINUX_RENAME_EXCHANGE = 2
_DARWIN_RENAME_SWAP = 0x00000002
_DARWIN_RENAME_EXCL = 0x00000004


def atomic_project_write(
    snapshot: _ConfigSnapshot,
    content: bytes,
) -> None:
    if snapshot.project_root is None or snapshot.root_identity is None:
        raise AssertionError("project snapshot is required")
    root_fd, _ = _open_project_root(
        snapshot.project_root,
        snapshot.root_identity,
    )
    directory_fd = -1
    temporary_name: str | None = None
    try:
        directory_fd, directory_identity = _open_config_directory_for_write(
            snapshot,
            root_fd,
        )
        temporary_name = _write_temporary_config(
            snapshot,
            directory_fd,
            content,
        )
        _assert_project_snapshot_current_fd(
            snapshot,
            root_fd=root_fd,
            directory_fd=directory_fd,
            directory_identity=directory_identity,
        )
        _replace_temporary_config(
            snapshot,
            directory_fd=directory_fd,
            temporary_name=temporary_name,
            proposed_content=content,
            proposed_digest=hashlib.sha256(content).hexdigest(),
        )
        temporary_name = None
    except CodexConfigError as exc:
        if exc.details.get("preserved_config_path"):
            temporary_name = None
        raise
    except OSError:
        raise _error(
            "config_write_failed",
            path=str(snapshot.path),
            config_committed=False,
            message="Project Codex configuration could not be replaced atomically.",
        ) from None
    finally:
        if temporary_name is not None and directory_fd >= 0:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except OSError:
                pass
        if directory_fd >= 0:
            os.close(directory_fd)
        os.close(root_fd)


def _open_config_directory_for_write(
    snapshot: _ConfigSnapshot,
    root_fd: int,
) -> tuple[int, tuple[int, int]]:
    if snapshot.directory_identity is None:
        try:
            os.mkdir(".codex", 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
        except FileExistsError:
            pass
    try:
        directory_fd = _open_directory_at(root_fd, ".codex")
    except OSError:
        raise _error(
            "unsafe_config_directory",
            path=str(snapshot.path.parent),
            message="Project Codex directory changed before configuration commit.",
        ) from None
    directory_identity = _identity(os.fstat(directory_fd))
    if (
        snapshot.directory_identity is not None
        and directory_identity != snapshot.directory_identity
    ):
        os.close(directory_fd)
        raise _error(
            "unsafe_config_directory",
            path=str(snapshot.path.parent),
            message="Project Codex directory changed before configuration commit.",
        )
    return directory_fd, directory_identity


def _write_temporary_config(
    snapshot: _ConfigSnapshot,
    directory_fd: int,
    content: bytes,
) -> str:
    write_name = f".config.toml.write.{os.getpid()}.{secrets.token_hex(8)}"
    temporary_name = f".config.toml.{os.getpid()}.{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    mode = snapshot.mode if snapshot.mode is not None else 0o600
    fd = os.open(write_name, flags, mode, dir_fd=directory_fd)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            write_name,
            temporary_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
    except BaseException:
        for name in (write_name, temporary_name):
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
        raise
    return temporary_name


def _replace_temporary_config(
    snapshot: _ConfigSnapshot,
    *,
    directory_fd: int,
    temporary_name: str,
    proposed_content: bytes,
    proposed_digest: str,
) -> None:
    proposed_name = _write_preserved_config(
        snapshot,
        directory_fd=directory_fd,
        content=proposed_content,
        label="nomad-proposed",
    )
    proposed_path = str(snapshot.path.parent / proposed_name)
    preserve_proposed = False
    try:
        if snapshot.exists:
            _atomic_exchange(directory_fd, temporary_name, "config.toml")
            displaced_digest = _file_digest_at(directory_fd, temporary_name)
            active_digest = _file_digest_at(directory_fd, "config.toml")
            if (
                displaced_digest == snapshot.digest
                and active_digest == proposed_digest
            ):
                _unlink_committed_temporary(
                    snapshot,
                    directory_fd=directory_fd,
                    temporary_name=temporary_name,
                )
            else:
                preserve_proposed = True
                _resolve_exchange_conflict(
                    snapshot,
                    directory_fd=directory_fd,
                    temporary_name=temporary_name,
                    displaced_digest=displaced_digest,
                    active_digest=active_digest,
                    proposed_digest=proposed_digest,
                    proposed_path=proposed_path,
                )
        else:
            try:
                _atomic_no_replace(directory_fd, temporary_name, "config.toml")
            except FileExistsError:
                preserve_proposed = True
                _unlink_if_digest(
                    directory_fd,
                    temporary_name,
                    proposed_digest,
                )
                _fsync_conflict_directory(directory_fd)
                raise _concurrent_project_error(
                    snapshot.path,
                    proposed_config_path=proposed_path,
                ) from None
            active_digest = _file_digest_at(directory_fd, "config.toml")
            if active_digest != proposed_digest:
                preserve_proposed = True
                _fsync_conflict_directory(directory_fd)
                raise _concurrent_project_error(
                    snapshot.path,
                    proposed_config_path=proposed_path,
                )
        try:
            os.unlink(proposed_name, dir_fd=directory_fd)
        except OSError:
            preserve_proposed = True
            raise _error(
                "config_cleanup_failed",
                path=str(snapshot.path),
                config_committed=True,
                preserved_config_path=proposed_path,
                message=(
                    "Project configuration was committed, but its proposed "
                    "configuration copy could not be removed."
                ),
            ) from None
        try:
            os.fsync(directory_fd)
        except OSError:
            raise _error(
                "config_durability_uncertain",
                path=str(snapshot.path),
                config_committed=True,
                message=(
                    "Project configuration was replaced, but directory durability "
                    "could not be confirmed."
                ),
            ) from None
    finally:
        if not preserve_proposed:
            try:
                os.unlink(proposed_name, dir_fd=directory_fd)
            except OSError:
                pass


def _resolve_exchange_conflict(
    snapshot: _ConfigSnapshot,
    *,
    directory_fd: int,
    temporary_name: str,
    displaced_digest: str | None,
    active_digest: str | None,
    proposed_digest: str,
    proposed_path: str,
) -> None:
    concurrent_paths: list[str] = []
    if displaced_digest is not None and displaced_digest != proposed_digest:
        try:
            concurrent_paths.append(
                _copy_config_at(
                    snapshot,
                    directory_fd=directory_fd,
                    source_name=temporary_name,
                    label="concurrent",
                )
            )
        except OSError:
            _recover_from_conflict_failure(
                snapshot,
                directory_fd=directory_fd,
                temporary_name=temporary_name,
                proposed_digest=proposed_digest,
                proposed_path=proposed_path,
                concurrent_paths=concurrent_paths,
                restore_if_safe=True,
            )
    if active_digest == proposed_digest and displaced_digest is not None:
        try:
            _atomic_exchange(directory_fd, temporary_name, "config.toml")
        except OSError:
            _recover_from_conflict_failure(
                snapshot,
                directory_fd=directory_fd,
                temporary_name=temporary_name,
                proposed_digest=proposed_digest,
                proposed_path=proposed_path,
                concurrent_paths=concurrent_paths,
                restore_if_safe=False,
            )

        restored_active_digest = _file_digest_at(directory_fd, "config.toml")
        exchanged_digest = _file_digest_at(directory_fd, temporary_name)
        if exchanged_digest is not None and exchanged_digest != proposed_digest:
            try:
                concurrent_paths.append(
                    _copy_config_at(
                        snapshot,
                        directory_fd=directory_fd,
                        source_name=temporary_name,
                        label="concurrent",
                    )
                )
            except OSError:
                _recover_from_conflict_failure(
                    snapshot,
                    directory_fd=directory_fd,
                    temporary_name=temporary_name,
                    proposed_digest=proposed_digest,
                    proposed_path=proposed_path,
                    concurrent_paths=concurrent_paths,
                    restore_if_safe=False,
                )
        _unlink_if_digest(directory_fd, temporary_name, exchanged_digest)
        if (
            restored_active_digest is None
            or restored_active_digest == proposed_digest
        ):
            _recover_from_conflict_failure(
                snapshot,
                directory_fd=directory_fd,
                temporary_name=temporary_name,
                proposed_digest=proposed_digest,
                proposed_path=proposed_path,
                concurrent_paths=concurrent_paths,
                restore_if_safe=False,
            )
    else:
        _unlink_if_digest(directory_fd, temporary_name, displaced_digest)

    _fsync_conflict_directory(directory_fd)
    raise _concurrent_project_error(
        snapshot.path,
        proposed_config_path=proposed_path,
        concurrent_config_paths=concurrent_paths,
    )


def _recover_from_conflict_failure(
    snapshot: _ConfigSnapshot,
    *,
    directory_fd: int,
    temporary_name: str,
    proposed_digest: str,
    proposed_path: str,
    concurrent_paths: list[str],
    restore_if_safe: bool,
) -> None:
    """Restore the displaced user config first, preserving files if uncertain."""
    temporary_path = str(snapshot.path.parent / temporary_name)
    active_digest = _file_digest_at(directory_fd, "config.toml")
    temporary_digest = _file_digest_at(directory_fd, temporary_name)

    if (
        restore_if_safe
        and active_digest == proposed_digest
        and temporary_digest is not None
        and temporary_digest != proposed_digest
    ):
        try:
            _atomic_exchange(directory_fd, temporary_name, "config.toml")
        except OSError:
            pass

    active_digest = _file_digest_at(directory_fd, "config.toml")
    temporary_digest = _file_digest_at(directory_fd, temporary_name)

    retained_paths = list(concurrent_paths)
    if active_digest is not None and active_digest != proposed_digest:
        _append_unique_path(retained_paths, str(snapshot.path))
    if (
        temporary_digest != proposed_digest
        and _path_exists_at(directory_fd, temporary_name)
    ):
        _append_unique_path(retained_paths, temporary_path)

    if active_digest != proposed_digest and temporary_digest == proposed_digest:
        _unlink_if_digest(directory_fd, temporary_name, proposed_digest)

    _fsync_conflict_directory(directory_fd)
    raise _conflict_recovery_error(
        snapshot.path,
        proposed_path=proposed_path,
        concurrent_paths=retained_paths,
        config_committed=active_digest == proposed_digest,
    ) from None


def _append_unique_path(paths: list[str], path: str) -> None:
    if path not in paths:
        paths.append(path)


def _path_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return False
    return True


def _write_preserved_config(
    snapshot: _ConfigSnapshot,
    *,
    directory_fd: int,
    content: bytes,
    label: str,
) -> str:
    for _ in range(16):
        name = f".config.toml.{label}.{os.getpid()}.{secrets.token_hex(8)}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
            raise
        return name
    raise OSError(errno.EEXIST, "Could not allocate a preserved config path")


def _copy_config_at(
    snapshot: _ConfigSnapshot,
    *,
    directory_fd: int,
    source_name: str,
    label: str,
) -> str:
    content = _read_regular_at(directory_fd, source_name)
    name = _write_preserved_config(
        snapshot,
        directory_fd=directory_fd,
        content=content,
        label=label,
    )
    return str(snapshot.path.parent / name)


def _read_regular_at(directory_fd: int, name: str) -> bytes:
    fd = _open_regular_at(directory_fd, name)
    with os.fdopen(fd, "rb") as handle:
        return handle.read()


def _file_digest_at(directory_fd: int, name: str) -> str | None:
    try:
        content = _read_regular_at(directory_fd, name)
    except OSError:
        return None
    return hashlib.sha256(content).hexdigest()


def _unlink_if_digest(
    directory_fd: int,
    name: str,
    expected_digest: str | None,
) -> None:
    if expected_digest is None:
        return
    if _file_digest_at(directory_fd, name) != expected_digest:
        return
    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        pass


def _unlink_committed_temporary(
    snapshot: _ConfigSnapshot,
    *,
    directory_fd: int,
    temporary_name: str,
) -> None:
    try:
        os.unlink(temporary_name, dir_fd=directory_fd)
    except OSError:
        raise _error(
            "config_cleanup_failed",
            path=str(snapshot.path),
            config_committed=True,
            preserved_config_path=str(snapshot.path.parent / temporary_name),
            message=(
                "Project configuration was committed, but the previous "
                "configuration copy could not be removed."
            ),
        ) from None


def _conflict_recovery_error(
    path: Path,
    *,
    proposed_path: str,
    concurrent_paths: list[str],
    config_committed: bool,
) -> CodexConfigError:
    return _error(
        "config_conflict_recovery_required",
        path=str(path),
        config_committed=config_committed,
        preserved_config_path=proposed_path,
        preserved_concurrent_config_paths=concurrent_paths,
        message=(
            "Configuration changed during the atomic commit and a concurrent "
            "version could not be confirmed as the active configuration."
        ),
    )


def _fsync_conflict_directory(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError:
        pass


def _atomic_exchange(directory_fd: int, first: str, second: str) -> None:
    _atomic_rename(directory_fd, first, second, exchange=True)


def _atomic_no_replace(directory_fd: int, source: str, destination: str) -> None:
    _atomic_rename(directory_fd, source, destination, exchange=False)


def _atomic_rename(
    directory_fd: int,
    source: str,
    destination: str,
    *,
    exchange: bool,
) -> None:
    system = os.uname().sysname
    if system == "Darwin":
        flags = _DARWIN_RENAME_SWAP if exchange else _DARWIN_RENAME_EXCL
        _darwin_renameatx_np(directory_fd, source, destination, flags)
        return
    if system == "Linux":
        flags = _LINUX_RENAME_EXCHANGE if exchange else _LINUX_RENAME_NOREPLACE
        _linux_renameat2(directory_fd, source, destination, flags)
        return
    raise OSError(
        errno.ENOTSUP,
        "Atomic Codex configuration commits require macOS or Linux.",
    )


def _darwin_renameatx_np(
    directory_fd: int,
    source: str,
    destination: str,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.renameatx_np
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    result = function(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _linux_renameat2(
    directory_fd: int,
    source: str,
    destination: str,
    flags: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    arguments = (
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        flags,
    )
    if function is not None:
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(*arguments)
    else:
        syscall_number = {
            "aarch64": 276,
            "arm64": 276,
            "i386": 353,
            "i686": 353,
            "ppc64": 357,
            "ppc64le": 357,
            "riscv64": 276,
            "s390x": 347,
            "x86_64": 316,
        }.get(os.uname().machine)
        if syscall_number is None:
            raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        result = syscall(syscall_number, *arguments)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _assert_project_snapshot_current_fd(
    snapshot: _ConfigSnapshot,
    *,
    root_fd: int,
    directory_fd: int,
    directory_identity: tuple[int, int],
) -> None:
    if _identity(os.fstat(root_fd)) != snapshot.root_identity:
        raise _concurrent_project_error(snapshot.path)
    if (
        snapshot.directory_identity is not None
        and directory_identity != snapshot.directory_identity
    ):
        raise _concurrent_project_error(snapshot.path)
    try:
        fd = _open_regular_at(directory_fd, "config.toml")
    except FileNotFoundError:
        current_exists = False
        current_raw = b""
        current_identity = None
    except OSError:
        raise _concurrent_project_error(snapshot.path) from None
    else:
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            current_raw = handle.read()
        current_exists = True
        current_identity = _identity(metadata)
    current_digest = (
        hashlib.sha256(current_raw).hexdigest() if current_exists else None
    )
    if (
        current_exists != snapshot.exists
        or current_digest != snapshot.digest
        or current_identity != snapshot.file_identity
    ):
        raise _concurrent_project_error(snapshot.path)
