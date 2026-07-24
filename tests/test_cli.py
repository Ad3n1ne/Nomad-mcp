import json
import subprocess
import tomllib

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
    assert config == {
        "command": "uvx",
        "args": ["--from", "nomad-mcp", "nomad"],
    }


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


def test_cli_client_config_stdio_custom_name_keeps_compatible_output(capsys):
    assert main(["client-config", "--name", "nomad_project-1"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "mcpServers": {
            "nomad_project-1": {
                "command": "uvx",
                "args": ["--from", "nomad-mcp", "nomad"],
            }
        }
    }


def test_cli_client_config_http_json(monkeypatch, capsys):
    monkeypatch.setattr(
        daemon,
        "status_daemon",
        lambda **kwargs: {
            "status": "running",
            "running": True,
            "project_root": "/tmp/project",
            "url": "http://127.0.0.1:54321/mcp",
            "token_env_var": "NOMAD_MCP_BEARER_TOKEN_ABC123",
            "token": "must-not-leak",
            "token_path": "/must/not/leak",
        },
    )

    assert main(
        [
            "client-config",
            "--transport",
            "http",
            "--project",
            "/tmp/project",
            "--name",
            "nomad-project",
        ]
    ) == 0

    output = capsys.readouterr().out
    assert "must-not-leak" not in output
    assert "/must/not/leak" not in output
    assert json.loads(output) == {
        "mcpServers": {
            "nomad-project": {
                "url": "http://127.0.0.1:54321/mcp",
                "bearerTokenEnvVar": "NOMAD_MCP_BEARER_TOKEN_ABC123",
            }
        }
    }


def test_cli_client_config_http_toml(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        daemon,
        "status_daemon",
        lambda **kwargs: calls.append(kwargs)
        or {
            "status": "running",
            "running": True,
            "url": "http://127.0.0.1:54321/mcp",
            "token_env_var": "NOMAD_MCP_BEARER_TOKEN_ABC123",
        },
    )

    assert main(
        [
            "client-config",
            "--transport",
            "http",
            "--project",
            "/tmp/project",
            "--name",
            "nomad_project",
            "--format",
            "toml",
        ]
    ) == 0

    assert calls == [{"project": "/tmp/project"}]
    rendered = capsys.readouterr().out
    assert tomllib.loads(rendered) == {
        "mcp_servers": {
            "nomad_project": {
                "url": "http://127.0.0.1:54321/mcp",
                "bearer_token_env_var": "NOMAD_MCP_BEARER_TOKEN_ABC123",
            }
        }
    }


@pytest.mark.parametrize("name", ["nomad.project", "nomad project", "x/y", ""])
def test_cli_client_config_rejects_invalid_name(name):
    with pytest.raises(SystemExit):
        main(["client-config", "--name", name])


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        ("stopped", "daemon_not_running"),
        ("starting", "daemon_starting"),
        ("ownership_mismatch", "daemon_ownership_mismatch"),
    ],
)
def test_cli_client_config_http_requires_running_daemon(
    monkeypatch, capsys, status, error_type
):
    monkeypatch.setattr(
        daemon,
        "status_daemon",
        lambda **kwargs: {
            "status": status,
            "running": False,
            "project_root": "/tmp/project",
        },
    )

    assert main(["client-config", "--transport", "http"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error == {
        "ok": False,
        "error_type": error_type,
        "message": (
            f"project daemon is {status}; "
            "run 'nomad daemon start --project <project>' first"
        ),
        "status": status,
        "project_root": "/tmp/project",
    }


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


def test_cli_serve_claims_daemon_state_before_starting_server(monkeypatch):
    calls = []
    monkeypatch.setenv("NOMAD_MCP_BEARER_TOKEN", "environment-secret")
    monkeypatch.setattr(
        daemon,
        "claim_daemon_state",
        lambda state_path, instance_id: calls.append(
            ("claim", state_path, instance_id)
        ),
    )
    monkeypatch.setattr(
        "nomad.server.main",
        lambda **kwargs: calls.append(("server", kwargs)),
    )

    assert main(
        [
            "serve",
            "--daemon-id",
            "internal-instance",
            "--daemon-state",
            "/tmp/internal-state.json",
        ]
    ) is None

    assert calls[0] == (
        "claim",
        "/tmp/internal-state.json",
        "internal-instance",
    )
    assert calls[1][0] == "server"


@pytest.mark.parametrize(
    "hidden_args",
    [
        ["--daemon-id", "internal-instance"],
        ["--daemon-state", "/tmp/internal-state.json"],
    ],
)
def test_cli_serve_requires_complete_daemon_claim_arguments(hidden_args):
    with pytest.raises(SystemExit):
        main(["serve", *hidden_args])


def test_cli_serve_rejects_failed_daemon_claim(monkeypatch):
    monkeypatch.setattr(
        daemon,
        "claim_daemon_state",
        lambda *args: (_ for _ in ()).throw(
            daemon.DaemonError("instance mismatch")
        ),
    )
    server_calls = []
    monkeypatch.setattr(
        "nomad.server.main",
        lambda **kwargs: server_calls.append(kwargs),
    )

    with pytest.raises(SystemExit):
        main(
            [
                "serve",
                "--daemon-id",
                "internal-instance",
                "--daemon-state",
                "/tmp/internal-state.json",
            ]
        )

    assert server_calls == []


def test_cli_serve_rejects_remote_even_with_token(monkeypatch):
    monkeypatch.setenv("NOMAD_MCP_BEARER_TOKEN", "environment-secret")

    with pytest.raises(SystemExit):
        main(["serve", "--host", "0.0.0.0"])


def test_cli_serve_hidden_daemon_id_cannot_bypass_loopback(monkeypatch):
    monkeypatch.setenv("NOMAD_MCP_BEARER_TOKEN", "environment-secret")

    with pytest.raises(SystemExit):
        main(
            [
                "serve",
                "--host",
                "0.0.0.0",
                "--daemon-id",
                "internal-instance",
            ]
        )


def test_cli_serve_rejects_removed_allow_remote_option():
    with pytest.raises(SystemExit):
        main(["serve", "--allow-remote"])


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
        ]
    ) == 0

    assert calls == [
        {
            "project": "/tmp/project",
            "host": "localhost",
            "port": 9876,
            "path": "/nomad",
        }
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "running"


def test_cli_daemon_start_omits_port_for_project_derived_default(
    monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(
        daemon,
        "start_daemon",
        lambda **kwargs: calls.append(kwargs) or {"status": "running"},
    )

    assert main(["daemon", "start", "--project", "/tmp/project"]) == 0

    assert calls == [
        {
            "project": "/tmp/project",
            "host": "127.0.0.1",
            "port": None,
            "path": "/mcp",
        }
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "running"


def test_cli_daemon_start_rejects_removed_allow_remote_option():
    with pytest.raises(SystemExit):
        main(["daemon", "start", "--allow-remote"])


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


def test_cli_daemon_token_prints_only_secret(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        daemon,
        "read_daemon_token",
        lambda **kwargs: calls.append(kwargs) or "project-secret",
    )

    assert main(["daemon", "token", "--project", "/tmp/project"]) == 0

    captured = capsys.readouterr()
    assert calls == [{"project": "/tmp/project"}]
    assert captured.out == "project-secret\n"
    assert captured.err == ""


def test_cli_daemon_token_reports_uninitialized_without_stdout(
    monkeypatch, capsys
):
    def fail(**kwargs):
        raise daemon.DaemonError("daemon authentication token is not initialized")

    monkeypatch.setattr(daemon, "read_daemon_token", fail)

    assert main(["daemon", "token"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "error: daemon authentication token is not initialized\n"
    )


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
