import json
import subprocess
from pathlib import Path
from nomad.tools.commands import run_remote


def _payload(result: str) -> dict:
    return json.loads(result)


def test_run_remote_unconfigured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    res = run_remote("echo hello")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "unconfigured"


def test_run_remote_local_mode(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text('{"project_name": "app", "mode": "local"}', encoding="utf-8")

    res = run_remote("echo hello")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "local_mode"


def test_run_remote_invalid_json_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text("{bad json", encoding="utf-8")

    res = run_remote("echo hello")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"


def test_run_remote_unsafe_remote_path(tmp_path, monkeypatch):
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
                "remote_path": "/home/bad",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    res = run_remote("echo hello", "gpu")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] in {"unsafe_remote_path", "invalid_config"}



def test_run_remote_interactive_rejected(tmp_path, monkeypatch):
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

    res = run_remote("vim test.py", "gpu")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "interactive_command"


def test_run_remote_dangerous_rejected(tmp_path, monkeypatch):
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

    res = run_remote("rm -rf /", "gpu")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "dangerous_command"


def test_run_remote_ssh_preflight_failed(tmp_path, monkeypatch):
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

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="Permission denied (publickey).")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = run_remote("echo hello", "gpu")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "ssh_auth_failed"
    assert payload["next_action"] == {"tool": "net_diagnose", "args": {"target": "gpu"}}


def test_run_remote_success(tmp_path, monkeypatch):
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
                "runtime": {
                    "extra_env": {
                        "DATA_DIR": "/data/datasets"
                    }
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            # preflight
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            # run_remote
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="hello world\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = run_remote("python script.py", "gpu")
    payload = _payload(res)

    assert payload["ok"] is True
    assert payload["tool"] == "run_remote"
    assert payload["target"] == "gpu"
    assert "hello world" in payload["data"]["output"]

    exec_cmd = calls[1]
    remote_shell = exec_cmd[-1]
    assert "cd /workspace/app" in remote_shell
    assert "DATA_DIR=/data/datasets" in remote_shell
    assert "python script.py" in remote_shell



def test_run_remote_command_failed(tmp_path, monkeypatch):
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

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="Error: module not found\n")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = run_remote("python invalid.py", "gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "remote_command_failed"
    assert "Error: module not found" in payload["diagnostics"][0]


def test_run_remote_timeout(tmp_path, monkeypatch):
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

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            raise subprocess.TimeoutExpired(cmd, timeout=300)

    monkeypatch.setattr("subprocess.run", fake_run)

    res = run_remote("python long_running.py", "gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "command_timeout"


def test_run_remote_tunnel_env_injection_and_user_override(tmp_path, monkeypatch):
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
                "network": {
                    "reverse_tunnel": {
                        "enabled": True,
                        "local_proxy_port": 7890,
                        "remote_bind_port": 10800,
                    }
                },
                "runtime": {
                    "extra_env": {
                        "ALL_PROXY": "socks5://127.0.0.1:9999"
                    }
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if "-O" in cmd and "check" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="Master running", stderr="")
        elif "echo ok" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="done", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = run_remote("curl https://example.com", "gpu")
    payload = _payload(res)

    assert payload["ok"] is True
    # Verify user extra_env overrode tunnel_env
    remote_shell = calls[-1][-1]
    assert "ALL_PROXY=socks5://127.0.0.1:9999" in remote_shell
    assert any("overridden by user runtime.extra_env" in diag for diag in payload["diagnostics"])


def test_run_remote_invalid_extra_env_key(tmp_path, monkeypatch):
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
                "runtime": {
                    "extra_env": {
                        "BAD-NAME": "value"
                    }
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    res = run_remote("echo hello", "gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"
    assert len(calls) == 0


def test_run_remote_non_string_extra_env_value(tmp_path, monkeypatch):
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
                "runtime": {
                    "extra_env": {
                        "PORT": 8080
                    }
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    res = run_remote("echo hello", "gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"
    assert len(calls) == 0


def test_run_remote_redacts_secret_in_diagnostics(tmp_path, monkeypatch):
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
                "network": {
                    "reverse_tunnel": {
                        "enabled": True,
                        "local_proxy_port": 7890,
                        "remote_bind_port": 10800,
                    }
                },
                "runtime": {
                    "extra_env": {
                        "ALL_PROXY": "socks5://user:secret@127.0.0.1:9999"
                    }
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if "-O" in cmd and "check" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="Master running", stderr="")
        elif "echo ok" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="done", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = run_remote("curl https://example.com", "gpu")
    payload = _payload(res)

    assert payload["ok"] is True
    # The diagnostics string in JSON response MUST NOT contain the raw secret or user credentials
    assert "secret" not in res
    assert "user:secret" not in res
    assert any("Env 'ALL_PROXY' from tunnel was overridden by user runtime.extra_env." in d for d in payload["diagnostics"])

