import re
import json
import base64
import subprocess
import pytest
from nomad.tools.tasks import (
    validate_task_name,
    get_session_name,
    generate_task_script,
    task_start,
    task_status,
    task_list,
    task_kill,
)


def test_validate_task_name_valid():
    # Valid names should not raise any exception
    validate_task_name("my-task-1")
    validate_task_name("task_2")
    validate_task_name("a" * 40)


def test_validate_task_name_invalid():
    with pytest.raises(ValueError, match="invalid task name"):
        validate_task_name("My-Task")  # uppercase not allowed
    with pytest.raises(ValueError, match="invalid task name"):
        validate_task_name("task.1")  # dots not allowed
    with pytest.raises(ValueError, match="invalid task name"):
        validate_task_name("")  # empty not allowed
    with pytest.raises(ValueError, match="invalid task name"):
        validate_task_name("a" * 41)  # too long


def test_get_session_name_valid():
    session = get_session_name("project", "gpu", "my-task")
    assert session == "project_gpu_my-task"


def test_get_session_name_too_long():
    with pytest.raises(ValueError, match="session name exceeds 100 characters"):
        get_session_name("a" * 40, "b" * 30, "c" * 30)  # total 100 is fine, but this might exceed 100 with underscores
    
    # 40 + 30 + 28 = 98 + 2 underscores = 100 (allowed)
    get_session_name("a" * 40, "b" * 30, "c" * 28)

    # 40 + 30 + 29 = 99 + 2 underscores = 101 (too long)
    with pytest.raises(ValueError, match="session name exceeds 100 characters"):
        get_session_name("a" * 40, "b" * 30, "c" * 29)


def test_generate_task_script():
    remote_path = "/workspace/my_app"
    env_vars = {
        "ALL_PROXY": "socks5://127.0.0.1:1080",
        "DATA_DIR": "/workspace/data"
    }
    cmd = "python train.py --epochs 10 > output.txt"
    exit_file = "/workspace/my_app/.nomad/tasks/session_1.exit"

    script = generate_task_script(remote_path, env_vars, cmd, exit_file)

    # Check script contents
    assert script.startswith("#!/usr/bin/env bash")
    assert "cd /workspace/my_app" in script
    assert "export ALL_PROXY=socks5://127.0.0.1:1080" in script
    assert "export DATA_DIR=/workspace/data" in script
    
    # Extract base64 payload and verify it decodes to original command
    match = re.search(r"echo\s+([A-Za-z0-9+/=]+)\s*\|\s*base64\s+-d", script)
    assert match is not None
    b64_payload = match.group(1)
    decoded_cmd = base64.b64decode(b64_payload.encode("utf-8")).decode("utf-8")
    assert decoded_cmd == cmd

    assert "echo $? > /workspace/my_app/.nomad/tasks/session_1.exit" in script


def test_generate_task_script_invalid_env_key():
    with pytest.raises(ValueError, match="invalid env key"):
        generate_task_script("/workspace/a", {"BAD-NAME": "x"}, "echo hi", "/tmp/e")
    
    with pytest.raises(ValueError, match="invalid env key"):
        generate_task_script("/workspace/a", {"BAD\nKEY": "x"}, "echo hi", "/tmp/e")


def test_generate_task_script_invalid_env_val():
    with pytest.raises(ValueError, match="must be a string"):
        generate_task_script("/workspace/a", {"PORT": 8080}, "echo hi", "/tmp/e")


