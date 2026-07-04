"""
rsync synchronization tools.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import List, Optional

from nomad.config import (
    ConfigError,
    guard_remote,
    load_config,
    resolve_target,
    resolve_target_with_name,
)
from nomad.result import failure_result, success_result
from nomad.security import (
    verify_local_cwd_safety,
    verify_remote_path_safety,
    write_audit_log,
)
from nomad.ssh import (
    CONTROL_PATH,
    execute_remote_cmd_sync,
    probe_ssh_connectivity_result,
)
from nomad.truncate import safe_truncate

BUILTIN_EXCLUDES: list[str] = [
    ".git/",
    ".DS_Store",
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".idea/",
    ".vscode/",
    "node_modules/",
    ".pytest_cache/",
    "*.egg-info/",
    "dist/",
    "build/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".nomad.json",
    ".nomad.json.bak",
    ".nomad.local.json",
    ".venv/",
    "venv/",
]
REMOTE_RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
RSYNC_DELETE_THRESHOLD = 50


def _matches_builtin(pattern: str) -> bool:
    clean = pattern.strip().lstrip("/")
    clean_no_slash = clean.rstrip("/")
    for b in BUILTIN_EXCLUDES:
        b_clean = b.lstrip("/")
        b_no_slash = b_clean.rstrip("/")
        if clean == b_clean or clean_no_slash == b_no_slash:
            return True
        if b_clean.startswith("*.") and clean_no_slash.endswith(b_clean[1:]):
            return True
    return False


def convert_gitignore_to_rsync(
    content: str, extra_excludes: Optional[List[str]] = None
) -> list[str]:
    """Parses gitignore to compatible rsync exclude filter format.

    Note: Supports MVP gitignore rules (comments, empty lines, negation !, root / prefix, directory / suffix).
    Does not support recursive multi-level gitignore merging or complex git wildcards.
    Builtin excludes (.git/, .nomad.json, secrets, build artifacts) can NEVER be overridden by negation rules.
    """
    includes: list[str] = []
    user_excludes: list[str] = []
    builtin_rules: list[str] = [f"- {pattern}" for pattern in BUILTIN_EXCLUDES]

    if extra_excludes:
        for pattern in extra_excludes:
            pattern = pattern.strip()
            if pattern:
                rule = f"- {pattern}"
                if rule not in builtin_rules and rule not in user_excludes:
                    user_excludes.append(rule)

    if content:
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("!"):
                negated_pattern = line[1:].strip()
                if _matches_builtin(negated_pattern):
                    # Builtin excludes can never be un-excluded
                    continue
                rule = f"+ {negated_pattern}"
                if rule not in includes:
                    includes.append(rule)
            else:
                rule = f"- {line}"
                if rule not in builtin_rules and rule not in user_excludes:
                    user_excludes.append(rule)

    return builtin_rules + includes + user_excludes


def _validate_remote_relative_path(remote_relative_path: str) -> str:
    if not isinstance(remote_relative_path, str) or not remote_relative_path:
        raise ValueError("remote_relative_path must be a non-empty relative path")
    if "\x00" in remote_relative_path:
        raise ValueError("remote_relative_path must not contain null bytes")
    path = PurePosixPath(remote_relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("remote_relative_path must stay inside remote_path")
    if not REMOTE_RELATIVE_PATH_RE.fullmatch(remote_relative_path):
        raise ValueError("remote_relative_path contains unsafe characters")
    clean = path.as_posix()
    if clean in {"", "."}:
        raise ValueError("remote_relative_path must name a file or directory")
    return clean


def _resolve_local_dest(local_dest: str | None, target_name: str) -> Path:
    project_root = Path.cwd().resolve()
    if local_dest is None:
        dest = project_root / "remote_artifacts" / target_name
    else:
        dest = Path(local_dest)
        if not dest.is_absolute():
            dest = project_root / dest
    resolved = dest.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("local_dest must stay inside current project directory") from exc
    return resolved


def _local_transfer_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def _infer_saved_path(local_dest: Path, remote_relative_path: str) -> Path:
    clean = remote_relative_path.rstrip("/")
    name = PurePosixPath(clean).name
    return local_dest / name if name else local_dest


def _parse_rsync_deleted_paths(output: str) -> list[str]:
    deleted_paths: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("*deleting "):
            deleted_paths.append(line.removeprefix("*deleting ").strip())
        elif line.startswith("deleting "):
            deleted_paths.append(line.removeprefix("deleting ").strip())
    return deleted_paths


def _delete_summary(deleted_paths: list[str]) -> dict[str, object]:
    preview_limit = 10
    return {
        "delete_count": len(deleted_paths),
        "threshold": RSYNC_DELETE_THRESHOLD,
        "deleted_preview": deleted_paths[:preview_limit],
        "preview_truncated": len(deleted_paths) > preview_limit,
    }


def sync_push(target: str = "default") -> str:
    """Synchronizes local files to the remote workspace target."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="sync_push",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="sync_push",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="sync_push",
            target=target,
            message="Remote synchronization is disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    cwd_err = verify_local_cwd_safety()
    if cwd_err is not None:
        return failure_result(
            tool="sync_push",
            target=target,
            message="Current working directory is unsafe for remote operations.",
            error_type=cwd_err,
            recoverable=False,
        )

    try:
        target_cfg = resolve_target(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="sync_push",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    ssh_host = target_cfg["ssh_host"]
    remote_path = target_cfg["remote_path"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")
    local_subpath = target_cfg.get("local_subpath")
    project_name = config.get("project_name", "unnamed")

    path_err = verify_remote_path_safety(remote_path)
    if path_err is not None:
        return failure_result(
            tool="sync_push",
            target=target,
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Safety check failed for {remote_path}"],
        )

    if local_subpath:
        source_path = (Path.cwd() / local_subpath).resolve()
        try:
            source_path.relative_to(Path.cwd().resolve())
        except ValueError:
            return failure_result(
                tool="sync_push",
                target=target,
                message=f"Local subpath '{local_subpath}' escapes working directory.",
                error_type="path_traversal",
                recoverable=False,
            )
        if not source_path.exists():
            return failure_result(
                tool="sync_push",
                target=target,
                message=f"Local subpath '{local_subpath}' does not exist.",
                error_type="invalid_config",
                recoverable=True,
            )
    else:
        source_path = Path.cwd()

    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="sync_push",
            target=target,
            message=f"SSH preflight failed before rsync for target '{target}'.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            details={"ssh_host": ssh_host},
            diagnostics=preflight.get("diagnostics", []),
            next_action={"tool": "net_diagnose", "args": {"target": target}},
        )

    if target_cfg.get("auto_create_remote_path") is True:
        try:
            ret, stdout, stderr = execute_remote_cmd_sync(
                ssh_host, f"mkdir -p {shlex.quote(remote_path)}", jump_host=jump_host
            )
            if ret != 0:
                return failure_result(
                    tool="sync_push",
                    target=target,
                    message=f"Failed to create remote directory '{remote_path}'.",
                    error_type="ssh_unknown_failure",
                    recoverable=True,
                    diagnostics=[stderr.strip() or f"mkdir exit code {ret}"],
                )
        except Exception as exc:
            return failure_result(
                tool="sync_push",
                target=target,
                message=f"SSH error creating remote directory: {exc}",
                error_type="ssh_unknown_failure",
                recoverable=True,
                diagnostics=[str(exc)],
            )

    sync_cfg = target_cfg.get("sync") or {}
    respect_gitignore = sync_cfg.get("respect_gitignore", True)

    gitignore_content = ""
    if respect_gitignore:
        gitignore_file = Path.cwd() / ".gitignore"
        if gitignore_file.exists():
            gitignore_content = gitignore_file.read_text(encoding="utf-8")

    extra_excludes = sync_cfg.get("extra_excludes", [])
    filter_rules = convert_gitignore_to_rsync(gitignore_content, extra_excludes)


    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, prefix="nomad_filter_") as tmp_filter:
        tmp_filter_path = Path(tmp_filter.name)
        tmp_filter.write("\n".join(filter_rules) + "\n")

    try:
        ssh_e = f"ssh -o ControlMaster=auto -o ControlPath={CONTROL_PATH} -o ControlPersist=60s -o ConnectTimeout=5 -o BatchMode=yes"
        if jump_host:
            ssh_e += f" -J {jump_host}"

        rsync_argv = [
            "rsync",
            "-az",
            "--delete",
            "-e",
            ssh_e,
            f"--filter=merge {tmp_filter_path}",
            f"{source_path}/",
            f"{ssh_host}:{remote_path}/",
        ]

        dry_run_argv = rsync_argv[:3] + ["--dry-run", "--itemize-changes"] + rsync_argv[3:]
        dry_run = subprocess.run(
            dry_run_argv, capture_output=True, text=True, timeout=60
        )
        if dry_run.returncode != 0:
            return failure_result(
                tool="sync_push",
                target=target,
                message=f"rsync dry-run failed with exit code {dry_run.returncode}.",
                error_type="rsync_failed",
                recoverable=True,
                diagnostics=[dry_run.stderr.strip() or f"rsync dry-run exited with code {dry_run.returncode}"],
            )

        deleted_paths = _parse_rsync_deleted_paths(dry_run.stdout or "")
        delete_summary = _delete_summary(deleted_paths)
        if len(deleted_paths) > RSYNC_DELETE_THRESHOLD:
            return failure_result(
                tool="sync_push",
                target=target,
                message=(
                    f"rsync dry-run would delete {len(deleted_paths)} paths, "
                    f"exceeding threshold {RSYNC_DELETE_THRESHOLD}. Manual confirmation is required."
                ),
                error_type="rsync_delete_threshold_exceeded",
                recoverable=True,
                data={"delete_summary": delete_summary},
                diagnostics=[
                    "Automatic sync was not executed because the deletion threshold was exceeded.",
                    "Review the dry-run deletion preview before retrying with an explicit confirmation flow.",
                ],
            )

        completed = subprocess.run(
            rsync_argv, capture_output=True, text=True, timeout=60
        )

        if completed.returncode != 0:
            return failure_result(
                tool="sync_push",
                target=target,
                message=f"rsync failed with exit code {completed.returncode}.",
                error_type="rsync_failed",
                recoverable=True,
                diagnostics=[completed.stderr.strip() or f"rsync exited with code {completed.returncode}"],
            )

        write_audit_log(
            project_name,
            "sync_push",
            f"target={target} ssh_host={ssh_host} remote_path={remote_path}",
        )

        output_tail = safe_truncate(completed.stdout or "")
        return success_result(
            tool="sync_push",
            target=target,
            message=f"Successfully synchronized local workspace to target '{target}'.",
            data={
                "target": target,
                "ssh_host": ssh_host,
                "source_path": str(source_path),
                "remote_path": remote_path,
                "delete_summary": delete_summary,
                "output_tail": output_tail,
            },
        )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="sync_push",
            target=target,
            message="rsync process timed out after 60 seconds.",
            error_type="command_timeout",
            recoverable=True,
            diagnostics=["rsync process exceeded 60 seconds timeout."],
        )
    finally:
        if tmp_filter_path.exists():
            tmp_filter_path.unlink()


