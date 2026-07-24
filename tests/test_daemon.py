import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from nomad import __version__
from nomad import daemon


class FakeProcess:
    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.waited = True
        self.returncode = -signal.SIGTERM
        return self.returncode


@pytest.fixture
def daemon_home(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".nomad" / "daemons"
    monkeypatch.setattr(daemon, "DEFAULT_DAEMONS_DIR", path)
    return path


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def _state(project: Path, *, pid=4242, instance_id="instance-1"):
    paths = daemon._project_paths(project.resolve())
    payload = {
        "schema_version": daemon.SCHEMA_VERSION,
        "pid": pid,
        "project_root": str(project.resolve()),
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/mcp",
        "url": "http://127.0.0.1:8765/mcp",
        "version": __version__,
        "started_at": "2026-07-24T00:00:00+00:00",
        "instance_id": instance_id,
        "log_path": str(paths["log"]),
    }
    daemon._write_state(paths["state"], payload)
    return paths, payload


def _mock_successful_start(monkeypatch, process=None):
    process = process or FakeProcess()
    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return process

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: False)
    monkeypatch.setattr(daemon, "_wait_until_ready", lambda *args: None)
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda pid, instance: True)
    return process, popen_calls


def test_start_writes_secure_project_state_and_log(
    daemon_home, project, monkeypatch
):
    process, popen_calls = _mock_successful_start(monkeypatch)

    result = daemon.start_daemon(project=project)

    paths = daemon._project_paths(project.resolve())
    state_payload = json.loads(paths["state"].read_text())
    assert result["status"] == "running"
    assert result["already_running"] is False
    expected_port = daemon.project_default_port(project)
    assert state_payload == {
        "schema_version": 1,
        "pid": process.pid,
        "project_root": str(project.resolve()),
        "host": "127.0.0.1",
        "port": expected_port,
        "path": "/mcp",
        "url": f"http://127.0.0.1:{expected_port}/mcp",
        "version": __version__,
        "started_at": state_payload["started_at"],
        "ready_at": state_payload["ready_at"],
        "instance_id": state_payload["instance_id"],
        "log_path": str(paths["log"]),
        "allow_remote": False,
        "auth": True,
        "token_env_var": daemon._project_token_env_var(project.resolve()),
        "lifecycle": "running",
    }
    assert stat.S_IMODE(daemon_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths["state"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["log"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["lock"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["token"].stat().st_mode) == 0o600

    command, kwargs = popen_calls[0]
    assert command[:4] == [sys.executable, "-m", "nomad.cli", "serve"]
    assert command[-2:] == ["--daemon-id", state_payload["instance_id"]]
    assert kwargs["cwd"] == project.resolve()
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["NOMAD_MCP_LOG_PATH"] == str(paths["log"])
    token = paths["token"].read_text(encoding="ascii").strip()
    assert kwargs["env"][daemon.BEARER_TOKEN_ENV_VAR] == token
    assert token not in json.dumps(state_payload)
    assert token not in json.dumps(result)
    assert token not in " ".join(command)
    assert token not in paths["log"].read_text(encoding="utf-8")
    assert kwargs["stdout"] is kwargs["stderr"]


def test_repeated_start_is_idempotent(daemon_home, project, monkeypatch):
    _, popen_calls = _mock_successful_start(monkeypatch)
    first = daemon.start_daemon(project=project)
    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: True)

    second = daemon.start_daemon(project=project, port=9999)

    assert second["pid"] == first["pid"]
    assert second["url"] == first["url"]
    assert second["already_running"] is True
    assert len(popen_calls) == 1


def test_starting_state_exists_while_readiness_is_running(
    daemon_home, project, monkeypatch
):
    process = FakeProcess()
    observed = {}
    monkeypatch.setattr(
        daemon.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: False)
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda *args: True)

    def observe_state(*args):
        paths = daemon._project_paths(project.resolve())
        observed.update(json.loads(paths["state"].read_text(encoding="utf-8")))

    monkeypatch.setattr(daemon, "_wait_until_ready", observe_state)

    result = daemon.start_daemon(project=project)

    assert observed["lifecycle"] == "starting"
    assert observed["pid"] == process.pid
    assert observed["instance_id"] == result["instance_id"]
    assert "token" not in observed
    assert result["status"] == "running"
    assert result["lifecycle"] == "running"


def test_status_and_idempotent_start_preserve_starting_lifecycle(
    daemon_home, project, monkeypatch
):
    paths, payload = _state(project)
    payload["lifecycle"] = "starting"
    daemon._write_state(paths["state"], payload)
    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda *args: True)

    status = daemon.status_daemon(project=project)
    started = daemon.start_daemon(project=project)

    assert status["status"] == "starting"
    assert status["running"] is False
    assert started["status"] == "starting"
    assert started["running"] is False
    assert started["already_running"] is True


