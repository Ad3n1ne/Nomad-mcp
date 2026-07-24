# nomad

[中文说明](README.zh-CN.md)

nomad is a local MCP server for agentic remote development.

It helps an AI coding agent work with a remote machine while keeping the source
of truth on your local workstation: sync code with `rsync`, run short commands
over SSH, manage long-running jobs in remote `tmux` sessions, diagnose network
issues, and pull generated artifacts back into the local project.

For Codex, the recommended setup is a persistent, project-scoped Streamable
HTTP daemon. Stdio remains available for compatible clients and one-off use.

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

Run the latest PyPI release directly with `uvx`:

```bash
uvx nomad-mcp
```

Run a specific GitHub tag without waiting for PyPI propagation:

```bash
uvx --from git+https://github.com/Ad3n1ne/Nomad-mcp.git@v0.1.1 nomad
```

Or install a release as an isolated global command with `pipx`:

```bash
pipx install nomad-mcp
```

## MCP Client Configuration

### Recommended: persistent HTTP daemon

Start one daemon from each local project:

```bash
nomad daemon start --project "$PWD"
nomad daemon status --project "$PWD"
```

`status` returns the project-specific `url` and `token_env_var`. Bearer token
configuration through that environment variable is recommended instead of
placing the token inline in client configuration. The token command writes only
the secret to stdout so it can be used in command substitution. Treat its output
as a credential and do not log it.

Generate a Codex TOML snippet that references the environment variable:

```bash
nomad client-config \
  --transport http \
  --project "$PWD" \
  --name nomad-myproject \
  --format toml
```

To register the same endpoint with the Codex CLI, first read the non-secret
endpoint metadata from `status`:

```bash
NOMAD_PROJECT="$PWD"
NOMAD_STATUS="$(nomad daemon status --project "$NOMAD_PROJECT")"
NOMAD_URL="$(python -c 'import json,sys; print(json.load(sys.stdin)["url"])' <<<"$NOMAD_STATUS")"
NOMAD_TOKEN_ENV_VAR="$(python -c 'import json,sys; print(json.load(sys.stdin)["token_env_var"])' <<<"$NOMAD_STATUS")"
codex mcp add nomad-myproject \
  --url "$NOMAD_URL" \
  --bearer-token-env-var "$NOMAD_TOKEN_ENV_VAR"
```

For Codex CLI, or when launching Codex from a terminal, export the token in that
same shell before starting Codex:

```bash
export "$NOMAD_TOKEN_ENV_VAR=$(nomad daemon token --project "$NOMAD_PROJECT")"
codex
```

For Codex Desktop on macOS, put the variable into the current GUI login session:

```bash
launchctl setenv "$NOMAD_TOKEN_ENV_VAR" "$(nomad daemon token --project "$NOMAD_PROJECT")"
```

Then fully quit Codex Desktop and reopen it so the new process inherits the
variable. The `launchctl` value belongs to the current login session and may
need to be set again after logging out or restarting the Mac.

Each local project receives a stable high port, its own token environment
variable, and its own daemon state. Give every project a distinct MCP name, such
as `nomad-api` and `nomad-dataset`, and register each project's reported URL.

Manage the daemon with:

```bash
nomad daemon status --project "$PWD"
nomad daemon restart --project "$PWD"
nomad daemon stop --project "$PWD"
```

After upgrading nomad, restart every running project daemon so the persistent
process loads the new code.

The default bind address is loopback-only. A non-loopback bind requires
`--allow-remote` and bearer authentication remains mandatory. Exposing Nomad
outside the local machine grants access to tools that can sync files and execute
remote commands, so keep it on loopback unless the network and clients are
trusted.

### Compatible stdio mode

Stdio remains the default output of `client-config` for backward compatibility
and for clients that do not support Streamable HTTP.

Recommended PyPI no-install configuration:

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

For the latest GitHub tag:

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Ad3n1ne/Nomad-mcp.git@v0.1.1",
        "nomad"
      ]
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
nomad client-config --runner github
nomad client-config --runner nomad --format toml
nomad client-config --transport stdio --name nomad
```

## Quick Start

1. Start and register the project HTTP daemon as shown above.
2. Open Codex in the local project directory.
3. Ask it to call `health` before the first Nomad tool use.
4. Ask it to call `init_discover`.
5. Choose an SSH target and remote workspace path.
6. Ask it to save a `.nomad.json` config with `init_save_config`.
7. Push code with `sync_push`.
8. Run short commands with `run_remote`.
9. Run long jobs with `task_start`, then monitor them with `task_status` or `task_list`.
10. Pull remote artifacts with `sync_pull`.

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

## Codex Usage Guardrails

- Call `health` before the first Nomad tool call in each Codex task.
- Use `run_remote` only for short synchronous probes and commands.
- Use `task_start` for uploads, builds, training, servers, scans, or batch work.
- Moving from stdio to the persistent HTTP daemon prevents a broken Codex stdio
  child transport from taking the Nomad server and its state down with it.
- HTTP cannot prevent every disconnect: Codex, the local network stack, or the
  daemon can still restart. Reconnect the client, check `daemon status`, and
  restart the daemon only if it is not healthy.
- For legacy stdio mode, if the outer client reports `Transport closed`, stop
  retrying in that task and restart its MCP transport. To clear stale
  Codex-spawned stdio processes locally, run:

```bash
nomad doctor --kill-stale-mcp
```

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
nomad doctor --kill-stale-mcp --dry-run
python -m pytest
python -m compileall -q src tests
```

## License

MIT
