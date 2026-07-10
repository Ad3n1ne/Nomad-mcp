import json

from nomad import __version__
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


def test_cli_doctor_success(monkeypatch, capsys):
    monkeypatch.setattr("nomad.cli.shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    assert main(["doctor"]) == 0

    out = capsys.readouterr().out
    assert "python>=3.11" in out
    assert "ssh" in out
    assert "rsync" in out
