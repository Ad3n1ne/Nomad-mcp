import hashlib
import json
import multiprocessing
import os
import stat
import subprocess
import time

from pathlib import Path

import pytest
import tomlkit

from nomad import __version__
from nomad import codex
from nomad import codex_config
from nomad import codex_runtime


SECRET = "test-secret-that-must-not-leak"


def token_env_var(project):
    digest = hashlib.sha256(os.fsencode(str(project.resolve()))).hexdigest()
    return f"NOMAD_MCP_BEARER_TOKEN_{digest[:16].upper()}"


def daemon_state(project, **overrides):
    state = {
        "status": "running",
        "running": True,
        "already_running": False,
        "project_root": str(project.resolve()),
        "pid": 4321,
        "host": "127.0.0.1",
        "port": 54321,
        "path": "/mcp",
        "url": "http://127.0.0.1:54321/mcp",
        "version": __version__,
        "auth": True,
        "allow_remote": False,
        "instance_id": "instance-1",
        "token_env_var": token_env_var(project),
        "log_path": "/private/daemon.log",
        "token_path": "/private/daemon.token",
    }
    state.update(overrides)
    return state


class FakeDaemon:
    def __init__(self, project, status=None):
        self.project = project.resolve()
        self.DEFAULT_DAEMONS_DIR = project.parent / "daemon-locks"
        self.state = status or daemon_state(project)
        self.calls = []
        self.health_pid = self.state.get("pid", 4321)
        self.health_error = None

    def status_daemon(self, **kwargs):
        self.calls.append(("status", kwargs))
        return dict(self.state)

    def start_daemon(self, **kwargs):
        self.calls.append(("start", kwargs))
        self.state = daemon_state(self.project, already_running=False)
        return dict(self.state)

    def restart_daemon(self, **kwargs):
        self.calls.append(("restart", kwargs))
        self.state = daemon_state(self.project, already_running=False)
        return dict(self.state)

    def stop_daemon(self, **kwargs):
        self.calls.append(("stop", kwargs))
        self.state = {
            "status": "stopped",
            "running": False,
            "project_root": str(self.project),
        }
        return dict(self.state)

    def read_daemon_token(self, **kwargs):
        self.calls.append(("token", kwargs))
        return SECRET

    def _mcp_health_data(self, url, bearer_token, *, timeout):
        self.calls.append(("health", url, bearer_token, timeout))
        if self.health_error is not None:
            raise self.health_error
        return {"pid": self.health_pid}

    @staticmethod
    def is_loopback_host(host):
        return host in {"127.0.0.1", "::1", "localhost"}


@pytest.fixture
def codex_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(codex_runtime.sys, "platform", "linux")
    return project, codex_home


@pytest.fixture
def fake_daemon(codex_project, monkeypatch):
    project, _ = codex_project
    fake = FakeDaemon(project)
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)
    return fake


def write_project_config(project, text, mode=None):
    directory = project / ".codex"
    directory.mkdir(exist_ok=True)
    path = directory / "config.toml"
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def write_global_config(codex_home, text):
    path = codex_home / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def serialized(value):
    return json.dumps(value, sort_keys=True)


def trusted_global_text(project, trust_level="trusted"):
    return f"""[projects."{project.resolve()}"]
trust_level = "{trust_level}"
"""


def _locked_config_worker(
    project_value,
    lock_dir_value,
    name,
    gate,
    results,
):
    project = Path(project_value)

    class LockOnlyDaemon:
        DEFAULT_DAEMONS_DIR = Path(lock_dir_value)

    try:
        gate.wait(timeout=5)
        with codex_runtime._codex_transaction_lock(project, LockOnlyDaemon):
            snapshot = codex_config._read_project_config(project)
            time.sleep(0.15)
            codex_config._update_project_config(
                snapshot,
                name=name,
                url=f"http://127.0.0.1:54321/{name}",
                token_env_var=f"TOKEN_{name.upper()}",
            )
        results.put(None)
    except BaseException as exc:
        details = getattr(exc, "details", None)
        results.put(f"{type(exc).__name__}: {exc}: {details}")


def test_setup_converts_stdio_to_http_and_preserves_comments_and_unknown_fields(
    codex_project, fake_daemon
):
    project, _ = codex_project
    path = write_project_config(
        project,
        """# keep top
[mcp_servers.nomad]
# keep policy
command = "uvx"
args = ["nomad"]
env = { OLD = "value" }
env_vars = ["OLD"]
cwd = "/tmp"
startup_timeout_sec = 120
enabled = true

[features]
experimental = true
""",
    )

    result = codex.setup_codex(project)

    rendered = path.read_text(encoding="utf-8")
    parsed = tomlkit.parse(rendered)
    entry = parsed["mcp_servers"]["nomad"]
    assert result["config_changed"] is True
    assert result["token_env_action"] == "manual_export_required"
    assert result["codex_restart_required"] is True
    assert entry["url"] == fake_daemon.state["url"]
    assert entry["bearer_token_env_var"] == token_env_var(project)
    assert entry["startup_timeout_sec"] == 120
    assert entry["enabled"] is True
    assert not codex_config.STDIO_FIELDS.intersection(entry)
    assert "# keep top" in rendered
    assert "# keep policy" in rendered
    assert parsed["features"]["experimental"] is True


