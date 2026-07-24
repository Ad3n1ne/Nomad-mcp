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
    monkeypatch.setattr(daemon, "_wait_until_ready", lambda proc, host, port: None)
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
    assert state_payload == {
        "schema_version": 1,
        "pid": process.pid,
        "project_root": str(project.resolve()),
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/mcp",
        "url": "http://127.0.0.1:8765/mcp",
        "version": __version__,
        "started_at": state_payload["started_at"],
        "instance_id": state_payload["instance_id"],
        "log_path": str(paths["log"]),
    }
    assert stat.S_IMODE(daemon_home.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths["state"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["log"].stat().st_mode) == 0o600
    assert stat.S_IMODE(paths["lock"].stat().st_mode) == 0o600

    command, kwargs = popen_calls[0]
    assert command[:4] == [sys.executable, "-m", "nomad.cli", "serve"]
    assert command[-2:] == ["--daemon-id", state_payload["instance_id"]]
    assert kwargs["cwd"] == project.resolve()
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["NOMAD_MCP_LOG_PATH"] == str(paths["log"])
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
    assert first_paths["state"].parent == second_paths["state"].parent == daemon_home


def test_non_loopback_requires_explicit_allow_remote(
    daemon_home, project, monkeypatch, capsys
):
    with pytest.raises(daemon.DaemonError, match="--allow-remote"):
        daemon.start_daemon(project=project, host="0.0.0.0")

    _mock_successful_start(monkeypatch)
    result = daemon.start_daemon(
        project=project,
        host="0.0.0.0",
        allow_remote=True,
    )

    assert result["host"] == "0.0.0.0"
    assert "warning:" in capsys.readouterr().err


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
