import json
from pathlib import Path

from nomad.tools.init import (
    init_discover,
    init_probe_target,
    init_save_config,
    init_verify_and_probe,
)


def _payload(result: str) -> dict:
    return json.loads(result)


def test_init_discover_returns_local_workspace_summary(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "my_project"
    home.mkdir()
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (workspace / "package.json").write_text("{}", encoding="utf-8")
    (workspace / ".gitignore").write_text(".venv\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(workspace)

    payload = _payload(init_discover())

    assert payload["ok"] is True
    assert payload["tool"] == "init_discover"
    assert payload["data"]["state"] == "discovered"
    assert payload["data"]["project_name"] == "my_project"
    assert payload["data"]["project_types"] == ["python", "node"]
    assert payload["data"]["gitignore_exists"] is True
    assert "local_path" not in payload["data"]
    schema = payload["data"]["config_schema"]
    assert schema["minimal_remote_template"]["project_name"] == "my_project"
    assert schema["minimal_remote_template"]["targets"]["main"]["limits"]["command_timeout_seconds"] == 60
    assert "targets.<name>.local_subpath" in schema["fields"]
    assert "task_start" in schema["command_duration_guidance"]


def test_init_discover_reads_non_wildcard_ssh_hosts(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    workspace.mkdir()
    (ssh_dir / "config").write_text(
        """
Host gpu box
  HostName gpu.example
Host *
  ServerAliveInterval 30
Host ?.wild
  User ignored
Host cpu
  HostName cpu.example
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(workspace)

    payload = _payload(init_discover())

    assert payload["data"]["ssh_hosts"] == ["box", "cpu", "gpu"]


def test_init_discover_handles_missing_ssh_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(workspace)

    payload = _payload(init_discover())

    assert payload["data"]["ssh_hosts"] == []


def test_init_discover_detects_proxy_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")

    payload = _payload(init_discover())

    assert payload["data"]["network"]["proxy_detected"] is True
    assert payload["data"]["network"]["proxy_url"] == "socks5://127.0.0.1:7890"
    assert payload["data"]["network"]["proxy_scheme"] == "socks5"
    assert payload["data"]["network"]["proxy_host"] == "127.0.0.1"
    assert payload["data"]["network"]["proxy_port"] == 7890


def test_init_discover_redacts_proxy_credentials(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("ALL_PROXY", "socks5://user:secret@127.0.0.1:7890")

    result = init_discover()
    payload = _payload(result)

    assert "user:secret" not in result
    assert "secret" not in result
    assert payload["data"]["network"]["proxy_url"] == "socks5://***:***@127.0.0.1:7890"
    assert payload["data"]["network"]["proxy_scheme"] == "socks5"
    assert payload["data"]["network"]["proxy_host"] == "127.0.0.1"
    assert payload["data"]["network"]["proxy_port"] == 7890


def test_init_discover_detects_project_markers(monkeypatch, tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    for filename in ["requirements.txt", "go.mod", "Cargo.toml", "Makefile"]:
        (workspace / filename).write_text("", encoding="utf-8")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(workspace)

    payload = _payload(init_discover())

    assert payload["data"]["project_types"] == ["python", "go", "rust", "make"]


def test_init_verify_and_probe_connection_failed(monkeypatch):
    import subprocess
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="Permission denied (publickey).")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = init_verify_and_probe("myhost", "/workspace/remote/path")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "ssh_auth_failed"
    assert "Permission denied" in payload["diagnostics"][0]


def test_init_verify_and_probe_invalid_host():
    result = init_verify_and_probe("myhost; rm -rf /", "/workspace/remote/path")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"



def test_init_verify_and_probe_success(monkeypatch):
    import subprocess
    calls = []
    
    probe_output = (
        "___NOMAD_PROBE_DELIM___\n"
        "Linux 6.1.0-9-amd64 x86_64\n"
        "___NOMAD_PROBE_DELIM___\n"
        "16\n"
        "___NOMAD_PROBE_DELIM___\n"
        "64Gi\n"
        "___NOMAD_PROBE_DELIM___\n"
        "250G\n"
        "___NOMAD_PROBE_DELIM___\n"
        "NVIDIA A100-SXM4-80GB, 81920 MiB\n"
        "___NOMAD_PROBE_DELIM___\n"
        "Python 3.11.4\n"
        "/usr/bin/python3\n"
        "___NOMAD_PROBE_DELIM___\n"
        "base                  *  /opt/conda\n"
        "ml-env                   /opt/conda/envs/ml-env\n"
        "___NOMAD_PROBE_DELIM___\n"
        "/workspace/remote/path/.venv/pyvenv.cfg\n"
        "___NOMAD_PROBE_DELIM___\n"
        "v20.11.0\n"
        "/usr/bin/node\n"
        "___NOMAD_PROBE_DELIM___\n"
        "v18.19.0\n"
        "___NOMAD_PROBE_DELIM___\n"
        "go version go1.22.0 linux/amd64\n"
        "/usr/local/go/bin/go\n"
        "___NOMAD_PROBE_DELIM___\n"
        "ruby 3.2.2\n"
        "/usr/bin/ruby\n"
        "___NOMAD_PROBE_DELIM___\n"
    )

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        # First call is preflight "echo ok", second call is probe script
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=probe_output, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = init_verify_and_probe("myhost", "/workspace/remote/path", jump_host="bastion")
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["tool"] == "init_verify_and_probe"
    assert payload["data"]["verified"] is True
    assert payload["data"]["ssh_host"] == "myhost"
    assert payload["data"]["remote_path"] == "/workspace/remote/path"
    
    hw = payload["data"]["hardware"]
    assert hw["os"] == "Linux 6.1.0-9-amd64 x86_64"
    assert hw["cpu_cores"] == 16
    assert hw["memory_total"] == "64Gi"
    assert hw["disk_available"] == "250G"
    assert len(hw["gpu"]) == 1
    assert hw["gpu"][0]["name"] == "NVIDIA A100-SXM4-80GB"
    assert hw["gpu"][0]["memory_total"] == "81920 MiB"

    runtimes = hw["detected_runtimes"]
    # Check Python system
    py_sys = next(r for r in runtimes if r["lang"] == "python" and r["type"] == "system")
    assert py_sys["bin"] == "/usr/bin/python3"
    assert py_sys["version"] == "3.11.4"

    # Check Conda
    py_conda = [r for r in runtimes if r["lang"] == "python" and r["type"] == "conda"]
    assert len(py_conda) == 2
    assert py_conda[0]["name"] == "base"
    assert py_conda[0]["bin"] == "/opt/conda/bin/python"
    assert py_conda[1]["name"] == "ml-env"
    assert py_conda[1]["bin"] == "/opt/conda/envs/ml-env/bin/python"

    # Check Venv
    py_venv = next(r for r in runtimes if r["lang"] == "python" and r["type"] == "venv")
    assert py_venv["name"] == ".venv"
    assert py_venv["bin"] == "/workspace/remote/path/.venv/bin/python"


    # Check Node system & nvm
    node_sys = next(r for r in runtimes if r["lang"] == "node" and r["type"] == "system")
    assert node_sys["version"] == "20.11.0"
    node_nvm = next(r for r in runtimes if r["lang"] == "node" and r["type"] == "nvm")
    assert node_nvm["version"] == "18.19.0"

    # Check Go
    go_sys = next(r for r in runtimes if r["lang"] == "go")
    assert go_sys["version"] == "1.22.0"

    # Check Ruby
    ruby_sys = next(r for r in runtimes if r["lang"] == "ruby")
    assert ruby_sys["version"] == "3.2.2"

    assert "probed_at" in hw


def test_init_verify_and_probe_unsafe_remote_path(monkeypatch):
    import subprocess
    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    # Relative path is unsafe
    result = init_verify_and_probe("myhost", "../bad")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "unsafe_remote_path"
    # Preflight called once ("echo ok"), probe script must NOT be called
    assert len(calls) == 1


def test_init_verify_and_probe_command_timeout(monkeypatch):
    import subprocess
    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=15)

    monkeypatch.setattr("subprocess.run", fake_run)

    result = init_verify_and_probe("myhost", "/workspace/project")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "command_timeout"
    assert "timed out" in payload["diagnostics"][0]


def test_init_verify_and_probe_empty_memory_and_disk_normalized(monkeypatch):
    import subprocess
    calls = []

    probe_output = (
        "___NOMAD_PROBE_DELIM___\n"
        "Linux 6.1.0\n"
        "___NOMAD_PROBE_DELIM___\n"
        "4\n"
        "___NOMAD_PROBE_DELIM___\n"
        "   \n"  # empty memory
        "___NOMAD_PROBE_DELIM___\n"
        "path_not_exist\n"  # invalid disk
        "___NOMAD_PROBE_DELIM___\n"
        "__no_gpu__\n"
        "___NOMAD_PROBE_DELIM___\n"
        "__no_python__\n"
        "___NOMAD_PROBE_DELIM___\n"
    )

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=probe_output, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = init_verify_and_probe("myhost", "/workspace/project")
    payload = _payload(result)

    assert payload["ok"] is True
    hw = payload["data"]["hardware"]
    assert hw["memory_total"] == "Unknown"
    assert hw["disk_available"] == "Unknown"


def test_init_save_config_first_save(tmp_path, monkeypatch):
    import subprocess
    from nomad.config import load_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    valid_config = {
        "project_name": "my-app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "gpu-box",
                "remote_path": "/workspace/my-app",
                "auto_create_remote_path": True,
            }
        },
    }

    result = init_save_config(json.dumps(valid_config))
    payload = _payload(result)

    assert payload["ok"] is True
    assert (workspace / ".nomad.json").exists()
    assert not (workspace / ".nomad.json.bak").exists()

    loaded = load_config()
    assert loaded["project_name"] == "my-app"
    assert loaded["mode"] == "remote"
    assert "gpu" in loaded["targets"]

    # Verify SSH mkdir was called for remote path auto-creation
    assert any("mkdir -p" in " ".join(cmd) for cmd in calls)


def test_init_save_config_overwrite_creates_backup(tmp_path, monkeypatch):
    import subprocess

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text('{"project_name": "old-app", "mode": "local"}', encoding="utf-8")

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode=0, stdout="", stderr=""))

    new_config = {
        "project_name": "new-app",
        "mode": "local",
        "targets": {},
    }

    result = init_save_config(json.dumps(new_config))
    payload = _payload(result)

    assert payload["ok"] is True
    assert (workspace / ".nomad.json.bak").exists()
    bak_content = (workspace / ".nomad.json.bak").read_text(encoding="utf-8")
    assert "old-app" in bak_content

    new_content = (workspace / ".nomad.json").read_text(encoding="utf-8")
    assert "new-app" in new_content


def test_init_save_config_rejects_invalid_json_or_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    # Invalid JSON
    res1 = init_save_config("not json")
    payload1 = _payload(res1)
    assert payload1["ok"] is False
    assert payload1["error_type"] == "invalid_config"

    # Invalid config schema (remote mode with no targets)
    invalid_cfg = {"project_name": "app", "mode": "remote", "targets": {}}
    res2 = init_save_config(json.dumps(invalid_cfg))
    payload2 = _payload(res2)
    assert payload2["ok"] is False
    assert payload2["error_type"] == "invalid_config"


def test_init_probe_target_unconfigured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = init_probe_target("default")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "unconfigured"


def test_init_probe_target_invalid_json_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text("{bad json", encoding="utf-8")

    result = init_probe_target("default")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"


def test_init_probe_target_target_not_found(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    result = init_probe_target("nonexistent")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "target_not_found"


def test_init_probe_target_success_updates_hardware_and_saves(tmp_path, monkeypatch):
    import subprocess
    from nomad.config import load_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    initial_cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "hardware": {
                    "os": "Linux",
                    "cpu_cores": 2,
                    "memory_total": "8Gi",
                    "disk_available": "10G",
                    "gpu": [],
                    "detected_runtimes": [],
                    "probed_at": "2026-01-01T00:00:00Z",
                },
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(initial_cfg), encoding="utf-8")

    probe_output = (
        "___NOMAD_PROBE_DELIM___\n"
        "Linux 6.1.0 x86_64\n"
        "___NOMAD_PROBE_DELIM___\n"
        "64\n"
        "___NOMAD_PROBE_DELIM___\n"
        "256Gi\n"
        "___NOMAD_PROBE_DELIM___\n"
        "1000G\n"
        "___NOMAD_PROBE_DELIM___\n"
        "NVIDIA H100 80GB, 81920 MiB\n"
        "___NOMAD_PROBE_DELIM___\n"
        "Python 3.12.0\n"
        "/usr/bin/python3\n"
        "___NOMAD_PROBE_DELIM___\n"
    )

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=probe_output, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = init_probe_target("default")
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["tool"] == "init_probe_target"

    loaded = load_config()
    hw = loaded["targets"]["gpu"]["hardware"]
    assert hw["cpu_cores"] == 64
    assert hw["memory_total"] == "256Gi"
    assert hw["gpu"][0]["name"] == "NVIDIA H100 80GB"


def test_init_save_config_redacts_extra_env_secrets(tmp_path, monkeypatch):
    import subprocess
    from nomad.config import load_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], returncode=0, stdout="", stderr=""))

    config_with_secrets = {
        "project_name": "secret-app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "gpu-box",
                "remote_path": "/workspace/secret-app",
                "runtime": {
                    "extra_env": {
                        "API_TOKEN": "supersecret",
                        "NORMAL_ENV": "visible",
                    }
                },
            }
        },
    }

    result = init_save_config(json.dumps(config_with_secrets))
    payload = _payload(result)

    assert payload["ok"] is True
    # The return JSON string must NEVER contain the secret text
    assert "supersecret" not in result
    assert "extra_env" not in json.dumps(payload["data"])

    # Physical config file on disk should still preserve the actual config
    loaded = load_config()
    assert loaded["targets"]["gpu"]["runtime"]["extra_env"]["API_TOKEN"] == "supersecret"


