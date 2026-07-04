import json
import asyncio
from nomad.server import mcp_server, get_current_project_resource


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
    }
    expected_phase2_tools = {
        "task_start",
        "task_status",
        "task_list",
        "task_kill",
    }
    assert expected_phase1_tools.issubset(registered_tools)
    assert expected_phase2_tools.issubset(registered_tools)


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