def test_setup_is_idempotent_and_does_not_rewrite_unchanged_config(
    codex_project, fake_daemon
):
    project, _ = codex_project
    first = codex.setup_codex(project)
    path = Path(first["config_path"])
    first_stat = path.stat()
    first_bytes = path.read_bytes()

    second = codex.setup_codex(project)

    assert second["config_changed"] is False
    assert path.read_bytes() == first_bytes
    assert path.stat().st_ino == first_stat.st_ino


def test_setup_preserves_existing_config_mode(codex_project, fake_daemon):
    project, _ = codex_project
    path = write_project_config(
        project,
        "[mcp_servers.nomad]\ncommand = \"nomad\"\n",
        mode=0o640,
    )

    codex.setup_codex(project)

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_setup_rejects_malformed_project_toml_without_starting_daemon(
    codex_project, monkeypatch
):
    project, _ = codex_project
    path = write_project_config(project, "[mcp_servers.nomad\nsecret = 'keep'\n")
    original = path.read_bytes()
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "malformed_toml"
    assert path.read_bytes() == original
    assert fake.calls == []


@pytest.mark.parametrize("target", ["file", "directory"])
def test_setup_rejects_symlinked_config_paths(
    codex_project, fake_daemon, tmp_path, target
):
    project, _ = codex_project
    outside = tmp_path / "outside"
    if target == "file":
        (project / ".codex").mkdir()
        outside.write_text("", encoding="utf-8")
        (project / ".codex" / "config.toml").symlink_to(outside)
    else:
        outside.mkdir()
        (project / ".codex").symlink_to(outside, target_is_directory=True)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type in {
        "unsafe_config_path",
        "unsafe_config_directory",
    }
    assert fake_daemon.calls == []


def test_setup_never_changes_global_config_and_fails_on_same_name(
    codex_project, monkeypatch
):
    project, codex_home = codex_project
    global_path = write_global_config(
        codex_home,
        """# user owned
[mcp_servers.nomad]
command = "other"

[projects."/tmp"]
trust_level = "trusted"
""",
    )
    original = global_path.read_bytes()
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "global_config_conflict"
    assert raised.value.details["conflict"] is True
    assert global_path.read_bytes() == original
    assert not (project / ".codex").exists()
    assert fake.calls == []


