import json
import subprocess

import pytest

from nomad import __version__
from nomad import daemon
from nomad.cli import main


def test_cli_version(capsys):
    assert main(["--version"]) == 0

    out = capsys.readouterr().out.strip()
    assert out == __version__


def test_cli_schema(capsys):
    assert main(["schema"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert "minimal_remote_template" in payload
    assert payload["minimal_remote_template"]["project_name"] == "my_project"


def test_cli_client_config_uvx_json(capsys):
    assert main(["client-config"]) == 0

    payload = json.loads(capsys.readouterr().out)
    config = payload["mcpServers"]["nomad"]
    assert config == {"command": "uvx", "args": ["nomad-mcp"]}


def test_cli_client_config_github_json(capsys):
    assert main(["client-config", "--runner", "github"]) == 0

    payload = json.loads(capsys.readouterr().out)
    config = payload["mcpServers"]["nomad"]
    assert config["command"] == "uvx"
    assert config["args"] == [
        "--from",
        f"git+https://github.com/Ad3n1ne/Nomad-mcp.git@v{__version__}",
        "nomad",
    ]


def test_cli_client_config_installed_toml(capsys):
    assert main(["client-config", "--runner", "nomad", "--format", "toml"]) == 0

    out = capsys.readouterr().out
    assert 'command = "nomad"' in out
    assert "args = []" in out


def test_cli_serve_uses_streamable_http_defaults(monkeypatch):
    calls = []
    monkeypatch.delenv("NOMAD_MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(
        "nomad.server.main",
        lambda **kwargs: calls.append(kwargs),
    )

    assert main(["serve"]) is None

    assert calls == [
        {
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": 8765,
            "path": "/mcp",
            "bearer_token": None,
        }
    ]


def test_cli_serve_passes_explicit_http_options(monkeypatch):
    calls = []
    monkeypatch.setenv("NOMAD_MCP_BEARER_TOKEN", "environment-secret")
    monkeypatch.setattr(
        "nomad.server.main",
        lambda **kwargs: calls.append(kwargs),
    )

    assert main(
        ["serve", "--host", "localhost", "--port", "9999", "--path", "/nomad"]
    ) is None

    assert calls == [
        {
            "transport": "streamable-http",
            "host": "localhost",
            "port": 9999,
            "path": "/nomad",
            "bearer_token": "environment-secret",
        }
    ]


def test_cli_serve_remote_requires_explicit_allow_remote(monkeypatch):
    monkeypatch.setenv("NOMAD_MCP_BEARER_TOKEN", "environment-secret")

    with pytest.raises(SystemExit):
        main(["serve", "--host", "0.0.0.0"])


def test_cli_serve_remote_requires_env_token_when_explicitly_allowed(monkeypatch):
    monkeypatch.delenv("NOMAD_MCP_BEARER_TOKEN", raising=False)

    with pytest.raises(SystemExit):
        main(["serve", "--host", "0.0.0.0", "--allow-remote"])


def test_cli_serve_remote_passes_env_token_when_explicitly_allowed(monkeypatch):
    calls = []
    monkeypatch.setenv("NOMAD_MCP_BEARER_TOKEN", "environment-secret")
    monkeypatch.setattr(
        "nomad.server.main",
        lambda **kwargs: calls.append(kwargs),
    )

    assert main(["serve", "--host", "0.0.0.0", "--allow-remote"]) is None
    assert calls == [
        {
            "transport": "streamable-http",
            "host": "0.0.0.0",
            "port": 8765,
            "path": "/mcp",
            "bearer_token": "environment-secret",
        }
    ]


def test_cli_serve_does_not_accept_token_on_command_line():
    with pytest.raises(SystemExit):
        main(["serve", "--bearer-token", "secret"])


@pytest.mark.parametrize("port", ["0", "65536", "invalid"])
def test_cli_serve_rejects_invalid_port(port):
    with pytest.raises(SystemExit):
        main(["serve", "--port", port])


def test_cli_serve_rejects_path_without_leading_slash():
    with pytest.raises(SystemExit):
        main(["serve", "--path", "mcp"])


def test_cli_daemon_start_dispatches_all_options(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        daemon,
        "start_daemon",
        lambda **kwargs: calls.append(kwargs) or {"status": "running"},
    )

    assert main(
        [
            "daemon",
            "start",
            "--project",
            "/tmp/project",
            "--host",
            "localhost",
            "--port",
            "9876",
            "--path",
            "/nomad",
            "--allow-remote",
        ]
    ) == 0

    assert calls == [
        {
            "project": "/tmp/project",
            "host": "localhost",
            "port": 9876,
            "path": "/nomad",
            "allow_remote": True,
        }
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "running"


@pytest.mark.parametrize(
    ("command", "function_name"),
    [
        ("status", "status_daemon"),
        ("restart", "restart_daemon"),
        ("stop", "stop_daemon"),
    ],
)
def test_cli_daemon_project_command_dispatch(monkeypatch, capsys, command, function_name):
    calls = []
    monkeypatch.setattr(
        daemon,
        function_name,
        lambda **kwargs: calls.append(kwargs) or {"status": command},
    )

    assert main(["daemon", command, "--project", "/tmp/project"]) == 0

    assert calls == [{"project": "/tmp/project"}]
    assert json.loads(capsys.readouterr().out)["status"] == command


def test_cli_daemon_error_is_reported_without_traceback(monkeypatch, capsys):
    def fail(**kwargs):
        raise daemon.DaemonError("daemon unavailable")

    monkeypatch.setattr(daemon, "status_daemon", fail)

    assert main(["daemon", "status"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == "error: daemon unavailable"


def test_cli_doctor_success(monkeypatch, capsys):
    monkeypatch.setattr("nomad.cli.shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    assert main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "python>=3.11" in out
    assert "ssh" in out
    assert "rsync" in out


def test_cli_doctor_kill_stale_mcp(monkeypatch, capsys):
    ps_output = "\n".join(
        [
            "100 1 /Applications/ChatGPT.app/Contents/Resources/codex app-server",
            "101 100 /Users/me/miniconda3/bin/python /Users/me/miniconda3/bin/nomad",
            "102 1 /Users/me/miniconda3/bin/python /Users/me/miniconda3/bin/nomad",
            "103 100 /Users/me/miniconda3/bin/python /Users/me/miniconda3/bin/nomad doctor --kill-stale-mcp",
        ]
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=ps_output, stderr="")

    killed = []
    monkeypatch.setattr("nomad.cli.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr("nomad.cli.subprocess.run", fake_run)
    monkeypatch.setattr("nomad.cli.os.getpid", lambda: 999)
    monkeypatch.setattr("nomad.cli.os.kill", lambda pid, sig: killed.append((pid, sig)))

    assert main(["doctor", "--kill-stale-mcp"]) == 0

    out = capsys.readouterr().out
    assert "killed pid=101" in out
    assert "pid=102" not in out
    assert "pid=103" not in out
    assert killed == [(101, 15)]


def test_cli_doctor_dry_run_stale_mcp(monkeypatch, capsys):
    ps_output = "\n".join(
        [
            "200 1 /Applications/ChatGPT.app/Contents/Resources/codex app-server",
            "201 200 /Users/me/miniconda3/bin/python /Users/me/miniconda3/bin/nomad",
        ]
    )

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=ps_output, stderr="")

    killed = []
    monkeypatch.setattr("nomad.cli.shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr("nomad.cli.subprocess.run", fake_run)
    monkeypatch.setattr("nomad.cli.os.kill", lambda pid, sig: killed.append((pid, sig)))

    assert main(["doctor", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would kill pid=201" in out
    assert killed == []
