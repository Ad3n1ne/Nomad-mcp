# nomad

[中文说明](README.zh-CN.md)

nomad is a local MCP server for agentic remote development. It keeps source
code local while an AI agent syncs with `rsync`, runs commands over SSH, manages
long jobs in remote `tmux`, and pulls artifacts back into the project.

For Codex, use the project-scoped Streamable HTTP daemon. Stdio remains
available for compatible clients and one-off use.

## Features

- Multiple remote targets per local project.
- Project-local `.nomad.json` configuration.
- SSH preflight checks and read-only network diagnostics.
- Incremental `rsync` push and guarded artifact pull.
- Short remote commands and long-running `tmux` tasks.
- Optional persistent reverse SSH tunnels.
- Path guards, dangerous-command checks, output limits, and secret redaction.

## Requirements

- Python 3.11+, `ssh`, and `rsync`
- Key-based SSH access to remote targets
- Remote `tmux` when using long-running tasks

Daemon lifecycle management supports macOS, Linux, and other POSIX systems.
Windows is not currently supported or tested.

## Installation

Run the latest PyPI release without installing it globally:

```bash
uvx --from nomad-mcp nomad
```

Or install an isolated global command:

```bash
pipx install nomad-mcp
```

## Codex Setup

Install nomad with `pipx`, then start a daemon in the local project:

```bash
nomad daemon start --project "$PWD"
nomad daemon status --project "$PWD"
```

Generate the project-specific Codex configuration:

```bash
nomad client-config \
  --transport http \
  --project "$PWD" \
  --name nomad-myproject \
  --format toml
```

The generated configuration references a bearer-token environment variable
instead of storing the token. Export it before starting Codex:

```bash
export NOMAD_TOKEN_ENV_VAR="$(nomad daemon status --project "$PWD" |
  python -c 'import json,sys; print(json.load(sys.stdin)["token_env_var"])')"
export "$NOMAD_TOKEN_ENV_VAR=$(nomad daemon token --project "$PWD")"
codex
```

For Codex Desktop on macOS, set the same value with `launchctl setenv`, then
fully quit and reopen Codex:

```bash
launchctl setenv "$NOMAD_TOKEN_ENV_VAR" \
  "$(nomad daemon token --project "$PWD")"
```

See [Persistent MCP Daemon](docs/09-persistent-daemon.md) for direct
`codex mcp add` commands, project isolation, lifecycle operations, upgrades,
security boundaries, and troubleshooting.

### Stdio Compatibility

Clients without Streamable HTTP support can launch nomad directly:

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": ["--from", "nomad-mcp", "nomad"]
    }
  }
}
```

Equivalent TOML:

```toml
[mcp_servers.nomad]
command = "uvx"
args = ["--from", "nomad-mcp", "nomad"]
startup_timeout_sec = 120
```

`nomad client-config` can generate JSON or TOML snippets for both transports.

## Quick Start

1. Start and register the project daemon.
2. Open Codex in the local project.
3. Call `health`, then `init_discover`.
4. Select an SSH target and remote workspace.
5. Save `.nomad.json` with `init_save_config`.
6. Push code with `sync_push`.
7. Use `run_remote` for short commands.
8. Use `task_start` and `task_status` for long jobs.
9. Pull artifacts with `sync_pull`.

Use `run_remote` only for short synchronous work. Downloads, builds, training,
servers, and batch jobs belong in `task_start`. If a call with side effects
times out, inspect its status before retrying it.

## Documentation

- [Project overview](docs/00-overview.md)
- [`.nomad.json` schema and examples](docs/01-schema.md)
- [Tools and workflows](docs/02-tools.md)
- [Network and reverse tunnels](docs/03-network.md)
- [Security model](docs/04-security.md)
- [Context and output limits](docs/05-context-defense.md)
- [Workspace isolation](docs/06-workspace-isolation.md)
- [Persistent MCP daemon](docs/09-persistent-daemon.md)

## Safety

nomad executes commands over SSH and synchronizes files with `rsync`. Use it
only with trusted local projects and remote machines. Its guardrails reduce
risk, but cannot make an untrusted agent or host trustworthy.

## Development

```bash
python -m pip install -e .[dev]
nomad doctor
python -m pytest
python -m compileall -q src tests
```

## License

MIT
