import json
import os
from nomad.server import _safe_resource, _safe_tool, health, mcp_server, get_current_project_resource


def test_server_tools_registered():
    registered_tools = set(mcp_server._tool_manager._tools.keys())
    expected_phase1_tools = {
        "init_discover",
        "init_verify_and_probe",
        "init_save_config",
        "init_probe_target",
        "sync_push",
        "sync_pull",
        "run_remote",
        "tunnel_start",
        "tunnel_status",
        "tunnel_stop",
        "net_diagnose",
        "health",
    }
    expected_phase2_tools = {
        "task_start",
        "task_status",
        "task_list",
        "task_kill",
    }
    assert expected_phase1_tools.issubset(registered_tools)
    assert expected_phase2_tools.issubset(registered_tools)


def test_health_returns_process_metadata(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    res = json.loads(health())

    assert res["ok"] is True
    assert res["tool"] == "health"
    assert res["data"]["pid"] == os.getpid()
    assert res["data"]["cwd"]
    assert res["data"]["version"]
    assert res["data"]["log_path"] == str(log_path)


def test_safe_tool_catches_exception_and_logs_traceback(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    def boom(target: str = "default") -> str:
        raise RuntimeError("kaboom")

    wrapped = _safe_tool(boom)
    res = json.loads(wrapped(target="gpu"))

    assert res["ok"] is False
    assert res["tool"] == "boom"
    assert res["target"] == "gpu"
    assert res["error_type"] == "internal_error"
    assert str(log_path) in res["diagnostics"][1]
    log_content = log_path.read_text(encoding="utf-8")
    assert "tool entry name=boom" in log_content
    assert "tool exception name=boom" in log_content
    assert "RuntimeError: kaboom" in log_content
    assert "Traceback" in log_content


def test_safe_tool_redacts_sensitive_params_from_log(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    def accepts_sensitive(cmd: str, config_json: str, target: str = "default") -> str:
        return json.dumps({"ok": True, "tool": "accepts_sensitive", "target": target})

    wrapped = _safe_tool(accepts_sensitive)
    wrapped(
        "curl -H 'Authorization: Bearer secret-token' https://example.test",
        '{"runtime":{"extra_env":{"API_TOKEN":"supersecret"}}}',
        target="gpu",
    )

    log_content = log_path.read_text(encoding="utf-8")
    assert "secret-token" not in log_content
    assert "supersecret" not in log_content
    assert "Authorization" not in log_content
    assert '"cmd": "<redacted str len=' in log_content
    assert '"config_json": "<redacted str len=' in log_content
    assert '"target": "gpu"' in log_content


def test_safe_resource_catches_exception_and_logs_traceback(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    def broken_resource() -> str:
        raise ValueError("resource broke")

    wrapped = _safe_resource(broken_resource)
    res = json.loads(wrapped())

    assert res["ok"] is False
    assert res["tool"] == "broken_resource"
    assert res["error_type"] == "internal_error"
    log_content = log_path.read_text(encoding="utf-8")
    assert "resource exception name=broken_resource" in log_content
    assert "ValueError: resource broke" in log_content


def test_resource_unconfigured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    res_str = get_current_project_resource()
    res = json.loads(res_str)

    assert res["mode"] == "unconfigured"
    assert "agent_hints" in res
    assert "config_schema" in res
    assert "minimal_remote_template" in res["config_schema"]


def test_resource_invalid_json_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text("{bad json", encoding="utf-8")

    res_str = get_current_project_resource()
    res = json.loads(res_str)

    assert res["mode"] == "invalid_config"
    assert "agent_hints" in res
    assert "config_schema" in res


def test_resource_configured_redacted(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "my_app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/my_app",
                "hardware": {"gpu": "A100", "cpu_cores": 16},
                "runtime": {
                    "extra_env": {
                        "API_TOKEN": "super_secret_value",
                        "NORMAL": "visible_value"
                    }
                }
            }
        }
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    res_str = get_current_project_resource()
    res = json.loads(res_str)

    assert res["project_name"] == "my_app"
    assert res["mode"] == "remote"
    assert res["default_target"] == "gpu"
    assert "gpu" in res["targets"]
    
    target_info = res["targets"]["gpu"]
    assert target_info["ssh_host"] == "myhost"
    assert target_info["remote_path"] == "/workspace/my_app"
    assert target_info["hardware"] == {"gpu": "A100", "cpu_cores": 16}
    assert target_info["extra_env_keys"] == ["API_TOKEN", "NORMAL"]

    # Critical security assertion: secret values MUST NOT be exposed in the resource
    assert "super_secret_value" not in res_str
    assert "visible_value" not in res_str
    assert "agent_hints" in res
    assert "sync_pull" in res["agent_hints"]
    assert "task_start" in res["agent_hints"]
    assert "net_diagnose" in res["agent_hints"]
    assert res["config_schema"]["minimal_remote_template"]["project_name"] == "my_app"
    assert "run_remote" in res["config_schema"]["fields"]["targets.<name>.limits.command_timeout_seconds"]
