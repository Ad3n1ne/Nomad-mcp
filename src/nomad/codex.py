"""Public Codex project-configuration API and lifecycle orchestration."""

from __future__ import annotations

import os
import re

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from nomad import __version__
from nomad import codex_config as config
from nomad import codex_runtime as runtime


NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CodexConfigError = config.CodexConfigError

__all__ = [
    "CodexConfigError",
    "doctor_codex",
    "repair_codex",
    "setup_codex",
]


def setup_codex(
    project: str | os.PathLike[str] | None = None,
    name: str = "nomad",
) -> dict[str, Any]:
    """Starts or reuses a healthy daemon and configures this project."""
    return _configure_codex(project=project, name=name, repair=False)


def doctor_codex(
    project: str | os.PathLike[str] | None = None,
    name: str = "nomad",
) -> dict[str, Any]:
    """Diagnoses project Codex configuration without changing configuration."""
    project_root = runtime._resolve_project(project)
    _validate_name(name)
    config_path = project_root / ".codex" / "config.toml"
    global_path = config._global_config_path()
    token_env_var = runtime._project_token_env_var(project_root)

    project_snapshot, project_error = config._diagnostic_read_project(
        project_root
    )
    global_snapshot, global_error = config._diagnostic_read_global(global_path)
    daemon_module = runtime._daemon_module()
    state, daemon_error = runtime._diagnostic_daemon_status(
        daemon_module,
        project_root,
    )
    validated, healthy, daemon_error = _diagnose_daemon(
        state=state,
        daemon_error=daemon_error,
        project_root=project_root,
        daemon_module=daemon_module,
    )

    expected_url = validated.url if validated is not None else None
    project_report, global_report = _diagnose_configuration(
        project_snapshot=project_snapshot,
        project_error=project_error,
        global_snapshot=global_snapshot,
        global_error=global_error,
        project_root=project_root,
        name=name,
        url=expected_url,
        token_env_var=token_env_var,
    )
    restart_required = bool(
        validated is not None and validated.version != __version__
    )
    credential: Mapping[str, Any] = {
        "status": "unknown",
        "source_kind": "unknown",
        "source_status": "unknown",
        "source_ready": False,
        "process_environment_status": "unknown",
        "login_environment_status": "unknown",
        "host_environment_status": "unverified",
        "connection_verified": False,
        "action": "manual_host_environment_required",
        "codex_restart_required": True,
    }
    if healthy and validated is not None:
        credential = runtime._token_environment_status(
            validated.token_env_var,
            project_root=project_root,
            daemon_module=daemon_module,
        )
    token_env_status = str(credential["status"])
    codex_restart_required = bool(credential["codex_restart_required"])

    daemon_report: dict[str, Any] = {
        "status": str(state.get("status", "error")),
        "version": validated.version if validated is not None else None,
        "url": validated.url if validated is not None else None,
        "healthy": healthy,
    }
    if daemon_error is not None:
        daemon_report["error_type"] = daemon_error

    trust_status = str(global_report.get("trust_status", "unknown"))
    global_clear = _global_is_clear(global_report)
    ok = bool(
        healthy
        and project_report.get("match") is True
        and global_clear
        and not restart_required
        and credential["source_ready"] is True
        and trust_status == "trusted"
    )
    messages = _doctor_messages(
        healthy=healthy,
        project_match=project_report.get("match") is True,
        global_report=global_report,
        restart_required=restart_required,
        credential=credential,
        trust_status=trust_status,
    )
    return {
        "ok": ok,
        "status": _doctor_status(
            ok=ok,
            healthy=healthy,
            project_match=project_report.get("match") is True,
            global_report=global_report,
            global_clear=global_clear,
            restart_required=restart_required,
            trust_status=trust_status,
        ),
        "project_root": str(project_root),
        "config_path": str(config_path),
        "daemon": daemon_report,
        "project_config": project_report,
        "global_config": global_report,
        "token_env_present": credential["source_ready"] is True,
        "token_env_status": token_env_status,
        "credential_source_kind": credential["source_kind"],
        "credential_source_status": credential["source_status"],
        "credential_source_ready": credential["source_ready"],
        "token_process_environment_status": credential[
            "process_environment_status"
        ],
        "token_login_environment_status": credential[
            "login_environment_status"
        ],
        "token_host_environment_status": credential[
            "host_environment_status"
        ],
        "token_env_action": credential["action"],
        "connection_verified": credential["connection_verified"],
        "trust_status": trust_status,
        "restart_required": restart_required,
        "codex_restart_required": codex_restart_required,
        "messages": messages,
    }


