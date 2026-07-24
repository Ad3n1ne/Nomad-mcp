# Persistent MCP Daemon

The recommended Codex transport is one authenticated Streamable HTTP daemon per
local project. It stays alive independently of an individual Codex task, so a
broken stdio child transport does not terminate Nomad and its state.

## Start and Inspect

Install the command with `pipx`, then run:

```bash
nomad daemon start --project "$PWD"
nomad daemon status --project "$PWD"
```

`status` reports the project-specific URL, token environment variable, PID,
version, and log path. Retrieve the secret only when assigning it:

```bash
NOMAD_PROJECT="$PWD"
NOMAD_STATUS="$(nomad daemon status --project "$NOMAD_PROJECT")"
NOMAD_URL="$(python -c 'import json,sys; print(json.load(sys.stdin)["url"])' \
  <<<"$NOMAD_STATUS")"
NOMAD_TOKEN_ENV_VAR="$(python -c \
  'import json,sys; print(json.load(sys.stdin)["token_env_var"])' \
  <<<"$NOMAD_STATUS")"
```

Treat `nomad daemon token` output as a credential. Do not log it or place it
inline in client configuration.

## Register with Codex

Add the endpoint:

```bash
codex mcp add nomad-myproject \
  --url "$NOMAD_URL" \
  --bearer-token-env-var "$NOMAD_TOKEN_ENV_VAR"
```

For Codex CLI, export the token in the shell that starts Codex:

```bash
export "$NOMAD_TOKEN_ENV_VAR=$(nomad daemon token --project "$NOMAD_PROJECT")"
codex
```

For Codex Desktop on macOS, add it to the current GUI login session:

```bash
launchctl setenv "$NOMAD_TOKEN_ENV_VAR" \
  "$(nomad daemon token --project "$NOMAD_PROJECT")"
```

Fully quit and reopen Codex Desktop afterward. The `launchctl` value belongs to
the current login session and may need to be restored after logout or restart.

The same configuration can be generated without manually reading status:

```bash
nomad client-config \
  --transport http \
  --project "$NOMAD_PROJECT" \
  --name nomad-myproject \
  --format toml
```

## Multiple Projects

Each project receives a persisted high port, token environment variable, and
daemon state. Nomad selects ports deterministically while avoiding active and
reserved ports. Register each project under a distinct MCP name, such as
`nomad-api` or `nomad-dataset`.

## Lifecycle

```bash
nomad daemon status --project "$NOMAD_PROJECT"
nomad daemon restart --project "$NOMAD_PROJECT"
nomad daemon stop --project "$NOMAD_PROJECT"
```

Restart running daemons after upgrading nomad so they load the new code. The
daemon is not currently installed as an operating-system login service, so it
must be started again after a machine reboot.

## Security Boundary

- HTTP listens only on loopback addresses.
- Every project has an independent bearer token.
- State, logs, locks, ports, and tokens use owner-only permissions.
- Tokens are excluded from state and logs.
- Tool exceptions and tracebacks are logged with secret redaction.
- Remote non-loopback binds remain disabled until TLS transport is supported.

## Troubleshooting

Call `health` before the first Nomad operation in a Codex task. If the client
disconnects, check the daemon before restarting it:

```bash
nomad daemon status --project "$NOMAD_PROJECT"
```

After upgrading, changing the token environment, or changing MCP configuration,
fully restart Codex so it establishes a new connection.

For legacy stdio mode, clear Codex-spawned stale processes with:

```bash
nomad doctor --kill-stale-mcp
```

HTTP transport removes the stdio child-process failure mode, but cannot prevent
every client, network-stack, or daemon restart. Calls with side effects should
not be retried until their remote or task status has been checked.
