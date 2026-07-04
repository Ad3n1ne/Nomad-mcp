# nomad

[中文说明](README.zh-CN.md)

nomad is a local MCP server for agentic remote development.

It helps an AI coding agent work with a remote machine while keeping the source
of truth on your local workstation: sync code with `rsync`, run short commands
over SSH, manage long-running jobs in remote `tmux` sessions, diagnose network
issues, and pull generated artifacts back into the local project.

Any MCP-enabled agent environment that can start a stdio server with a command
and arguments can use nomad.

## Features

- Multi-target remote workspaces per local project.
- Project-local `.nomad.json` configuration with schema hints exposed through MCP.
- SSH preflight checks and read-only network diagnostics.
- Incremental `rsync` push with `.gitignore` conversion and `--delete` dry-run protection.
- Remote artifact pull into project-owned local directories.
- Short remote command execution with output truncation.
- Long-running remote task management through `tmux`.
- Optional persistent reverse SSH tunnel for sharing a local proxy with remote jobs.
- Path guards, dangerous-command checks, and secret redaction for safer agent workflows.

## Requirements

- Python 3.11+
- `ssh`
- `rsync`
- `tmux` on remote machines when using long-running tasks
- Key-based SSH access to your remote targets

## Installation

Run directly with `uvx`:

```bash
uvx nomad-mcp
```

Or install it as an isolated global command with `pipx`:

```bash
pipx install nomad-mcp
```

## MCP Client Configuration

Use nomad as a stdio MCP server. The exact config file depends on your client.

Recommended no-install configuration:

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": ["nomad-mcp"]
    }
  }
}
```

For TOML-based clients:

```toml
[mcp_servers.nomad]
command = "uvx"
args = ["nomad-mcp"]
startup_timeout_sec = 120
```

If you installed with `pipx`, use the installed command instead:

```json
{
  "mcpServers": {
    "nomad": {
      "command": "nomad",
      "args": []
    }
  }
}
```

You can also print config snippets with:

```bash
nomad client-config
nomad client-config --runner nomad --format toml
```

## Quick Start

1. Open an MCP-enabled agent in your local project directory.
2. Ask it to call `init_discover`.
3. Choose an SSH target and remote workspace path.
4. Ask it to save a `.nomad.json` config with `init_save_config`.
5. Push code with `sync_push`.
6. Run short commands with `run_remote`.
7. Run long jobs with `task_start`, then monitor them with `task_status` or `task_list`.
8. Pull remote artifacts with `sync_pull`.

## Example `.nomad.json`

```json
{
  "project_name": "my_project",
  "mode": "remote",
  "default_target": "devbox",
  "targets": {
    "devbox": {
      "description": "Primary remote development machine",
      "ssh_host": "devbox",
      "remote_path": "/data/my_project",
      "local_subpath": null,
      "auto_create_remote_path": true,
      "network": {
        "use_proxy_for_ssh": false,
        "jump_host": null,
        "reverse_tunnel": {
          "enabled": false,
          "proxy_scheme": "socks5"
        }
      },
      "sync": {
        "respect_gitignore": true,
        "extra_excludes": []
      },
      "runtime": {
        "interpreter": null,
        "extra_env": {}
      },
      "limits": {
        "command_timeout_seconds": 60,
        "max_output_lines": 200,
        "max_output_bytes": 10240
      }
    }
  }
}
```

`run_remote` uses `limits.command_timeout_seconds`. For downloads, builds,
training, fuzzing, and other slow work, prefer `task_start` so the job runs in a
remote tmux session and can be checked later.

## Tools

- `init_discover`: inspect the local workspace, SSH aliases, and proxy settings.
- `init_verify_and_probe`: verify SSH reachability and probe remote hardware/runtimes.
- `init_save_config`: validate and save `.nomad.json`.
- `init_probe_target`: refresh hardware/runtime information for a target.
- `sync_push`: push local code to the remote workspace.
- `sync_pull`: pull a remote file or directory into local `remote_artifacts/`.
- `run_remote`: run a short command in the remote workspace.
- `task_start`: start a long-running tmux task.
- `task_status`: inspect one task and return a log tail.
- `task_list`: list project-owned tasks across targets.
- `task_kill`: stop a task without deleting its logs.
- `net_diagnose`: run read-only SSH/network diagnostics.
- `tunnel_start`, `tunnel_status`, `tunnel_stop`: manage persistent reverse tunnels.

## Safety Notes

nomad can execute commands over SSH and synchronize files with `rsync`. Use it
only with trusted local projects and trusted remote machines.

The server includes guardrails such as local/remote path checks, dangerous-command
blocking, `.nomad.json` sync exclusion, secret redaction, output truncation, and
`rsync --delete` dry-run protection. These guardrails reduce risk, but they do not
turn an untrusted agent or remote machine into a trusted one.

## Development

```bash
python -m pip install -e .[dev]
nomad --version
nomad doctor
python -m pytest
python -m compileall -q src tests
```

## License

MIT