def repair_codex(
    project: str | os.PathLike[str] | None = None,
    name: str = "nomad",
) -> dict[str, Any]:
    """Repairs daemon health/version and project-scoped Codex configuration."""
    return _configure_codex(project=project, name=name, repair=True)


def _diagnose_configuration(
    *,
    project_snapshot: config._ConfigSnapshot | None,
    project_error: CodexConfigError | None,
    global_snapshot: config._ConfigSnapshot | None,
    global_error: CodexConfigError | None,
    project_root: Path,
    name: str,
    url: str | None,
    token_env_var: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    project_report = config._project_diagnostic(
        project_snapshot,
        project_error,
        name=name,
        url=url,
        token_env_var=token_env_var,
    )
    global_report = config._global_diagnostic(
        global_snapshot,
        global_error,
        project_root=project_root,
        name=name,
        url=url,
        token_env_var=token_env_var,
    )
    return project_report, global_report


def _diagnose_daemon(
    *,
    state: Mapping[str, Any],
    daemon_error: str | None,
    project_root: Path,
    daemon_module: Any,
) -> tuple[runtime._ValidatedDaemon | None, bool, str | None]:
    validated = None
    healthy = False
    if state.get("status") == "running" and state.get("running") is True:
        try:
            validated = runtime._validate_daemon_state(
                state,
                project_root=project_root,
                daemon_module=daemon_module,
            )
            runtime._authenticated_health(
                validated,
                project_root=project_root,
                daemon_module=daemon_module,
            )
            healthy = True
        except CodexConfigError as exc:
            daemon_error = exc.error_type
    elif state.get("status") == "ownership_mismatch":
        daemon_error = "daemon_ownership_mismatch"
    return validated, healthy, daemon_error


def _configure_codex(
    *,
    project: str | os.PathLike[str] | None,
    name: str,
    repair: bool,
) -> dict[str, Any]:
    project_root = runtime._resolve_project(project)
    _validate_name(name)
    daemon_module = runtime._daemon_module()
    with runtime._codex_transaction_lock(project_root, daemon_module):
        return _configure_codex_locked(
            project_root=project_root,
            name=name,
            repair=repair,
            daemon_module=daemon_module,
        )


def _configure_codex_locked(
    *,
    project_root: Path,
    name: str,
    repair: bool,
    daemon_module: Any,
) -> dict[str, Any]:
    config_path = project_root / ".codex" / "config.toml"
    global_path = config._global_config_path()
    token_env_var = runtime._project_token_env_var(project_root)
    project_snapshot = config._read_project_config(project_root)
    global_snapshot = config._read_global_config(global_path)
    preliminary_global = config._analyze_global(
        global_snapshot,
        project_root=project_root,
        name=name,
        url=None,
        token_env_var=token_env_var,
    )
    config._raise_for_global_blocker(preliminary_global)

    started_instance_id: str | None = None
    config_committed = False
    try:
        validated, daemon_action, started_instance_id = _ready_daemon(
            project_root=project_root,
            repair=repair,
            daemon_module=daemon_module,
        )
        changed, postcommit_global = _commit_project_configuration(
            project_root=project_root,
            name=name,
            validated=validated,
            project_snapshot=project_snapshot,
            global_snapshot=global_snapshot,
            global_path=global_path,
        )
        config_committed = True
        credential = runtime._configure_token_environment(
            validated.token_env_var,
            project_root=project_root,
            daemon_module=daemon_module,
        )
    except BaseException as exc:
        committed = config_committed or (
            isinstance(exc, CodexConfigError)
            and exc.details.get("config_committed") is True
        )
        if started_instance_id is not None and not committed:
            runtime._rollback_started_daemon(
                project_root=project_root,
                instance_id=started_instance_id,
                daemon_module=daemon_module,
            )
        raise

    return _configuration_result(
        project_root=project_root,
        config_path=config_path,
        name=name,
        repair=repair,
        changed=changed,
        validated=validated,
        daemon_action=daemon_action,
        global_report=postcommit_global,
        credential=credential,
    )


def _ready_daemon(
    *,
    project_root: Path,
    repair: bool,
    daemon_module: Any,
) -> tuple[runtime._ValidatedDaemon, str, str | None]:
    state = runtime._daemon_status(daemon_module, project_root)
    state, daemon_action, started_instance_id = runtime._prepare_daemon(
        state,
        project_root=project_root,
        repair=repair,
        daemon_module=daemon_module,
    )
    try:
        validated = runtime._validate_daemon_state(
            state,
            project_root=project_root,
            daemon_module=daemon_module,
        )
        health_error = runtime._health_error_or_none(
            validated,
            project_root=project_root,
            daemon_module=daemon_module,
        )
        already_restarted = daemon_action == "restarted"
        if already_restarted:
            if validated.version != __version__:
                raise runtime._version_mismatch(project_root, repair=True)
            if health_error is not None:
                raise health_error
            return validated, daemon_action, started_instance_id

        needs_restart = repair and (
            health_error is not None or validated.version != __version__
        )
        if needs_restart:
            state = runtime._daemon_call(
                "daemon_restart_failed",
                daemon_module.restart_daemon,
                project=project_root,
            )
            validated = runtime._validate_daemon_state(
                state,
                project_root=project_root,
                daemon_module=daemon_module,
            )
            if validated.version != __version__:
                raise runtime._version_mismatch(project_root, repair=True)
            runtime._authenticated_health(
                validated,
                project_root=project_root,
                daemon_module=daemon_module,
            )
            return validated, "restarted", started_instance_id

        if health_error is not None:
            raise health_error
        if validated.version != __version__:
            raise runtime._version_mismatch(project_root, repair=repair)
        return validated, daemon_action, started_instance_id
    except BaseException:
        if started_instance_id is not None:
            runtime._rollback_started_daemon(
                project_root=project_root,
                instance_id=started_instance_id,
                daemon_module=daemon_module,
            )
        raise


def _commit_project_configuration(
    *,
    project_root: Path,
    name: str,
    validated: runtime._ValidatedDaemon,
    project_snapshot: config._ConfigSnapshot,
    global_snapshot: config._ConfigSnapshot,
    global_path: Path,
) -> tuple[bool, dict[str, Any]]:
    final_precommit_global = config._analyze_global(
        global_snapshot,
        project_root=project_root,
        name=name,
        url=validated.url,
        token_env_var=validated.token_env_var,
    )
    config._raise_for_global_blocker(final_precommit_global)
    config._assert_global_snapshot_current(global_snapshot)
    changed = config._update_project_config(
        project_snapshot,
        name=name,
        url=validated.url,
        token_env_var=validated.token_env_var,
    )
    postcommit_global = config._postcommit_global_report(
        global_path=global_path,
        project_root=project_root,
        name=name,
        url=validated.url,
        token_env_var=validated.token_env_var,
    )
    config._raise_for_postcommit_global_blocker(postcommit_global)
    return changed, postcommit_global


def _configuration_result(
    *,
    project_root: Path,
    config_path: Path,
    name: str,
    repair: bool,
    changed: bool,
    validated: runtime._ValidatedDaemon,
    daemon_action: str,
    global_report: Mapping[str, Any],
    credential: Mapping[str, Any],
) -> dict[str, Any]:
    trust_status = str(global_report["trust_status"])
    codex_restart_required = bool(
        changed or credential["codex_restart_required"]
    )
    manual_reasons: list[str] = []
    if credential["source_ready"] is not True:
        manual_reasons.append("token_environment")
    if trust_status != "trusted":
        manual_reasons.append("project_trust")
    connection_actions = (
        ["codex_restart"] if codex_restart_required else []
    )
    ok = not manual_reasons
    return {
        "ok": ok,
        "status": "ok" if ok else "manual_action_required",
        "action": "repair" if repair else "setup",
        "project_root": str(project_root),
        "config_path": str(config_path),
        "name": name,
        "config_changed": changed,
        "config_committed": True,
        "daemon": {
            "status": "running",
            "version": validated.version,
            "url": validated.url,
            "healthy": True,
            "action": daemon_action,
        },
        "global_config": dict(global_report),
        "trust_status": trust_status,
        "token_env_var": validated.token_env_var,
        "token_env_action": credential["action"],
        "token_env_status": credential["status"],
        "credential_source_kind": credential["source_kind"],
        "credential_source_status": credential["source_status"],
        "credential_source_ready": credential["source_ready"],
        "token_process_environment_status": credential[
            "process_environment_status"
        ],
        "token_login_environment_status": credential[
            "login_environment_status"
        ],
        "token_host_environment_status": credential[
            "host_environment_status"
        ],
        "connection_verified": credential["connection_verified"],
        "codex_restart_required": codex_restart_required,
        "restart_required": False,
        "manual_actions": manual_reasons,
        "connection_actions": connection_actions,
        "messages": _configuration_messages(
            credential=credential,
            trust_status=trust_status,
            codex_restart_required=codex_restart_required,
        ),
    }


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or NAME_RE.fullmatch(name) is None:
        raise config._error(
            "invalid_name",
            name=name if isinstance(name, str) else type(name).__name__,
            message=(
                "Server name must use 1-64 letters, digits, underscores, "
                "or hyphens."
            ),
        )


def _global_is_clear(global_report: Mapping[str, Any]) -> bool:
    return not (
        global_report.get("conflict")
        or global_report.get("stale_global")
        or global_report.get("status") == "error"
    )


def _doctor_status(
    *,
    ok: bool,
    healthy: bool,
    project_match: bool,
    global_report: Mapping[str, Any],
    global_clear: bool,
    restart_required: bool,
    trust_status: str,
) -> str:
    if ok:
        return "ok"
    if global_report.get("conflict") or global_report.get("stale_global"):
        return "conflict"
    if trust_status == "untrusted":
        return "untrusted"
    if healthy and project_match and not restart_required and global_clear:
        return "manual_action_required"
    return "repair_required"


def _doctor_messages(
    *,
    healthy: bool,
    project_match: bool,
    global_report: Mapping[str, Any],
    restart_required: bool,
    credential: Mapping[str, Any],
    trust_status: str,
) -> list[str]:
    messages: list[str] = []
    if global_report.get("message"):
        messages.append(str(global_report["message"]))
    if not healthy:
        messages.append("Start or repair the authenticated project daemon.")
    if not project_match:
        messages.append("Run repair after resolving any reported blocker.")
    if restart_required:
        messages.append("Run repair to restart the daemon with this Nomad version.")
    if credential["source_ready"] is True:
        messages.append(
            "The credential source is ready, but this CLI cannot verify that "
            "the running Codex connection inherited it."
        )
    elif credential["login_environment_status"] != "not_applicable":
        messages.append(
            "Make the daemon token available in the current process or the "
            "macOS login environment."
        )
    else:
        messages.append(
            "Configure the daemon token in the Codex host startup environment."
        )
    if credential["host_environment_status"] == "unverified":
        messages.append(
            "Restart Codex and verify the MCP connection from inside Codex; "
            "host token inheritance is unverified from this CLI."
        )
    if trust_status != "trusted" and not global_report.get("message"):
        messages.append("Mark the canonical project trusted manually in Codex.")
    return messages


def _configuration_messages(
    *,
    credential: Mapping[str, Any],
    trust_status: str,
    codex_restart_required: bool,
) -> list[str]:
    messages: list[str] = []
    if credential["action"] == "manual_login_environment_required":
        messages.append(
            "Configure the project token in the Codex startup environment manually."
        )
    if credential["action"] == "manual_export_required":
        messages.append(
            "Export the project token environment variable before starting Codex."
        )
    if credential["action"] == "manual_host_environment_required":
        messages.append(
            "Configure the project token in the Codex host startup environment."
        )
    if credential["action"] == "manual_credential_source_required":
        messages.append(
            "Export the project token or configure it in the macOS login environment."
        )
    if codex_restart_required:
        messages.append(
            "Start a new Codex connection so it reloads the project "
            "configuration and credential source."
        )
    if trust_status != "trusted":
        messages.append("Review and set project trust manually in Codex.")
    return messages
