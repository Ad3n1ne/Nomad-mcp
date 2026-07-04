"""
FastMCP Server Entry Point.
"""
from __future__ import annotations

import json
from mcp.server.fastmcp import FastMCP

from nomad.config import ConfigError, guard_remote, load_config
from nomad.tools.commands import run_remote
from nomad.tools.init import (
    init_discover,
    init_probe_target,
    init_save_config,
    init_verify_and_probe,
)
from nomad.tools.network import net_diagnose, tunnel_start, tunnel_status, tunnel_stop
from nomad.schema import get_config_schema_hints
from nomad.tools.sync import sync_pull, sync_push
from nomad.tools.tasks import task_kill, task_list, task_start, task_status

mcp_server = FastMCP("nomad")

# Register Phase 1 Tools
mcp_server.tool()(init_discover)
mcp_server.tool()(init_verify_and_probe)
mcp_server.tool()(init_save_config)
mcp_server.tool()(init_probe_target)
mcp_server.tool()(sync_push)
mcp_server.tool()(sync_pull)
mcp_server.tool()(run_remote)
mcp_server.tool()(tunnel_start)
mcp_server.tool()(tunnel_status)
mcp_server.tool()(tunnel_stop)
mcp_server.tool()(net_diagnose)

# Register Phase 2 Tools
mcp_server.tool()(task_start)
mcp_server.tool()(task_status)
mcp_server.tool()(task_list)
mcp_server.tool()(task_kill)


@mcp_server.resource("config://current-project")
def get_current_project_resource() -> str:
    """Returns a sanitized summary of current project config and agent hints."""
    try:
        config = load_config()
    except ConfigError:
        return json.dumps(
            {
                "mode": "invalid_config",
                "agent_hints": "The project configuration file .nomad.json is invalid or corrupted. Run init_discover or fix the file.",
                "config_schema": get_config_schema_hints(),
            },
            indent=2,
        )

    remote_guard = guard_remote(config)
    if remote_guard == "unconfigured":
        return json.dumps(
            {
                "mode": "unconfigured",
                "agent_hints": "Project is unconfigured. Call 'init_discover' to probe local SSH environment.",
                "config_schema": get_config_schema_hints(),
            },
            indent=2,
        )

    mode = config.get("mode", "unconfigured")
    project_name = config.get("project_name", "unnamed")
    default_target = config.get("default_target", "default")

    sanitized_targets = {}
    targets = config.get("targets") or {}
    for target_name, target_cfg in targets.items():
        if not isinstance(target_cfg, dict):
            continue
        extra_env = (target_cfg.get("runtime") or {}).get("extra_env") or {}
        extra_env_keys = sorted(list(extra_env.keys()))
        sanitized_targets[target_name] = {
            "description": target_cfg.get("description", ""),
            "ssh_host": target_cfg.get("ssh_host", ""),
            "remote_path": target_cfg.get("remote_path", ""),
            "hardware": target_cfg.get("hardware") or {},
            "extra_env_keys": extra_env_keys,
        }

    hints = (
        "In 'remote' mode, push code with 'sync_push' before 'run_remote'. "
        "Use 'task_start' for long-running commands, 'task_status'/'task_list' to monitor them, "
        "and 'sync_pull' to retrieve remote artifacts. "
        "If network issues or proxies are involved, use 'net_diagnose' and the tunnel tools."
        if mode == "remote"
        else "Project is in 'local' mode. All commands run locally; remote sync and tunnels are disabled."
    )

    payload = {
        "project_name": project_name,
        "mode": mode,
        "default_target": default_target,
        "targets": sanitized_targets,
        "agent_hints": hints,
        "config_schema": get_config_schema_hints(project_name),
    }
    return json.dumps(payload, indent=2)


def main():
    """Server CLI Entry."""
    mcp_server.run()


if __name__ == "__main__":
    main()
