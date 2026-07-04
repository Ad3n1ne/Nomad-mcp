"""
Small, MCP-friendly .nomad.json schema hints.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


def get_config_schema_hints(project_name: str = "my_project") -> dict[str, Any]:
    """Returns concise schema guidance for agents creating .nomad.json."""
    return {
        "purpose": ".nomad.json binds one local project to one or more remote targets.",
        "recommended_init_flow": [
            "Call init_discover to inspect local project markers, SSH aliases, and proxy env.",
            "Ask the user for ssh_host, remote_path, and optional local_subpath.",
            "Call init_verify_and_probe(ssh_host, remote_path) before saving when possible.",
            "Call init_save_config with a JSON string matching the template.",
            "Call sync_push, then run_remote or task_start depending on command duration.",
        ],
        "minimal_remote_template": _minimal_remote_template(project_name),
        "fields": {
            "project_name": "Required. Stable project id for tmux session prefixes. Regex: ^[a-zA-Z0-9_-]{1,50}$.",
            "mode": "Required. Use 'remote' for SSH workflow or 'local' to disable all remote operations.",
            "default_target": "Optional for one target; required when multiple targets exist.",
            "targets.<name>": "Target key. Regex: ^[a-zA-Z0-9_-]{1,30}$. Reserved: default, all, local.",
            "targets.<name>.description": "Human purpose of the machine; helps the agent choose among targets.",
            "targets.<name>.ssh_host": "SSH alias from ~/.ssh/config or user@host. No password field.",
            "targets.<name>.remote_path": "Absolute remote workspace under allowed prefixes such as /data/, /workspace/, /home/, /root/, /tmp/, /opt/.",
            "targets.<name>.local_subpath": "Relative local subdirectory to sync, such as 'worker'. Null syncs project root.",
            "targets.<name>.auto_create_remote_path": "Default true. Creates remote_path with mkdir -p during init/save/sync.",
            "targets.<name>.network.jump_host": "Optional SSH jump host alias. Conflicts with use_proxy_for_ssh=true.",
            "targets.<name>.network.reverse_tunnel": "Optional persistent reverse tunnel for letting remote commands use a local proxy.",
            "targets.<name>.sync.respect_gitignore": "Default true. Converts .gitignore into rsync filter rules.",
            "targets.<name>.sync.extra_excludes": "Extra rsync excludes, for example ['*.log', 'tmp/'].",
            "targets.<name>.runtime.interpreter": "Optional runtime path chosen from probe results, for example /opt/conda/envs/app/bin/python.",
            "targets.<name>.runtime.extra_env": "Optional env vars for remote commands. Keys must match ^[A-Z_][A-Z0-9_]*$, values must be strings.",
            "targets.<name>.limits.command_timeout_seconds": "Short run_remote timeout in seconds. Increase for slow commands; use task_start for long jobs.",
            "targets.<name>.limits.max_output_lines": "Output tail line cap.",
            "targets.<name>.limits.max_output_bytes": "Output byte cap.",
        },
        "defaults": {
            "local_subpath": None,
            "auto_create_remote_path": True,
            "network.use_proxy_for_ssh": False,
            "network.jump_host": None,
            "network.reverse_tunnel.enabled": False,
            "network.reverse_tunnel.proxy_scheme": "socks5",
            "sync.respect_gitignore": True,
            "sync.extra_excludes": [],
            "runtime.interpreter": None,
            "runtime.extra_env": {},
            "limits.command_timeout_seconds": 60,
            "limits.max_output_lines": 200,
            "limits.max_output_bytes": 10240,
        },
        "command_duration_guidance": {
            "run_remote": "Use for short commands. Its timeout is limits.command_timeout_seconds.",
            "task_start": "Use for long jobs. It starts tmux and returns immediately; monitor with task_status/task_list.",
        },
    }


def _minimal_remote_template(project_name: str) -> dict[str, Any]:
    safe_project_name = re.sub(r"[^a-zA-Z0-9_-]+", "_", project_name).strip("_")
    safe_project_name = safe_project_name[:50] or "my_project"
    template = {
        "project_name": safe_project_name,
        "mode": "remote",
        "default_target": "main",
        "targets": {
            "main": {
                "description": "Primary remote development target.",
                "ssh_host": "ssh-alias-or-user@host",
                "remote_path": "/data/my_project",
                "local_subpath": None,
                "auto_create_remote_path": True,
                "network": {
                    "use_proxy_for_ssh": False,
                    "jump_host": None,
                    "reverse_tunnel": {
                        "enabled": False,
                        "proxy_scheme": "socks5",
                    },
                },
                "sync": {
                    "respect_gitignore": True,
                    "extra_excludes": [],
                },
                "runtime": {
                    "interpreter": None,
                    "extra_env": {},
                },
                "limits": {
                    "command_timeout_seconds": 60,
                    "max_output_lines": 200,
                    "max_output_bytes": 10240,
                },
            }
        },
    }
    return deepcopy(template)
