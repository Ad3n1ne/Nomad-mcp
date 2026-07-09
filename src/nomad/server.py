"""
FastMCP Server Entry Point.
"""
from __future__ import annotations

import atexit
import functools
import inspect
import json
import os
import sys
import time
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from nomad import __version__
from nomad.config import ConfigError, guard_remote, load_config
from nomad.mcp_logging import (
    format_traceback,
    get_log_path,
    get_mcp_logger,
    log_server_shutdown,
    log_server_startup,
    redact_text,
    summarize_call,
    summarize_result,
)
from nomad.result import failure_result, success_result
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

SERVER_START_TIME = time.time()

mcp_server = FastMCP("nomad")


def _safe_tool(func: Callable[..., str]) -> Callable[..., str]:
    """Wraps an MCP tool so exceptions become structured failures, not transport death."""
    tool_name = func.__name__
    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        logger = get_mcp_logger()
        logger.info(
            "tool entry name=%s params=%s",
            tool_name,
            summarize_call(args, kwargs, signature),
        )
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            target = kwargs.get("target") if isinstance(kwargs.get("target"), str) else None
            exc_summary = redact_text(f"{type(exc).__name__}: {exc}")
            logger.error(
                "tool exception name=%s target=%s exception=%s\n%s",
                tool_name,
                target,
                exc_summary,
                format_traceback(exc),
            )
            return failure_result(
                tool=tool_name,
                target=target,
                message=(
                    f"Internal error in {tool_name}. "
                    f"See Nomad MCP log: {get_log_path()}"
                ),
                error_type="internal_error",
                recoverable=True,
                diagnostics=[exc_summary, f"log_path={get_log_path()}"],
            )
        logger.info("tool exit name=%s result=%s", tool_name, summarize_result(result))
        return result

    wrapper.__signature__ = signature  # type: ignore[attr-defined]
    return wrapper


def _safe_resource(func: Callable[..., str]) -> Callable[..., str]:
    resource_name = func.__name__
    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        logger = get_mcp_logger()
        logger.info(
            "resource entry name=%s params=%s",
            resource_name,
            summarize_call(args, kwargs, signature),
        )
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            exc_summary = redact_text(f"{type(exc).__name__}: {exc}")
            logger.error(
                "resource exception name=%s exception=%s\n%s",
                resource_name,
                exc_summary,
                format_traceback(exc),
            )
            return failure_result(
                tool=resource_name,
                message=(
                    f"Internal error in resource {resource_name}. "
                    f"See Nomad MCP log: {get_log_path()}"
                ),
                error_type="internal_error",
                recoverable=True,
                diagnostics=[exc_summary, f"log_path={get_log_path()}"],
            )
        logger.info("resource exit name=%s result=%s", resource_name, summarize_result(result))
        return result

    wrapper.__signature__ = signature  # type: ignore[attr-defined]
    return wrapper


def _register_tool(func: Callable[..., str]) -> None:
    mcp_server.tool()(_safe_tool(func))


# Register Phase 1 Tools
_register_tool(init_discover)
_register_tool(init_verify_and_probe)
_register_tool(init_save_config)
_register_tool(init_probe_target)
_register_tool(sync_push)
_register_tool(sync_pull)
_register_tool(run_remote)
_register_tool(tunnel_start)
_register_tool(tunnel_status)
_register_tool(tunnel_stop)
_register_tool(net_diagnose)

# Register Phase 2 Tools
_register_tool(task_start)
_register_tool(task_status)
_register_tool(task_list)
_register_tool(task_kill)


def health() -> str:
    """Reports local Nomad MCP server process health."""
    return success_result(
        tool="health",
        message="Nomad MCP server is running.",
        data={
            "pid": os.getpid(),
            "uptime_seconds": round(time.time() - SERVER_START_TIME, 3),
            "cwd": os.getcwd(),
            "version": __version__,
            "python": sys.version.replace("\n", " "),
            "log_path": str(get_log_path()),
        },
    )


_register_tool(health)


@mcp_server.resource("config://current-project")
@_safe_resource
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
    log_server_startup(os.getcwd(), __version__)
    atexit.register(log_server_shutdown)
    mcp_server.run()


if __name__ == "__main__":
    main()
