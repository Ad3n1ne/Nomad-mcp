import json
import subprocess
from pathlib import Path
from nomad.tools.network import (
    get_tunnel_env,
    net_diagnose,
    tunnel_start,
    tunnel_status,
    tunnel_stop,
)


def _payload(result: str) -> dict:
    return json.loads(result)


PROXY_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "all_proxy",
    "https_proxy",
    "http_proxy",
)


def _write_remote_config(workspace: Path, extra_network: dict | None = None) -> None:
    network = {
        "reverse_tunnel": {
            "enabled": True,
            "local_proxy_port": 7890,
            "remote_bind_port": 10800,
        }
    }
    if extra_network:
        network.update(extra_network)
    cfg = {
        "project_name": "test_app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "gpu",
                "remote_path": "/workspace/app",
                "network": network,
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")


def _clear_proxy_env(monkeypatch):
    for key in PROXY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _ssh_g_stdout() -> str:
    return "\n".join(
        [
            "hostname gpu.example.com",
            "port 2222",
            "user ubuntu",
            "proxyjump jumpbox",
            "proxycommand none",
            "identityfile ~/.ssh/id_ed25519",
            "identityfile ~/.ssh/id_rsa",
        ]
    )


def test_get_tunnel_env():
    cfg_socks = {
        "network": {
            "reverse_tunnel": {
                "enabled": True,
                "proxy_scheme": "socks5",
                "remote_bind_port": 10800,
            }
        }
    }
    env_socks = get_tunnel_env(cfg_socks)
    assert env_socks == {"ALL_PROXY": "socks5://127.0.0.1:10800"}

    cfg_http = {
        "network": {
            "reverse_tunnel": {
                "enabled": True,
                "proxy_scheme": "http",
                "remote_bind_port": 10800,
            }
        }
    }
    env_http = get_tunnel_env(cfg_http)
    assert env_http == {
        "HTTP_PROXY": "http://127.0.0.1:10800",
        "HTTPS_PROXY": "http://127.0.0.1:10800",
    }


def test_net_diagnose_direct_success_with_dns(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _clear_proxy_env(monkeypatch)
    _write_remote_config(workspace, {"jump_host": "jumpbox"})

    calls = []

    def fake_getaddrinfo(host, port, type=None):
        return [
            (None, None, None, None, ("203.0.113.10", port)),
            (None, None, None, None, ("203.0.113.11", port)),
        ]

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if cmd[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=_ssh_g_stdout(), stderr="")
        if cmd[:4] == ["nc", "-z", "-w", "3"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[0] == "ssh" and cmd[-1] == "echo ok":
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("subprocess.run", fake_run)

    payload = _payload(net_diagnose("gpu"))

    assert payload["ok"] is True
    assert payload["target"] == "gpu"
    data = payload["data"]
    assert data["ssh_config"] == {
        "hostname": "gpu.example.com",
        "port": 2222,
        "user": "ubuntu",
        "proxyjump": "jumpbox",
        "proxycommand": None,
        "identityfile": ["~/.ssh/id_ed25519", "~/.ssh/id_rsa"],
    }
    assert data["dns"]["checked"] is True
    assert data["dns"]["addresses"] == ["203.0.113.10", "203.0.113.11"]
    assert data["direct_tcp"]["status"] == "reachable"
    assert data["direct_tcp"]["cmd"] == ["nc", "-z", "-w", "3", "gpu.example.com", "2222"]
    assert data["ssh_batch"]["status"] == "ok"
    assert any(cmd[:2] == ["ssh", "-G"] for cmd in calls)
    assert all("-f" not in cmd for cmd in calls)


def test_net_diagnose_direct_timeout_and_auth_failed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _clear_proxy_env(monkeypatch)
    _write_remote_config(workspace)

    def fake_getaddrinfo(host, port, type=None):
        return [(None, None, None, None, ("203.0.113.10", port))]

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=_ssh_g_stdout(), stderr="")
        if cmd[:4] == ["nc", "-z", "-w", "3"]:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if cmd[0] == "ssh" and cmd[-1] == "echo ok":
            return subprocess.CompletedProcess(
                cmd,
                returncode=255,
                stdout="",
                stderr="Permission denied (publickey).",
            )
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr("subprocess.run", fake_run)

    payload = _payload(net_diagnose("gpu"))
    data = payload["data"]

    assert data["direct_tcp"]["status"] == "timeout"
    assert data["ssh_batch"]["classification"] == "permission_denied"
    assert any("authentication failed" in suggestion for suggestion in data["suggestions"])


def test_net_diagnose_host_key_failed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _clear_proxy_env(monkeypatch)
    _write_remote_config(workspace)

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=_ssh_g_stdout(), stderr="")
        if cmd[:4] == ["nc", "-z", "-w", "3"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            returncode=255,
            stdout="",
            stderr="Host key verification failed.",
        )

    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])
    monkeypatch.setattr("subprocess.run", fake_run)

    payload = _payload(net_diagnose("gpu"))

    assert payload["data"]["ssh_batch"]["classification"] == "host_key_failed"