def test_old_state_without_lifecycle_is_reported_running(
    daemon_home, project, monkeypatch
):
    _state(project)
    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda *args: True)

    result = daemon.status_daemon(project=project)

    assert result["status"] == "running"
    assert result["running"] is True
    assert result["lifecycle"] == "running"


def test_status_removes_dead_stale_state(daemon_home, project, monkeypatch):
    paths, _ = _state(project)
    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: False)

    result = daemon.status_daemon(project=project)

    assert result["status"] == "stopped"
    assert result["stale_state_removed"] is True
    assert not paths["state"].exists()


def test_failed_start_returns_error_and_leaves_no_state(
    daemon_home, project, monkeypatch
):
    process = FakeProcess()
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: False)
    monkeypatch.setattr(
        daemon,
        "_wait_until_ready",
        lambda *args: (_ for _ in ()).throw(daemon.DaemonError("not ready")),
    )

    with pytest.raises(daemon.DaemonError, match="not ready"):
        daemon.start_daemon(project=project)

    paths = daemon._project_paths(project.resolve())
    assert process.terminated is True
    assert process.waited is True
    assert not paths["state"].exists()
    assert paths["token"].exists()


def test_keyboard_interrupt_terminates_child_and_removes_starting_state(
    daemon_home, project, monkeypatch
):
    process = FakeProcess()
    monkeypatch.setattr(
        daemon.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: False)
    monkeypatch.setattr(
        daemon,
        "_wait_until_ready",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        daemon.start_daemon(project=project)

    paths = daemon._project_paths(project.resolve())
    assert process.terminated is True
    assert process.waited is True
    assert not paths["state"].exists()
    assert paths["token"].exists()


@pytest.mark.parametrize("fail_on_write", [1, 2])
def test_state_write_failure_terminates_child_and_removes_state(
    daemon_home, project, monkeypatch, fail_on_write
):
    process, _ = _mock_successful_start(monkeypatch)
    original_write_state = daemon._write_state
    write_count = 0

    def failing_write(path, state):
        nonlocal write_count
        write_count += 1
        if write_count == fail_on_write:
            raise OSError("state write failed")
        original_write_state(path, state)

    monkeypatch.setattr(daemon, "_write_state", failing_write)

    with pytest.raises(OSError, match="state write failed"):
        daemon.start_daemon(project=project)

    paths = daemon._project_paths(project.resolve())
    assert process.terminated is True
    assert not paths["state"].exists()
    assert paths["token"].exists()


def test_token_is_reused_across_failed_start_and_retry(
    daemon_home, project, monkeypatch
):
    first_process = FakeProcess()
    second_process = FakeProcess(pid=4343)
    processes = iter([first_process, second_process])
    monkeypatch.setattr(
        daemon.subprocess,
        "Popen",
        lambda *args, **kwargs: next(processes),
    )
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: False)
    readiness = iter([daemon.DaemonError("not ready"), None])

    def wait(*args):
        outcome = next(readiness)
        if outcome is not None:
            raise outcome

    monkeypatch.setattr(daemon, "_wait_until_ready", wait)
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda *args: True)

    with pytest.raises(daemon.DaemonError, match="not ready"):
        daemon.start_daemon(project=project)
    paths = daemon._project_paths(project.resolve())
    first_token = paths["token"].read_text(encoding="ascii")

    daemon.start_daemon(project=project)

    assert paths["token"].read_text(encoding="ascii") == first_token


