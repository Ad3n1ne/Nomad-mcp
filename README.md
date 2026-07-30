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

`nomad codex setup` is an optional Codex configuration adapter, not a protocol
requirement. Any MCP host can use Nomad through the standard stdio or
Streamable HTTP transports without Codex configuration.

From the project you want Codex to control, run:

```bash
nomad codex setup --project "$PWD"
nomad codex doctor --project "$PWD"
```

The adapter starts or repairs a project daemon and writes only the trusted
project's `.codex/config.toml`. It never changes user-level Codex configuration
or project trust. Resolve any reported global conflict, then fully restart
Codex so it loads the project MCP entry.

Other MCP hosts can launch Nomad directly over stdio:

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

Use `nomad client-config` to generate JSON or TOML for standard stdio or
Streamable HTTP clients. See [Persistent MCP Daemon](docs/09-persistent-daemon.md)
for manual registration, lifecycle, security, and troubleshooting.

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