def test_setup_fails_on_stale_global_project_env_under_another_name(
    codex_project, fake_daemon
):
    project, codex_home = codex_project
    global_path = write_global_config(
        codex_home,
        f"""[mcp_servers.legacy]
url = "http://127.0.0.1:1111/mcp"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    original = global_path.read_bytes()

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "global_config_conflict"
    assert raised.value.details["stale_global"] is True
    assert raised.value.details["stale"] == ["legacy"]
    assert global_path.read_bytes() == original
    assert fake_daemon.calls == []


def test_setup_ignores_unrelated_global_entries_without_modifying_them(
    codex_project, fake_daemon
):
    project, codex_home = codex_project
    global_path = write_global_config(
        codex_home,
        """# untouched
[mcp_servers.other]
url = "http://127.0.0.1:54321/mcp"
bearer_token_env_var = "OTHER_TOKEN"
command = "nomad-looking"
""",
    )
    original = global_path.read_bytes()

    result = codex.setup_codex(project)

    assert result["status"] == "manual_action_required"
    assert result["ok"] is False
    assert global_path.read_bytes() == original


def test_concurrent_project_edit_is_not_overwritten_and_new_daemon_rolls_back(
    codex_project, monkeypatch
):
    project, _ = codex_project
    path = write_project_config(
        project,
        "[mcp_servers.nomad]\ncommand = \"nomad\"\n",
    )
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    original_assert = codex_config._assert_global_snapshot_current

    def concurrent_edit(snapshot):
        original_assert(snapshot)
        path.write_text("# concurrent user edit\n", encoding="utf-8")

    monkeypatch.setattr(
        codex_config,
        "_assert_global_snapshot_current",
        concurrent_edit,
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "concurrent_config_change"
    assert path.read_text(encoding="utf-8") == "# concurrent user edit\n"
    assert [call[0] for call in fake.calls].count("stop") == 1
    stop_call = next(call for call in fake.calls if call[0] == "stop")
    assert stop_call[1]["expected_instance_id"] == "instance-1"


def test_config_write_failure_does_not_damage_original_and_rolls_back(
    codex_project, monkeypatch
):
    project, _ = codex_project
    path = write_project_config(
        project,
        "# original\n[mcp_servers.nomad]\ncommand = \"nomad\"\n",
    )
    original = path.read_bytes()
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)
    monkeypatch.setattr(
        codex_config.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "config_write_failed"
    assert path.read_bytes() == original
    assert list(path.parent.glob(".config.toml.*")) == []
    assert [call[0] for call in fake.calls].count("stop") == 1


def test_existing_daemon_is_not_stopped_when_project_write_fails(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    path = write_project_config(
        project,
        "[mcp_servers.nomad]\ncommand = \"nomad\"\n",
    )
    monkeypatch.setattr(
        codex_config.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )

    with pytest.raises(codex.CodexConfigError):
        codex.setup_codex(project)

    assert path.exists()
    assert "stop" not in [call[0] for call in fake_daemon.calls]


def test_repair_starts_stopped_daemon(codex_project, monkeypatch):
    project, _ = codex_project
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    result = codex.repair_codex(project)

    assert result["daemon"]["action"] == "started"
    assert "start" in [call[0] for call in fake.calls]


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("state", "invalid_daemon_state"),
        ("version", "daemon_version_mismatch"),
        ("health", "daemon_health_failed"),
    ],
)
def test_started_daemon_post_start_failure_rolls_back_exact_instance(
    codex_project,
    monkeypatch,
    failure,
    expected_error,
):
    project, _ = codex_project
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )

    def start(**kwargs):
        fake.calls.append(("start", kwargs))
        overrides = {}
        if failure == "state":
            overrides["url"] = "http://127.0.0.1:1/wrong"
        elif failure == "version":
            overrides["version"] = "0.0.0"
        fake.state = daemon_state(project, **overrides)
        if failure == "health":
            fake.health_error = RuntimeError(SECRET)
        return dict(fake.state)

    fake.start_daemon = start
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == expected_error
    stop_calls = [call for call in fake.calls if call[0] == "stop"]
    assert len(stop_calls) == 1
    assert stop_calls[0][1]["expected_instance_id"] == "instance-1"
    assert fake.state["status"] == "stopped"
    assert not (project / ".codex").exists()


def test_repair_restarts_daemon_with_different_version(
    codex_project, monkeypatch
):
    project, _ = codex_project
    fake = FakeDaemon(project, status=daemon_state(project, version="0.0.0"))
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    result = codex.repair_codex(project)

    assert result["daemon"]["action"] == "restarted"
    assert result["daemon"]["version"] == __version__
    assert [call[0] for call in fake.calls].count("restart") == 1


def test_repair_restarts_daemon_that_fails_authenticated_health(
    codex_project, monkeypatch
):
    project, _ = codex_project
    fake = FakeDaemon(project)
    attempts = 0

    def health(url, bearer_token, *, timeout):
        nonlocal attempts
        attempts += 1
        fake.calls.append(("health", url, bearer_token, timeout))
        if attempts == 1:
            raise RuntimeError(SECRET)
        return {"pid": fake.state["pid"]}

    fake._mcp_health_data = health
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    result = codex.repair_codex(project)

    assert result["daemon"]["action"] == "restarted"
    assert attempts == 2
    assert SECRET not in serialized(result)


@pytest.mark.parametrize("operation", [codex.setup_codex, codex.repair_codex])
def test_mutating_operations_refuse_daemon_ownership_mismatch(
    codex_project, monkeypatch, operation
):
    project, _ = codex_project
    fake = FakeDaemon(
        project,
        status={
            "status": "ownership_mismatch",
            "running": False,
            "project_root": str(project),
            "pid": 999,
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        operation(project)

    assert raised.value.error_type == "daemon_ownership_mismatch"
    assert [call[0] for call in fake.calls] == ["status"]


def test_setup_requires_authenticated_health_pid_match(
    codex_project, fake_daemon
):
    project, _ = codex_project
    fake_daemon.health_pid = 9999

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "daemon_health_pid_mismatch"
    assert not (project / ".codex").exists()
    assert "stop" not in [call[0] for call in fake_daemon.calls]


def test_setup_prefers_public_authenticated_health_api(
    codex_project, fake_daemon
):
    project, _ = codex_project

    def health_daemon(**kwargs):
        fake_daemon.calls.append(("public_health", kwargs))
        return {
            "ok": True,
            "status": "healthy",
            "project_root": str(project),
            "pid": fake_daemon.state["pid"],
            "version": fake_daemon.state["version"],
            "url": fake_daemon.state["url"],
        }

    fake_daemon.health_daemon = health_daemon
    fake_daemon._mcp_health_data = lambda *args, **kwargs: pytest.fail(
        "private health helper must not run when the public API exists"
    )

    result = codex.setup_codex(project)

    assert result["daemon"]["healthy"] is True
    assert [call[0] for call in fake_daemon.calls].count("public_health") == 1


@pytest.mark.parametrize(
    "override",
    [
        {"host": "0.0.0.0", "url": "http://0.0.0.0:54321/mcp"},
        {"url": "http://127.0.0.1:9999/mcp"},
        {"path": "/other"},
        {"token_env_var": "NOMAD_MCP_BEARER_TOKEN_TAMPERED"},
    ],
)
def test_setup_rejects_tampered_daemon_endpoint_state(
    codex_project, monkeypatch, override
):
    project, _ = codex_project
    fake = FakeDaemon(project, status=daemon_state(project, **override))
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "invalid_daemon_state"
    assert "token" not in [call[0] for call in fake.calls]
    assert not (project / ".codex").exists()


def test_setup_on_non_macos_reports_missing_source_without_launchctl(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project

    def forbidden(*args, **kwargs):
        raise AssertionError("launchctl must not run")

    monkeypatch.setattr(codex_runtime.subprocess, "run", forbidden)

    result = codex.setup_codex(project)

    assert result["token_env_action"] == "manual_export_required"
    assert result["token_env_status"] == "missing"
    assert result["credential_source_kind"] == "process_environment"
    assert result["credential_source_status"] == "missing"
    assert result["credential_source_ready"] is False
    assert result["token_login_environment_status"] == "not_applicable"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["ok"] is False
    assert result["codex_restart_required"] is True


def test_setup_macos_never_passes_token_to_launchctl_argv(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    calls = []
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=b"")

    monkeypatch.setattr(codex_runtime.subprocess, "run", run)

    result = codex.setup_codex(project)

    assert calls[0][0] == [
        "launchctl",
        "getenv",
        token_env_var(project),
    ]
    assert len(calls) == 1
    assert result["token_env_action"] == "manual_credential_source_required"
    assert result["token_env_status"] == "missing"
    assert result["credential_source_kind"] == "none"
    assert result["credential_source_status"] == "missing"
    assert result["credential_source_ready"] is False
    assert result["token_process_environment_status"] == "missing"
    assert result["token_login_environment_status"] == "missing"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["status"] == "manual_action_required"
    assert result["ok"] is False
    assert result["codex_restart_required"] is True
    assert SECRET not in serialized(result)
    assert SECRET not in Path(result["config_path"]).read_text(encoding="utf-8")


def test_launchctl_failure_requires_manual_action_without_leaking_token(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")

    def fail(*args, **kwargs):
        raise OSError(SECRET)

    monkeypatch.setattr(codex_runtime.subprocess, "run", fail)

    result = codex.setup_codex(project)

    assert result["ok"] is False
    assert result["token_env_status"] == "unknown"
    assert result["credential_source_kind"] == "none"
    assert result["credential_source_status"] == "unknown"
    assert result["credential_source_ready"] is False
    assert result["token_process_environment_status"] == "missing"
    assert result["token_login_environment_status"] == "unknown"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["token_env_action"] == "manual_credential_source_required"
    assert result["config_committed"] is True
    assert SECRET not in serialized(result)


def test_doctor_is_read_only_and_reports_authenticated_stable_state(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    project_path = write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    global_path = write_global_config(
        codex_home,
        f"""[mcp_servers.other]
command = "other"

[projects."{project.resolve()}"]
trust_level = "trusted"
""",
    )
    monkeypatch.setenv(token_env_var(project), SECRET)
    project_original = project_path.read_bytes()
    global_original = global_path.read_bytes()

    result = codex.doctor_codex(project)

    assert result["status"] == "ok"
    assert result["ok"] is True
    assert result["daemon"] == {
        "status": "running",
        "version": __version__,
        "url": fake_daemon.state["url"],
        "healthy": True,
    }
    assert result["project_config"]["match"] is True
    assert result["global_config"]["status"] == "clear"
    assert result["token_env_present"] is True
    assert result["token_env_status"] == "present"
    assert result["credential_source_kind"] == "process_environment"
    assert result["credential_source_status"] == "present"
    assert result["credential_source_ready"] is True
    assert result["token_login_environment_status"] == "not_applicable"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["codex_restart_required"] is True
    assert result["restart_required"] is False
    assert any(
        "cannot verify" in message and "Codex connection" in message
        for message in result["messages"]
    )
    assert project_path.read_bytes() == project_original
    assert global_path.read_bytes() == global_original
    assert "log_path" not in serialized(result)
    assert "token_path" not in serialized(result)
    assert "start" not in [call[0] for call in fake_daemon.calls]
    assert "stop" not in [call[0] for call in fake_daemon.calls]
    assert [call[0] for call in fake_daemon.calls].count("token") == 2
    assert [call[0] for call in fake_daemon.calls].count("health") == 1


def test_doctor_reports_bad_toml_and_global_conflict_without_writes(
    codex_project, fake_daemon
):
    project, codex_home = codex_project
    project_path = write_project_config(project, "[broken\n")
    global_path = write_global_config(
        codex_home,
        "[mcp_servers.nomad]\ncommand = \"other\"\n",
    )
    project_original = project_path.read_bytes()
    global_original = global_path.read_bytes()

    result = codex.doctor_codex(project)

    assert result["status"] == "conflict"
    assert result["project_config"]["error_type"] == "malformed_toml"
    assert result["global_config"]["conflict"] is True
    assert result["global_config"]["message"]
    assert project_path.read_bytes() == project_original
    assert global_path.read_bytes() == global_original


def test_doctor_reports_owned_and_stale_global_entries_read_only(
    codex_project, fake_daemon
):
    project, codex_home = codex_project
    global_path = write_global_config(
        codex_home,
        f"""[mcp_servers.current]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"

[mcp_servers.old]
url = "http://127.0.0.1:1111/mcp"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    original = global_path.read_bytes()

    result = codex.doctor_codex(project, name="project_nomad")

    assert result["status"] == "conflict"
    assert result["global_config"]["owned"] == ["current"]
    assert result["global_config"]["stale"] == ["old"]
    assert result["global_config"]["stale_global"] is True
    assert global_path.read_bytes() == original


def test_doctor_reports_restart_required_for_version_mismatch(
    codex_project, monkeypatch
):
    project, _ = codex_project
    fake = FakeDaemon(project, status=daemon_state(project, version="0.1.0"))
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    result = codex.doctor_codex(project)

    assert result["daemon"]["healthy"] is True
    assert result["restart_required"] is True
    assert "restart" not in [call[0] for call in fake.calls]


def test_doctor_health_failure_never_exposes_token(
    codex_project, fake_daemon
):
    project, _ = codex_project
    fake_daemon.health_error = RuntimeError(SECRET)

    result = codex.doctor_codex(project)

    assert result["daemon"]["healthy"] is False
    assert result["daemon"]["error_type"] == "daemon_health_failed"
    assert SECRET not in serialized(result)


def test_codex_config_error_has_json_serializable_deterministic_details():
    error = codex.CodexConfigError(
        "example",
        {"message": "actionable", "items": ["b", "a"]},
    )

    assert error.error_type == "example"
    assert error.details == {"items": ["b", "a"], "message": "actionable"}
    assert str(error) == "actionable"
    assert json.loads(json.dumps(error.details)) == error.details


@pytest.mark.parametrize("operation", [codex.setup_codex, codex.repair_codex])
def test_mutating_operation_accepts_ready_source_without_claiming_connection(
    codex_project, fake_daemon, monkeypatch, operation
):
    project, codex_home = codex_project
    write_global_config(codex_home, trusted_global_text(project))
    monkeypatch.setenv(token_env_var(project), SECRET)
    write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )

    result = operation(project)

    assert result["config_changed"] is False
    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["token_env_status"] == "present"
    assert result["credential_source_kind"] == "process_environment"
    assert result["credential_source_status"] == "present"
    assert result["credential_source_ready"] is True
    assert result["token_login_environment_status"] == "not_applicable"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["trust_status"] == "trusted"
    assert result["codex_restart_required"] is True
    assert result["manual_actions"] == []
    assert result["connection_actions"] == ["codex_restart"]
    assert any(
        "new Codex connection" in message
        for message in result["messages"]
    )