def test_token_is_reused_across_stop_start_and_restart(
    daemon_home, project, monkeypatch
):
    _, popen_calls = _mock_successful_start(monkeypatch)
    daemon.start_daemon(project=project)
    paths = daemon._project_paths(project.resolve())
    original_token = paths["token"].read_text(encoding="ascii")
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: None)

    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: next(iter_alive))
    iter_alive = iter([True, False])
    daemon.stop_daemon(project=project)
    daemon.start_daemon(project=project)

    iter_alive = iter([True, False])
    daemon.restart_daemon(project=project)

    assert paths["token"].read_text(encoding="ascii") == original_token
    assert len(popen_calls) == 3


def test_failed_start_cleanup_kills_after_terminate_wait_timeout():
    class TimeoutProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.wait_timeouts = []
            self.killed = False

        def wait(self, timeout=None):
            self.wait_timeouts.append(timeout)
            if len(self.wait_timeouts) == 1:
                raise subprocess.TimeoutExpired("nomad", timeout)
            self.returncode = -signal.SIGKILL
            return self.returncode

        def kill(self):
            self.killed = True

    process = TimeoutProcess()

    daemon._terminate_failed_start(process)

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_timeouts == [2, 2]


def test_stop_refuses_pid_owned_by_another_process(
    daemon_home, project, monkeypatch
):
    paths, _ = _state(project)
    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda pid, instance: False)
    killed = []
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(daemon.DaemonOwnershipError, match="refusing to stop"):
        daemon.stop_daemon(project=project)

    assert killed == []
    assert paths["state"].exists()


def test_stop_sends_sigterm_waits_and_removes_state(
    daemon_home, project, monkeypatch
):
    paths, payload = _state(project)
    alive = iter([True, False])
    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: next(alive))
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda pid, instance: True)
    killed = []
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    result = daemon.stop_daemon(project=project)

    assert result["status"] == "stopped"
    assert result["pid"] == payload["pid"]
    assert killed == [(payload["pid"], signal.SIGTERM)]
    assert not paths["state"].exists()


def test_projects_have_isolated_state_and_log_paths(
    daemon_home, tmp_path
):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_paths = daemon._project_paths(first.resolve())
    second_paths = daemon._project_paths(second.resolve())

    assert first_paths["state"] != second_paths["state"]
    assert first_paths["log"] != second_paths["log"]
    assert first_paths["token"] != second_paths["token"]
    assert first_paths["state"].parent == second_paths["state"].parent == daemon_home


def test_default_project_ports_are_stable_and_isolated(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    first_port = daemon.project_default_port(first)
    second_port = daemon.project_default_port(second)

    assert first_port == daemon.project_default_port(first.resolve())
    assert daemon.PROJECT_PORT_MIN <= first_port <= daemon.PROJECT_PORT_MAX
    assert daemon.PROJECT_PORT_MIN <= second_port <= daemon.PROJECT_PORT_MAX
    assert first_port != second_port


def test_non_loopback_and_legacy_allow_remote_are_rejected(daemon_home, project):
    with pytest.raises(daemon.DaemonError, match="restricted to loopback"):
        daemon.start_daemon(project=project, host="0.0.0.0")

    with pytest.raises(daemon.DaemonError, match="not supported"):
        daemon.start_daemon(project=project, allow_remote=True)


def test_restart_legacy_remote_state_falls_back_to_loopback(
    daemon_home, project, monkeypatch
):
    paths, payload = _state(project)
    payload.update(
        {
            "host": "0.0.0.0",
            "url": "http://0.0.0.0:8765/mcp",
            "allow_remote": True,
        }
    )
    daemon._write_state(paths["state"], payload)
    starts = []
    monkeypatch.setattr(daemon, "_stop_locked", lambda **kwargs: None)
    monkeypatch.setattr(
        daemon,
        "_start_locked",
        lambda **kwargs: starts.append(kwargs) or {"status": "running"},
    )

    result = daemon.restart_daemon(project=project)

    assert result["restarted"] is True
    assert starts[0]["host"] == daemon.DEFAULT_HOST
    assert "allow_remote" not in starts[0]


def test_start_rejects_an_address_already_in_use(
    daemon_home, project, monkeypatch
):
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: True)

    with pytest.raises(daemon.DaemonError, match="already in use"):
        daemon.start_daemon(project=project)