def test_task_start_unconfigured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    res = task_start("echo hello", "mytask")
    payload = json.loads(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "unconfigured"


def test_task_start_local_mode(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text('{"project_name": "app", "mode": "local"}', encoding="utf-8")

    res = task_start("echo hello", "mytask")
    payload = json.loads(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "local_mode"


def test_task_start_invalid_name(tmp_path, monkeypatch):
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

    res = task_start("echo hello", "BAD-TASK", "gpu")
    payload = json.loads(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"


def test_task_start_session_exists(tmp_path, monkeypatch):
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
            # preflight
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        elif len(calls) == 2:
            # tmux has-session
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="session exists", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_start("echo hello", "mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "task_exists"
    assert "tmux has-session -t app_gpu_mytask" in calls[1][-1]


def test_task_start_success(tmp_path, monkeypatch):
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
        elif "tmux has-session" in cmd[-1]:
            # tmux has-session (does not exist)
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no server running")
        else:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="done", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_start("python train.py", "mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["session_name"] == "app_gpu_mytask"
    assert "app_gpu_mytask.sh" in payload["data"]["script_path"]
    assert "app_gpu_mytask.log" in payload["data"]["log_path"]
    assert "app_gpu_mytask.exit" in payload["data"]["exit_path"]

    # 4 subprocess calls:
    # 1. SSH Preflight (echo ok)
    # 2. Tunnel check (check master)
    # 3. Tmux check (tmux has-session -t app_gpu_mytask)
    # 4. Upload script (echo b64 | base64 -d > script.sh)
    # 5. Start Tmux (tmux new-session -d ...)
    assert len(calls) >= 5
    # Verify the start tmux command
    tmux_start_cmd = calls[-1][-1]
    assert "tmux new-session -d -s app_gpu_mytask" in tmux_start_cmd
    assert "exec bash" in tmux_start_cmd
    assert "app_gpu_mytask.sh" in tmux_start_cmd


def test_task_start_default_uses_resolved_target_session(tmp_path, monkeypatch):
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
        if "echo ok" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        if "tmux has-session" in cmd[-1]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no server")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="done", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_start("echo hello", "mytask")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["target"] == "gpu"
    assert payload["data"]["session_name"] == "app_gpu_mytask"
    assert all("app_default_mytask" not in " ".join(call) for call in calls)


def test_task_start_tmux_check_timeout_does_not_start(tmp_path, monkeypatch):
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
            # preflight
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        elif len(calls) == 2:
            # tmux has-session (raise timeout)
            raise subprocess.TimeoutExpired(cmd, timeout=10)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_start("echo hello", "mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "command_timeout"
    # Verify we did not execute upload script or start tmux (only preflight and has-session)
    assert len(calls) == 2


def test_task_start_dangerous_command(tmp_path, monkeypatch):
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

    res = task_start("rm -rf /", "mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "dangerous_command"


def test_task_start_interactive_command(tmp_path, monkeypatch):
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

    res = task_start("vim train.py", "mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "interactive_command"


def test_task_start_tunnel_disabled_no_tunnel_check(tmp_path, monkeypatch):
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
                        "enabled": False,
                    }
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if "echo ok" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        elif "tmux has-session" in cmd[-1]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="no server")
        else:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="done", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_start("echo hello", "mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    # Verify no control master check was called (-O check is absent)
    assert not any("-O" in cmd and "check" in cmd for cmd in calls)


def test_task_start_tunnel_start_failed(tmp_path, monkeypatch):
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
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            # Preflight for task_start itself
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        elif "-O" in cmd and "check" in cmd:
            # tunnel is not running
            return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="not running")
        elif "echo ok" in cmd:
            # Preflight during tunnel_start fails
            return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="Permission denied (publickey).")
        else:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="done", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_start("echo hello", "mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "tunnel_start_failed"
    # Ensure underlying error is present in diagnostics
    assert any("Underlying error type: ssh_auth_failed" in d for d in payload["diagnostics"])


def test_task_status_running(tmp_path, monkeypatch):
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
            # check script stdout
            stdout_content = "running\n---LOG_START---\nEpoch 1/10\nEpoch 2/10"
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout_content, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_status("mytask", "gpu", tail_lines=2)
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["status"] == "running"
    assert "Epoch 1/10" in payload["data"]["output"]


def test_task_status_default_uses_resolved_target_session(tmp_path, monkeypatch):
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
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="running\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_status("mytask")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["target"] == "gpu"
    assert payload["data"]["session_name"] == "app_gpu_mytask"
    status_cmd = calls[-1][-1]
    assert "app_gpu_mytask" in status_cmd
    assert "app_default_mytask" not in status_cmd


def test_task_status_finished_success(tmp_path, monkeypatch):
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
            stdout_content = "finished_success\n---LOG_START---\nTraining completed successfully."
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout_content, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_status("mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["status"] == "finished_success"
    assert payload["data"]["exit_code"] == 0
    assert "Training completed successfully." in payload["data"]["output"]


def test_task_status_finished_error(tmp_path, monkeypatch):
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
            stdout_content = "finished_error:127\n---LOG_START---\nCommand not found error."
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout_content, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_status("mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["status"] == "finished_error"
    assert payload["data"]["exit_code"] == 127
    assert "Command not found error." in payload["data"]["output"]


def test_task_status_missing(tmp_path, monkeypatch):
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
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="missing\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_status("mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["status"] == "missing"


def test_task_status_unknown(tmp_path, monkeypatch):
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
            # Status check fails with code 255
            return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="SSH fail")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_status("mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["status"] == "unknown"


def test_task_list_specified(tmp_path, monkeypatch):
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
            stdout_content = "app_gpu_task-1\napp_gpu_task-2\n---FILES_START---\napp_gpu_task-1.sh\napp_gpu_task-2.sh\napp_gpu_task-3.exit"
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout_content, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_list("gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    tasks = payload["data"]["tasks"]
    assert len(tasks) == 3
    # Check status mapping
    t1 = next(t for t in tasks if t["task_name"] == "task-1")
    assert t1["status"] == "running"
    t3 = next(t for t in tasks if t["task_name"] == "task-3")
    assert t3["status"] == "finished" or t3["status"] in {"unknown", "finished_success", "finished_error"}


def test_task_list_default_uses_resolved_target_prefix(tmp_path, monkeypatch):
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
        stdout_content = "app_gpu_mytask\n---FILES_START---\napp_gpu_mytask.log"
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=stdout_content, stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_list("default")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["tasks"] == [
        {
            "task_name": "mytask",
            "target": "gpu",
            "session_name": "app_gpu_mytask",
            "status": "running",
        }
    ]
    scan_cmd = calls[-1][-1]
    assert "app_gpu_" in scan_cmd
    assert "app_default_" not in scan_cmd


def test_task_list_all_with_unreachable_target(tmp_path, monkeypatch):
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
            },
            "cpu": {
                "ssh_host": "deadhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        # Identify by deadhost in command
        cmd_str = " ".join(cmd)
        if "deadhost" in cmd_str:
            return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="host down")
        else:
            if "echo ok" in cmd_str:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
            else:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="app_gpu_task-1\n---FILES_START---\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_list()
    payload = json.loads(res)

    assert payload["ok"] is True
    # Verify the reachable one has its task, and dead one is noted/marked unknown
    tasks = payload["data"]["tasks"]
    assert any(t["target"] == "gpu" and t["task_name"] == "task-1" for t in tasks)
    assert any("deadhost" in diag or "cpu" in diag for diag in payload["diagnostics"])


def test_task_kill_normal(tmp_path, monkeypatch):
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
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_kill("mytask", "gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["session_name"] == "app_gpu_mytask"
    # Ensure command sent is tmux kill-session
    kill_cmd_str = calls[-1][-1]
    assert "tmux kill-session -t app_gpu_mytask" in kill_cmd_str
    # Verify tunnel_stop is NEVER called (no tunnel commands in subprocess list)
    assert not any("-O" in cmd and "exit" in cmd for cmd in calls)


def test_task_kill_default_uses_resolved_target_session(tmp_path, monkeypatch):
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
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_kill("mytask")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert payload["data"]["target"] == "gpu"
    assert payload["data"]["session_name"] == "app_gpu_mytask"
    kill_cmd_str = calls[-1][-1]
    assert "tmux kill-session -t app_gpu_mytask" in kill_cmd_str
    assert "app_default_mytask" not in kill_cmd_str


def test_task_status_tail_lines_invalid(tmp_path, monkeypatch):
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

    # String value
    res = task_status("mytask", "gpu", tail_lines="2; echo hacked")
    payload = json.loads(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"

    # Out of range (negative)
    res2 = task_status("mytask", "gpu", tail_lines=-5)
    payload2 = json.loads(res2)
    assert payload2["ok"] is False
    assert payload2["error_type"] == "invalid_config"

    # Out of range (too large)
    res3 = task_status("mytask", "gpu", tail_lines=501)
    payload3 = json.loads(res3)
    assert payload3["ok"] is False
    assert payload3["error_type"] == "invalid_config"


def test_task_list_cwd_unsafe(tmp_path, monkeypatch):
    # Simulate an unsafe local directory (e.g. root or user home)
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

    # Mock CWD safety to return an error
    monkeypatch.setattr("nomad.tools.tasks.verify_local_cwd_safety", lambda: "unsafe_local_cwd")

    res = task_list("gpu")
    payload = json.loads(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "unsafe_local_cwd"


def test_task_list_remote_path_unsafe(tmp_path, monkeypatch):
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
                "remote_path": "/workspace/unsafe-root",
            },
            "cpu": {
                "ssh_host": "cpu-host",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    # Mock verify_remote_path_safety to reject /workspace/unsafe-root
    def mock_verify(path):
        if path == "/workspace/unsafe-root":
            return "unsafe_remote_path"
        return None
    monkeypatch.setattr("nomad.tools.tasks.verify_remote_path_safety", mock_verify)

    # 1. Specified target 'gpu' is unsafe -> should fail fast
    res_fail = task_list("gpu")
    payload_fail = json.loads(res_fail)
    assert payload_fail["ok"] is False
    assert payload_fail["error_type"] == "unsafe_remote_path"

    # 2. Globally list (target=None) -> should skip gpu, probe cpu (but cpu has mock run return)
    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
    monkeypatch.setattr("subprocess.run", fake_run)

    res_global = task_list()
    payload_global = json.loads(res_global)
    assert payload_global["ok"] is True
    assert any("unsafe-root" in diag for diag in payload_global["diagnostics"])



def test_task_list_empty_grep_success(tmp_path, monkeypatch):
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
        if "echo ok" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            # Simulate grep returncode 0 but empty output (due to || true logic)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="\n---FILES_START---\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = task_list("gpu")
    payload = json.loads(res)

    assert payload["ok"] is True
    assert len(payload["data"]["tasks"]) == 0
    # No error recorded in diagnostics (empty grep is successful)
    assert len(payload["diagnostics"]) == 0