def test_net_diagnose_connection_refused(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _clear_proxy_env(monkeypatch)
    _write_remote_config(workspace)

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=_ssh_g_stdout(), stderr="")
        if cmd[:4] == ["nc", "-z", "-w", "3"]:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="connection refused")
        return subprocess.CompletedProcess(
            cmd,
            returncode=255,
            stdout="",
            stderr="ssh: connect to host gpu.example.com port 2222: Connection refused",
        )

    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])
    monkeypatch.setattr("subprocess.run", fake_run)

    payload = _payload(net_diagnose("gpu"))
    data = payload["data"]

    assert data["direct_tcp"]["status"] == "unreachable"
    assert data["ssh_batch"]["classification"] == "connection_refused"


def test_net_diagnose_proxy_env_redacted(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("ALL_PROXY", "socks5://user:secret@127.0.0.1:7890")
    _write_remote_config(workspace, {"use_proxy_for_ssh": True})

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout="\n".join(
                    [
                        "hostname 203.0.113.20",
                        "port 22",
                        "user ubuntu",
                        "proxyjump none",
                        "proxycommand nc -X 5 -x 127.0.0.1:7890 %h %p",
                        "identityfile ~/.ssh/id_ed25519",
                    ]
                ),
                stderr="",
            )
        if cmd[:4] == ["nc", "-z", "-w", "3"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = net_diagnose("gpu")
    payload = _payload(result)
    data = payload["data"]

    assert data["local_proxy"]["detected"] is True
    assert data["local_proxy"]["url"] == "socks5://***:***@127.0.0.1:7890"
    assert data["local_proxy"]["scheme"] == "socks5"
    assert data["local_proxy"]["port"] == 7890
    assert "secret" not in result


def test_net_diagnose_proxycommand_credentials_redacted(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    _clear_proxy_env(monkeypatch)
    _write_remote_config(workspace)

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[:2] == ["ssh", "-G"]:
            return subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout="\n".join(
                    [
                        "hostname gpu.example.com",
                        "port 22",
                        "user ubuntu",
                        "proxyjump none",
                        "proxycommand nc -X 5 -x user:secret@127.0.0.1:7890 %h %p",
                        "identityfile ~/.ssh/id_ed25519",
                    ]
                ),
                stderr="",
            )
        if cmd[:4] == ["nc", "-z", "-w", "3"]:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [])
    monkeypatch.setattr("subprocess.run", fake_run)

    result = net_diagnose("gpu")
    payload = _payload(result)

    assert payload["data"]["ssh_config"]["proxycommand"] == (
        "nc -X 5 -x ***:***@127.0.0.1:7890 %h %p"
    )
    assert "secret" not in result
    assert "user:secret" not in result


def test_tunnel_start_unconfigured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    res = tunnel_start()
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "unconfigured"


def test_tunnel_start_disabled(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "network": {
                    "reverse_tunnel": {
                        "enabled": False
                    }
                }
            }
        }
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    res = tunnel_start("gpu")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"