def test_process_ownership_requires_matching_hidden_id(monkeypatch):
    monkeypatch.setattr(
        daemon,
        "_process_command",
        lambda pid: (
            "/usr/bin/python3 -m nomad.cli serve --host 127.0.0.1 "
            "--daemon-id expected"
        ),
    )

    assert daemon._process_owns_instance(10, "expected") is True
    assert daemon._process_owns_instance(10, "other") is False


def test_pid_alive_treats_zombie_as_stopped(monkeypatch):
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon, "_process_state", lambda pid: "Z+")

    assert daemon._pid_is_alive(10) is False


def test_wait_until_ready_requires_authenticated_health_with_matching_pid(
    monkeypatch
):
    process = FakeProcess(pid=4242)
    calls = []
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: True)

    def health_data(url, token, *, timeout):
        calls.append((url, token, timeout))
        return {"pid": process.pid, "cwd": "/tmp/project"}

    monkeypatch.setattr(daemon, "_mcp_health_data", health_data)

    daemon._wait_until_ready(
        process,
        "127.0.0.1",
        54321,
        "/mcp",
        "private-token",
        timeout=0.5,
    )

    assert calls
    assert calls[0][0] == "http://127.0.0.1:54321/mcp"
    assert calls[0][1] == "private-token"


@pytest.mark.parametrize(
    ("health_behavior", "expected_error"),
    [
        (lambda token: {"pid": 9999}, "health_pid_mismatch"),
        (
            lambda token: (_ for _ in ()).throw(
                RuntimeError(f"foreign service rejected {token}")
            ),
            "RuntimeError",
        ),
    ],
)
def test_wait_until_ready_rejects_wrong_service_without_leaking_token(
    monkeypatch, health_behavior, expected_error
):
    process = FakeProcess(pid=4242)
    token = "must-never-appear"
    monkeypatch.setattr(daemon, "_can_connect", lambda host, port: True)
    monkeypatch.setattr(
        daemon,
        "_mcp_health_data",
        lambda url, bearer_token, *, timeout: health_behavior(bearer_token),
    )

    with pytest.raises(daemon.DaemonError) as exc_info:
        daemon._wait_until_ready(
            process,
            "127.0.0.1",
            54321,
            "/mcp",
            token,
            timeout=0.01,
        )

    assert expected_error in str(exc_info.value)
    assert token not in str(exc_info.value)


def test_status_uses_public_allowlist_and_never_leaks_token_fields(
    daemon_home, project, monkeypatch
):
    paths, payload = _state(project)
    payload.update(
        {
            "auth": True,
            "token_env_var": daemon._project_token_env_var(project.resolve()),
            "token": "secret",
            "token_path": "/secret/path",
        }
    )
    daemon._write_state(paths["state"], payload)
    monkeypatch.setattr(daemon, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(daemon, "_process_owns_instance", lambda *args: True)

    result = daemon.status_daemon(project=project)

    assert result["auth"] is True
    assert result["token_env_var"].startswith("NOMAD_MCP_BEARER_TOKEN_")
    assert "token" not in result
    assert "token_path" not in result
    assert "secret" not in json.dumps(result)


def test_read_daemon_token_returns_only_existing_secret(
    daemon_home, project
):
    paths = daemon._project_paths(project.resolve())
    paths["token"].write_text("project-secret\n", encoding="ascii")
    paths["token"].chmod(0o644)

    result = daemon.read_daemon_token(project=project)

    assert result == "project-secret"
    assert stat.S_IMODE(paths["token"].stat().st_mode) == 0o600


def test_read_daemon_token_requires_initialized_token(
    daemon_home, project
):
    with pytest.raises(daemon.DaemonError, match="not initialized"):
        daemon.read_daemon_token(project=project)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pid", 0),
        ("pid", -1),
        ("port", 0),
        ("port", 65536),
        ("path", ""),
        ("path", "   "),
        ("instance_id", ""),
        ("instance_id", "   "),
    ],
)
def test_valid_state_rejects_invalid_semantic_values(
    daemon_home, project, field, value
):
    _, payload = _state(project)
    payload[field] = value

    assert daemon._valid_state(payload, project.resolve()) is False