def sync_pull(
    remote_relative_path: str, target: str = "default", local_dest: str | None = None
) -> str:
    """Pulls a remote artifact into a local project-owned directory."""
    try:
        config = load_config()
    except ConfigError as exc:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"Invalid configuration: {exc}",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return failure_result(
            tool="sync_pull",
            target=target,
            message="Project is unconfigured. Run init_discover first.",
            error_type="unconfigured",
            recoverable=True,
        )
    if remote_guard == "local_mode":
        return failure_result(
            tool="sync_pull",
            target=target,
            message="Remote synchronization is disabled in local mode.",
            error_type="local_mode",
            recoverable=False,
        )

    cwd_err = verify_local_cwd_safety()
    if cwd_err is not None:
        return failure_result(
            tool="sync_pull",
            target=target,
            message="Current working directory is unsafe for remote operations.",
            error_type=cwd_err,
            recoverable=False,
        )

    try:
        target_name, target_cfg = resolve_target_with_name(config, target)
    except ConfigError as exc:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"Target '{target}' not found: {exc}",
            error_type="target_not_found",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    try:
        safe_relative_path = _validate_remote_relative_path(remote_relative_path)
    except ValueError as exc:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"Invalid remote relative path: {exc}",
            error_type="path_traversal",
            recoverable=False,
            diagnostics=[str(exc)],
        )

    try:
        local_dest_path = _resolve_local_dest(local_dest, target_name)
    except ValueError as exc:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"Invalid local destination: {exc}",
            error_type="path_traversal",
            recoverable=False,
            diagnostics=[str(exc)],
        )

    ssh_host = target_cfg["ssh_host"]
    remote_path = target_cfg["remote_path"]
    jump_host = (target_cfg.get("network") or {}).get("jump_host")
    project_name = config.get("project_name", "unnamed")

    path_err = verify_remote_path_safety(remote_path)
    if path_err is not None:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"Remote path '{remote_path}' is unsafe or invalid.",
            error_type="unsafe_remote_path",
            recoverable=True,
            diagnostics=[f"Safety check failed for {remote_path}"],
        )

    if local_dest_path.exists() and not local_dest_path.is_dir():
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"Local destination '{local_dest_path}' exists and is not a directory.",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[f"Expected local_dest to be a directory: {local_dest_path}"],
        )

    try:
        local_dest_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"Failed to create local destination '{local_dest_path}'.",
            error_type="invalid_config",
            recoverable=True,
            diagnostics=[str(exc)],
        )

    preflight = probe_ssh_connectivity_result(ssh_host, timeout=5, jump_host=jump_host)
    if not preflight["ok"]:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"SSH preflight failed before rsync for target '{target}'.",
            error_type=preflight.get("error_type", "ssh_unknown_failure"),
            recoverable=preflight.get("recoverable", True),
            details={"ssh_host": ssh_host},
            diagnostics=preflight.get("diagnostics", []),
            next_action={"tool": "net_diagnose", "args": {"target": target}},
        )

    remote_source = f"{ssh_host}:{remote_path.rstrip('/')}/{safe_relative_path}"
    ssh_e = (
        f"ssh -o ControlMaster=auto -o ControlPath={CONTROL_PATH} "
        f"-o ControlPersist=60s -o ConnectTimeout=5 -o BatchMode=yes"
    )
    if jump_host:
        ssh_e += f" -J {jump_host}"

    rsync_argv = [
        "rsync",
        "-az",
        "-e",
        ssh_e,
        remote_source,
        f"{local_dest_path}/",
    ]

    try:
        completed = subprocess.run(
            rsync_argv, capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        return failure_result(
            tool="sync_pull",
            target=target,
            message="rsync process timed out after 60 seconds.",
            error_type="command_timeout",
            recoverable=True,
            diagnostics=["rsync process exceeded 60 seconds timeout."],
        )

    if completed.returncode != 0:
        return failure_result(
            tool="sync_pull",
            target=target,
            message=f"rsync failed with exit code {completed.returncode}.",
            error_type="rsync_failed",
            recoverable=True,
            diagnostics=[completed.stderr.strip() or f"rsync exited with code {completed.returncode}"],
        )

    saved_path = _infer_saved_path(local_dest_path, safe_relative_path)
    transfer_size = _local_transfer_size(saved_path)
    write_audit_log(
        project_name,
        "sync_pull",
        f"target={target_name} ssh_host={ssh_host} remote_path={remote_path}/{safe_relative_path}",
    )

    return success_result(
        tool="sync_pull",
        target=target_name,
        message=f"Successfully pulled remote artifact from target '{target_name}'.",
        data={
            "target": target_name,
            "ssh_host": ssh_host,
            "remote_path": f"{remote_path.rstrip('/')}/{safe_relative_path}",
            "local_dest": str(local_dest_path),
            "saved_path": str(saved_path),
            "bytes": transfer_size,
            "output_tail": safe_truncate(completed.stdout or ""),
        },
    )