def test_unknown_trust_commits_config_but_requires_manual_action(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    monkeypatch.setenv(token_env_var(project), SECRET)

    result = codex.setup_codex(project)

    assert result["config_committed"] is True
    assert result["trust_status"] == "unknown"
    assert result["ok"] is False
    assert result["status"] == "manual_action_required"
    assert result["manual_actions"] == ["project_trust"]
    assert result["connection_actions"] == ["codex_restart"]


def test_explicit_untrusted_project_fails_before_daemon_or_project_write(
    codex_project, fake_daemon
):
    project, codex_home = codex_project
    global_path = write_global_config(
        codex_home,
        trusted_global_text(project, "untrusted"),
    )
    original = global_path.read_bytes()

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "project_explicitly_untrusted"
    assert "untrusted" in str(raised.value)
    assert fake_daemon.calls == []
    assert not (project / ".codex").exists()
    assert global_path.read_bytes() == original


def test_doctor_reports_untrusted_project_as_not_ok(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    write_global_config(
        codex_home,
        trusted_global_text(project, "untrusted"),
    )
    monkeypatch.setenv(token_env_var(project), SECRET)

    result = codex.doctor_codex(project)

    assert result["ok"] is False
    assert result["status"] == "untrusted"
    assert result["trust_status"] == "untrusted"


@pytest.mark.parametrize(
    ("launch_result", "expected"),
    [
        (
            subprocess.CompletedProcess([], 0, stdout=SECRET.encode() + b"\n"),
            "present",
        ),
        (subprocess.CompletedProcess([], 0, stdout=b""), "missing"),
        (subprocess.CompletedProcess([], 0, stdout=b"wrong\n"), "mismatch"),
        (OSError("contains " + SECRET), "unknown"),
    ],
)
def test_doctor_macos_compares_launchctl_value_without_leaking_output(
    codex_project,
    fake_daemon,
    monkeypatch,
    launch_result,
    expected,
):
    project, codex_home = codex_project
    write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    write_global_config(codex_home, trusted_global_text(project))
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")
    monkeypatch.delenv(token_env_var(project), raising=False)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if isinstance(launch_result, BaseException):
            raise launch_result
        return launch_result

    monkeypatch.setattr(codex_runtime.subprocess, "run", run)

    result = codex.doctor_codex(project)

    assert result["token_env_status"] == expected
    assert result["credential_source_kind"] == (
        "launchctl_login_environment" if expected == "present" else "none"
    )
    assert result["credential_source_status"] == expected
    assert result["credential_source_ready"] is (expected == "present")
    assert result["token_process_environment_status"] == "missing"
    assert result["token_login_environment_status"] == expected
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["codex_restart_required"] is True
    assert result["ok"] is (expected == "present")
    assert SECRET not in serialized(result)
    assert calls[0][0] == ["launchctl", "getenv", token_env_var(project)]
    assert calls[0][1]["stderr"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    (
        "process_value",
        "login_value",
        "expected_kind",
        "expected_source_status",
        "expected_process_status",
        "expected_login_status",
        "expected_ready",
    ),
    [
        (None, None, "none", "missing", "missing", "missing", False),
        (None, "wrong", "none", "mismatch", "missing", "mismatch", False),
        (
            None,
            SECRET,
            "launchctl_login_environment",
            "present",
            "missing",
            "present",
            True,
        ),
        ("wrong", None, "none", "mismatch", "mismatch", "missing", False),
        ("wrong", "wrong", "none", "mismatch", "mismatch", "mismatch", False),
        (
            "wrong",
            SECRET,
            "launchctl_login_environment",
            "present",
            "mismatch",
            "present",
            True,
        ),
        (
            SECRET,
            None,
            "process_environment",
            "present",
            "present",
            "missing",
            True,
        ),
        (
            SECRET,
            "wrong",
            "process_environment",
            "present",
            "present",
            "mismatch",
            True,
        ),
        (
            SECRET,
            SECRET,
            "process_environment",
            "present",
            "present",
            "present",
            True,
        ),
    ],
)
def test_doctor_macos_credential_source_matrix(
    codex_project,
    fake_daemon,
    monkeypatch,
    process_value,
    login_value,
    expected_kind,
    expected_source_status,
    expected_process_status,
    expected_login_status,
    expected_ready,
):
    project, codex_home = codex_project
    write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    write_global_config(codex_home, trusted_global_text(project))
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")
    if process_value is None:
        monkeypatch.delenv(token_env_var(project), raising=False)
    else:
        monkeypatch.setenv(token_env_var(project), process_value)

    def run(args, **kwargs):
        value = b"" if login_value is None else login_value.encode() + b"\n"
        return subprocess.CompletedProcess(args, 0, stdout=value)

    monkeypatch.setattr(codex_runtime.subprocess, "run", run)

    result = codex.doctor_codex(project)

    assert result["credential_source_kind"] == expected_kind
    assert result["credential_source_status"] == expected_source_status
    assert result["credential_source_ready"] is expected_ready
    assert (
        result["token_process_environment_status"]
        == expected_process_status
    )
    assert result["token_login_environment_status"] == expected_login_status
    assert result["ok"] is expected_ready
    assert result["connection_verified"] is False
    assert result["codex_restart_required"] is True
    assert SECRET not in serialized(result)


def test_doctor_non_macos_does_not_use_cli_environment_as_host_proof(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    write_global_config(codex_home, trusted_global_text(project))
    monkeypatch.setenv(token_env_var(project), "wrong")

    result = codex.doctor_codex(project)

    assert result["token_env_status"] == "mismatch"
    assert result["credential_source_kind"] == "process_environment"
    assert result["credential_source_status"] == "mismatch"
    assert result["credential_source_ready"] is False
    assert result["token_login_environment_status"] == "not_applicable"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["token_env_present"] is False
    assert result["ok"] is False
    assert result["status"] == "manual_action_required"
    assert result["codex_restart_required"] is True


def test_setup_rejects_starting_but_repair_restarts_it(
    codex_project, monkeypatch
):
    project, _ = codex_project
    starting = daemon_state(project, status="starting", running=False)
    setup_fake = FakeDaemon(project, status=starting)
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: setup_fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "daemon_starting"
    assert "restart" not in [call[0] for call in setup_fake.calls]

    repair_fake = FakeDaemon(project, status=starting)
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: repair_fake)

    result = codex.repair_codex(project)

    assert result["daemon"]["action"] == "restarted"
    assert [call[0] for call in repair_fake.calls].count("restart") == 1


def test_setup_old_daemon_version_fails_with_repair_message(
    codex_project, monkeypatch
):
    project, _ = codex_project
    fake = FakeDaemon(project, status=daemon_state(project, version="0.0.0"))
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "daemon_version_mismatch"
    assert raised.value.details["action"] == "repair"
    assert "repair" in str(raised.value).lower()
    assert not (project / ".codex").exists()


def test_repair_fails_if_restarted_daemon_still_has_old_version(
    codex_project, monkeypatch
):
    project, _ = codex_project
    fake = FakeDaemon(project, status=daemon_state(project, version="0.0.0"))

    def restart(**kwargs):
        fake.calls.append(("restart", kwargs))
        fake.state = daemon_state(project, version="0.0.0")
        return dict(fake.state)

    fake.restart_daemon = restart
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.repair_codex(project)

    assert raised.value.error_type == "daemon_version_mismatch"
    assert [call[0] for call in fake.calls].count("restart") == 1


def test_launchctl_is_not_called_when_project_write_fails(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    write_project_config(
        project,
        "[mcp_servers.nomad]\ncommand = \"nomad\"\n",
    )
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        codex_config.os,
        "replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError()),
    )
    launch_calls = []
    monkeypatch.setattr(
        codex_runtime.subprocess,
        "run",
        lambda *args, **kwargs: launch_calls.append((args, kwargs)),
    )

    with pytest.raises(codex.CodexConfigError):
        codex.setup_codex(project)

    assert launch_calls == []


def test_project_transaction_lock_serializes_real_processes(
    codex_project,
):
    project, _ = codex_project
    lock_dir = project.parent / "process-locks"
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_locked_config_worker,
            args=(str(project), str(lock_dir), name, gate, results),
        )
        for name in ("first", "second")
    ]
    for process in processes:
        process.start()
    gate.set()
    for process in processes:
        process.join(timeout=10)

    assert [process.exitcode for process in processes] == [0, 0]
    assert [results.get(timeout=2), results.get(timeout=2)] == [None, None]
    parsed = tomlkit.parse(
        (project / ".codex" / "config.toml").read_text(encoding="utf-8")
    )
    assert set(parsed["mcp_servers"]) == {"first", "second"}
    lock_path = (
        lock_dir
        / f"{codex_runtime._project_hash(project.resolve())}.codex.lock"
    )
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_transaction_lock_rejects_symlink_lock_file(
    codex_project, fake_daemon
):
    project, _ = codex_project
    fake_daemon.DEFAULT_DAEMONS_DIR.mkdir()
    lock_path = (
        fake_daemon.DEFAULT_DAEMONS_DIR
        / f"{codex_runtime._project_hash(project.resolve())}.codex.lock"
    )
    target = fake_daemon.DEFAULT_DAEMONS_DIR / "outside-lock"
    target.write_text("", encoding="utf-8")
    lock_path.symlink_to(target)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "unsafe_lock_path"
    assert fake_daemon.calls == []