def test_tunnel_start_port_in_use(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
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
                        "remote_bind_port": 10800
                    }
                }
            }
        }
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if "-O" in cmd and "check" in cmd:
            # tunnel check -> not running
            return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="No Master")
        elif any("nc -z" in str(arg) for arg in cmd):
            # remote port check -> in use (returncode 0)
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="port open", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")


    monkeypatch.setattr("subprocess.run", fake_run)

    res = tunnel_start("gpu")
    payload = _payload(res)
    assert payload["ok"] is False
    assert payload["error_type"] == "tunnel_port_in_use"


def test_tunnel_start_success(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
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
                        "remote_bind_port": 10800
                    }
                }
            }
        }
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if "-O" in cmd and "check" in cmd:
            # check -> not running initially
            return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="No Master")
        elif any("nc -z" in str(arg) for arg in cmd):
            # remote port check -> not in use (returncode 1)
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="")
        else:
            # ssh -f -N -M start -> success
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = tunnel_start("gpu")
    payload = _payload(res)
    assert payload["ok"] is True


    assert payload["data"]["status"] == "running"
    assert payload["data"]["env"] == {"ALL_PROXY": "socks5://127.0.0.1:10800"}

    # Verify start command flags
    start_cmd = calls[-1]
    assert "-f" in start_cmd
    assert "-N" in start_cmd
    assert "-M" in start_cmd
    assert "-R" in start_cmd
    assert "127.0.0.1:10800:127.0.0.1:7890" in start_cmd


def test_tunnel_status_running_and_stopped(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
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
                        "remote_bind_port": 10800
                    }
                }
            }
        }
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    status_state = {"running": True}
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if "-O" in cmd and "check" in cmd:
            if status_state["running"]:
                return subprocess.CompletedProcess(cmd, returncode=0, stdout="Master running", stderr="")
            else:
                return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="No master")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    # When running
    res = tunnel_status("gpu")
    payload = _payload(res)
    assert payload["ok"] is True
    assert payload["data"]["status"] == "running"

    # When stopped
    status_state["running"] = False
    res2 = tunnel_status("gpu")
    payload2 = _payload(res2)
    assert payload2["ok"] is True
    assert payload2["data"]["status"] == "stopped"


def test_tunnel_stop(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
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
                        "remote_bind_port": 10800
                    }
                }
            }
        }
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="Exit request sent", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    res = tunnel_stop("gpu")
    payload = _payload(res)
    assert payload["ok"] is True
    assert payload["data"]["status"] == "stopped"

    stop_cmd = calls[0]
    assert "-O" in stop_cmd
    assert "exit" in stop_cmd


def test_tunnel_status_malicious_jump_host(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "network": {
                    "jump_host": "-oProxyCommand=touch /tmp/pwn",
                    "reverse_tunnel": {
                        "enabled": True,
                        "local_proxy_port": 7890,
                        "remote_bind_port": 10800,
                    },
                },
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    res = tunnel_status("gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"
    assert len(calls) == 0


def test_tunnel_stop_malicious_jump_host(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "network": {
                    "jump_host": "-oProxyCommand=touch /tmp/pwn",
                    "reverse_tunnel": {
                        "enabled": True,
                        "local_proxy_port": 7890,
                        "remote_bind_port": 10800,
                    },
                },
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    res = tunnel_stop("gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"
    assert len(calls) == 0


def test_tunnel_start_malicious_ssh_host_does_not_call_check_master(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "-oProxyCommand=touch /tmp/pwn",
                "remote_path": "/workspace/app",
                "network": {
                    "reverse_tunnel": {
                        "enabled": True,
                        "local_proxy_port": 7890,
                        "remote_bind_port": 10800,
                    },
                },
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    res = tunnel_start("gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"
    assert len(calls) == 0


def test_tunnel_status_local_mode(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
        "mode": "local",
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
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    res = tunnel_status("gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "local_mode"
    assert len(calls) == 0


def test_tunnel_stop_local_mode(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "test_app",
        "mode": "local",
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
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    res = tunnel_stop("gpu")
    payload = _payload(res)

    assert payload["ok"] is False
    assert payload["error_type"] == "local_mode"
    assert len(calls) == 0