def test_transaction_lock_rejects_nonowned_directory_without_chmod(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    fake_daemon.DEFAULT_DAEMONS_DIR.mkdir()
    actual_uid = os.getuid()
    chmod_calls = []
    monkeypatch.setattr(codex_runtime.os, "getuid", lambda: actual_uid + 1)
    monkeypatch.setattr(
        codex_runtime.os,
        "fchmod",
        lambda *args: chmod_calls.append(args),
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "unsafe_lock_path"
    assert chmod_calls == []
    assert fake_daemon.calls == []


def test_transaction_lock_rejects_group_writable_directory(
    codex_project, fake_daemon
):
    project, _ = codex_project
    fake_daemon.DEFAULT_DAEMONS_DIR.mkdir(mode=0o770)
    fake_daemon.DEFAULT_DAEMONS_DIR.chmod(0o770)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "unsafe_lock_path"
    assert fake_daemon.calls == []


def test_transaction_lock_rejects_nonowned_lock_without_chmod(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    fake_daemon.DEFAULT_DAEMONS_DIR.mkdir()
    actual_uid = os.getuid()
    uid_checks = 0
    chmod_calls = []

    def staged_uid():
        nonlocal uid_checks
        uid_checks += 1
        return actual_uid if uid_checks <= 2 else actual_uid + 1

    monkeypatch.setattr(codex_runtime.os, "getuid", staged_uid)
    monkeypatch.setattr(
        codex_runtime.os,
        "fchmod",
        lambda *args: chmod_calls.append(args),
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "unsafe_lock_path"
    assert chmod_calls == []
    assert fake_daemon.calls == []


def test_transaction_lock_rejects_hardlinked_lock_without_chmod(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    fake_daemon.DEFAULT_DAEMONS_DIR.mkdir()
    lock_path = (
        fake_daemon.DEFAULT_DAEMONS_DIR
        / f"{codex_runtime._project_hash(project.resolve())}.codex.lock"
    )
    external = fake_daemon.DEFAULT_DAEMONS_DIR / "external-lock"
    external.write_text("", encoding="utf-8")
    os.link(external, lock_path)
    chmod_calls = []
    monkeypatch.setattr(
        codex_runtime.os,
        "fchmod",
        lambda *args: chmod_calls.append(args),
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "unsafe_lock_path"
    assert lock_path.stat().st_nlink == 2
    assert chmod_calls == []
    assert fake_daemon.calls == []


def test_rollback_skips_stop_when_started_instance_changed(
    codex_project, monkeypatch
):
    project, _ = codex_project
    path = write_project_config(
        project,
        "[mcp_servers.nomad]\ncommand = \"nomad\"\n",
    )
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)
    original_assert = codex_config._assert_global_snapshot_current

    def conflict(snapshot):
        original_assert(snapshot)
        path.write_text("# changed\n", encoding="utf-8")
        fake.state["instance_id"] = "replacement-instance"

    monkeypatch.setattr(
        codex_config,
        "_assert_global_snapshot_current",
        conflict,
    )

    with pytest.raises(codex.CodexConfigError):
        codex.setup_codex(project)

    assert "stop" not in [call[0] for call in fake.calls]


def test_directory_fsync_failure_after_replace_is_committed_and_not_rolled_back(
    codex_project, monkeypatch
):
    project, _ = codex_project
    path = write_project_config(
        project,
        "[mcp_servers.nomad]\ncommand = \"nomad\"\n",
    )
    fake = FakeDaemon(
        project,
        status={
            "status": "stopped",
            "running": False,
            "project_root": str(project),
        },
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)
    real_fsync = codex_config.os.fsync

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(codex_config.os, "fsync", fsync)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "config_durability_uncertain"
    assert raised.value.details["config_committed"] is True
    assert "url =" in path.read_text(encoding="utf-8")
    assert "stop" not in [call[0] for call in fake.calls]


def test_project_root_replacement_before_write_cannot_touch_external_directory(
    codex_project, fake_daemon, monkeypatch
):
    project, _ = codex_project
    path = write_project_config(
        project,
        "[mcp_servers.nomad]\ncommand = \"nomad\"\n",
    )
    project_original = path.read_bytes()
    original_parent = project.with_name("original-project")
    external = project.with_name("external-project")
    external.mkdir()
    external_path = write_project_config(external, "# external\n")
    external_original = external_path.read_bytes()
    original_assert = codex_config._assert_global_snapshot_current

    def replace_root(snapshot):
        original_assert(snapshot)
        project.rename(original_parent)
        project.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(
        codex_config,
        "_assert_global_snapshot_current",
        replace_root,
    )
    try:
        with pytest.raises(codex.CodexConfigError) as raised:
            codex.setup_codex(project)

        assert raised.value.error_type == "unsafe_project_path"
        assert external_path.read_bytes() == external_original
        assert (
            original_parent / ".codex" / "config.toml"
        ).read_bytes() == project_original
    finally:
        if project.is_symlink():
            project.unlink()
        if original_parent.exists():
            original_parent.rename(project)


def test_global_symlink_is_allowed_for_read_only_diagnostics_and_setup(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    target = codex_home.parent / "actual-global.toml"
    target.write_text(trusted_global_text(project), encoding="utf-8")
    (codex_home / "config.toml").symlink_to(target)
    original = target.read_bytes()
    monkeypatch.setenv(token_env_var(project), SECRET)

    result = codex.setup_codex(project)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["config_changed"] is True
    assert result["codex_restart_required"] is True
    assert target.read_bytes() == original
    assert (codex_home / "config.toml").is_symlink()


def test_macos_matching_login_environment_still_restarts_after_config_change(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    write_global_config(codex_home, trusted_global_text(project))
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=SECRET.encode() + b"\n",
        )

    monkeypatch.setattr(codex_runtime.subprocess, "run", run)

    result = codex.setup_codex(project)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["config_changed"] is True
    assert result["token_env_action"] == "new_connection_required"
    assert result["token_env_status"] == "present"
    assert result["credential_source_ready"] is True
    assert result["token_login_environment_status"] == "present"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["codex_restart_required"] is True
    assert calls == [["launchctl", "getenv", token_env_var(project)]]


def test_macos_unchanged_config_and_matching_login_environment_still_restarts(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    write_global_config(codex_home, trusted_global_text(project))
    write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")

    def run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=SECRET.encode() + b"\n",
        )

    monkeypatch.setattr(codex_runtime.subprocess, "run", run)

    result = codex.setup_codex(project)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["config_changed"] is False
    assert result["token_env_action"] == "new_connection_required"
    assert result["token_env_status"] == "present"
    assert result["credential_source_ready"] is True
    assert result["token_login_environment_status"] == "present"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["codex_restart_required"] is True


def test_macos_current_process_source_is_ready_without_claiming_connection(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    write_global_config(codex_home, trusted_global_text(project))
    write_project_config(
        project,
        f"""[mcp_servers.nomad]
url = "{fake_daemon.state["url"]}"
bearer_token_env_var = "{token_env_var(project)}"
""",
    )
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")
    monkeypatch.setenv(token_env_var(project), SECRET)

    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=b"",
        )

    monkeypatch.setattr(codex_runtime.subprocess, "run", run)

    result = codex.setup_codex(project)

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["token_env_status"] == "present"
    assert result["credential_source_kind"] == "process_environment"
    assert result["credential_source_ready"] is True
    assert result["token_process_environment_status"] == "present"
    assert result["token_login_environment_status"] == "missing"
    assert result["token_host_environment_status"] == "unverified"
    assert result["connection_verified"] is False
    assert result["token_env_action"] == "new_connection_required"
    assert result["codex_restart_required"] is True
    assert calls == [["launchctl", "getenv", token_env_var(project)]]


def test_postcommit_global_conflict_is_partial_and_skips_token_environment(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    original_read = codex_config._read_global_config
    reads = 0
    token_calls_before = 0

    def read_with_postcommit_conflict(path):
        nonlocal reads
        reads += 1
        if reads == 3:
            write_global_config(
                codex_home,
                "[mcp_servers.nomad]\ncommand = \"other\"\n",
            )
        return original_read(path)

    monkeypatch.setattr(
        codex_config,
        "_read_global_config",
        read_with_postcommit_conflict,
    )
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")

    def forbidden_launchctl(*args, **kwargs):
        raise AssertionError("token environment must not be touched")

    monkeypatch.setattr(
        codex_runtime.subprocess,
        "run",
        forbidden_launchctl,
    )
    token_calls_before = len(
        [call for call in fake_daemon.calls if call[0] == "token"]
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "global_conflict_after_project_commit"
    assert raised.value.details["config_committed"] is True
    assert raised.value.details["token_env_configured"] is False
    assert (project / ".codex" / "config.toml").exists()
    token_calls_after = len(
        [call for call in fake_daemon.calls if call[0] == "token"]
    )
    assert token_calls_after - token_calls_before == 1


def test_postcommit_malformed_global_is_explicit_partial_result(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    original_read = codex_config._read_global_config
    reads = 0

    def read_with_malformed_postcommit(path):
        nonlocal reads
        reads += 1
        if reads == 3:
            write_global_config(codex_home, "[broken\n")
        return original_read(path)

    monkeypatch.setattr(
        codex_config,
        "_read_global_config",
        read_with_malformed_postcommit,
    )
    launch_calls = []
    monkeypatch.setattr(codex_runtime.sys, "platform", "darwin")
    monkeypatch.setattr(
        codex_runtime.subprocess,
        "run",
        lambda *args, **kwargs: launch_calls.append((args, kwargs)),
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert (
        raised.value.error_type
        == "global_config_invalid_after_project_commit"
    )
    assert raised.value.details["config_committed"] is True
    assert raised.value.details["token_env_configured"] is False
    assert launch_calls == []


def test_precommit_global_change_aborts_before_project_commit(
    codex_project, fake_daemon, monkeypatch
):
    project, codex_home = codex_project
    global_path = write_global_config(codex_home, "# initial\n")
    original_assert = codex_config._assert_global_snapshot_current

    def change_then_check(snapshot):
        global_path.write_text("# changed\n", encoding="utf-8")
        original_assert(snapshot)

    monkeypatch.setattr(
        codex_config,
        "_assert_global_snapshot_current",
        change_then_check,
    )

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "concurrent_global_config_change"
    assert not (project / ".codex").exists()


def test_daemon_state_rejects_allow_remote_true(
    codex_project, monkeypatch
):
    project, _ = codex_project
    fake = FakeDaemon(
        project,
        status=daemon_state(project, allow_remote=True),
    )
    monkeypatch.setattr(codex_runtime, "_daemon_module", lambda: fake)

    with pytest.raises(codex.CodexConfigError) as raised:
        codex.setup_codex(project)

    assert raised.value.error_type == "invalid_daemon_state"
