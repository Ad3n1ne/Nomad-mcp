# nomad Implementation Spec

This document is the pre-coding implementation contract. The goal is not to add more features, but to pin down the boundaries, failure modes, and extension points ahead of time, so the MCP tools don't become brittle in real Agent calls.

---

## Design Principles

1. **Local is the control plane, remote is the execution plane**
   - Config reading, parameter validation, security circuit-breaking, audit logging, and output truncation all happen in the local Python layer.
   - The remote only executes commands that have been locally validated and escaped.

2. **Every remote operation must have a project context**
   - Without `.nomad.json`, reject `run_remote`, `sync_push`, `sync_pull`, `task_*`.
   - With `mode=local`, reject all remote operations.
   - `target` must be resolved to a concrete target via `resolve_target`; tools are not allowed to assemble the config themselves.

3. **Tool returns must be recoverable for the Agent**
   - Errors must tell the Agent what went wrong, whether it's retryable, and which tool to call next.
   - Long output must be truncated, but the truncation message must include follow-up investigation suggestions.

4. **Security policy is conservative by default**
   - The blacklist is the last line of circuit-breaking, not the only defense.
   - Paths, target names, session names, and env keys must all be validated before use.
   - All shell parameters must be constructed structurally or via `shlex.quote`.

5. **An MVP may simplify, but must state its simplification boundaries explicitly**
   - For example, `.gitignore` → rsync conversion handles common semantics first; do not claim full Git compatibility.
   - High-risk operations are rejected or dry-run first; automation comes later.

---

## Unified Return Contract

MCP tools may return strings externally, but internally they should uniformly build a structured result, then serialize it to a JSON string. This is both human-readable and easy for the Agent to reason about the next step.

```json
{
  "ok": true,
  "tool": "run_remote",
  "target": "gpu",
  "message": "Command completed.",
  "data": {},
  "diagnostics": [],
  "next_action": null
}
```

Failure return:

```json
{
  "ok": false,
  "tool": "sync_push",
  "target": "gpu",
  "error_type": "ssh_timeout",
  "message": "SSH preflight failed before rsync.",
  "details": {
    "ssh_host": "gpu-host",
    "timeout_seconds": 3
  },
  "recoverable": true,
  "next_action": {
    "tool": "net_diagnose",
    "args": {"target": "gpu"}
  }
}
```

### `error_type` enum

| Type | Meaning | Recoverable |
|---|---|---|
| `unconfigured` | No `.nomad.json` in the current directory | Yes |
| `local_mode` | Project configured as pure local mode | No |
| `invalid_config` | Config field missing or invalid | Yes |
| `target_not_found` | The specified target does not exist | Yes |
| `unsafe_local_cwd` | The current local directory is a sensitive directory | No |
| `unsafe_remote_path` | Remote path not in the whitelist or insufficient depth | Yes |
| `dangerous_command` | Command hit a security circuit-break rule | No |
| `interactive_command` | Command looks interactive | Yes |
| `ssh_timeout` | SSH connection timed out | Yes |
| `ssh_auth_failed` | SSH authentication failed | Yes |
| `ssh_host_key_failed` | Host key verification failed | Yes |
| `ssh_connection_refused` | SSH target port refused the connection | Yes |
| `ssh_unknown_failure` | SSH probe failed but cannot be categorized | Yes |
| `ssh_proxy_unavailable` | Config requires SSH via proxy, but no usable local proxy detected | Yes |
| `tunnel_start_failed` | Persistent reverse tunnel failed to start | Yes |
| `tunnel_not_running` | Target needs a proxy but the persistent tunnel isn't running | Yes |
| `tunnel_port_in_use` | Remote bind port already occupied | Yes |
| `command_timeout` | Synchronous remote command exceeded the target timeout | Yes |
| `path_traversal` | A relative-path parameter contains an absolute path, `..`, or a null byte | No |
| `remote_command_failed` | Remote command returned non-zero | Depends |
| `rsync_failed` | rsync returned non-zero | Depends |
| `task_exists` | A tmux task with the same name already exists | Yes |
| `task_not_found` | The specified task does not exist | Yes |
| `internal_error` | Uncategorized exception | Yes |

---

## Initialization State Machine

Initialization is not a single action; it's a recoverable state machine.

```text
unconfigured
  -> discovered
  -> ssh_verified
  -> runtime_selected
  -> config_saved
  -> ready
```

### State definitions

| State | File state | Allowed operations |
|---|---|---|
| `unconfigured` | No `.nomad.json` | Only `init_discover`, `init_verify_and_probe`, `init_save_config` |
| `discovered` | Config not yet written; local probe results available | Continue asking for target info |
| `ssh_verified` | A target has been verified | Choose a runtime; fill in description and sync paths |
| `runtime_selected` | A complete config draft is formed | Call `init_save_config` |
| `config_saved` | `.nomad.json` has been written | Auto reload and validate |
| `ready` | Config is valid and `mode=remote` | Remote operations allowed |

### Config write rules

- Before `init_save_config` overwrites an existing `.nomad.json`, it must write `.nomad.json.bak`.
- `.nomad.json.bak` and `.nomad.local.json` are treated as sensitive files and must be excluded by `.gitignore`.
- A `mode=local` config may omit `targets` but must include `project_name`.
- `mode=remote` must include at least one target.
- With a single target and `default_target=null`, `resolve_target("default")` automatically falls back to the only target.
- With multiple targets, `default_target` must exist.
- `local_subpath` must be a relative path; `..`, absolute paths, and null bytes are forbidden.
- `project_name` and target names allow only `[a-zA-Z0-9_-]`.

### Default value normalization

After reading the config, the internal code uses a normalized config; users are not required to write every optional field.

| Field | Default |
|---|---|
| `default_target` | May be `null` for a single target; resolution falls back to the only target |
| `targets.<name>.description` | Empty string, but the init flow should encourage the user to fill it in |
| `targets.<name>.local_subpath` | `null` |
| `targets.<name>.auto_create_remote_path` | `true` |
| `targets.<name>.network.use_proxy_for_ssh` | `false` |
| `targets.<name>.network.jump_host` | `null` |
| `targets.<name>.network.reverse_tunnel.enabled` | `false` |
| `targets.<name>.network.reverse_tunnel.proxy_scheme` | `"socks5"` |
| `targets.<name>.sync.respect_gitignore` | `true` |
| `targets.<name>.sync.extra_excludes` | `[]` |
| `targets.<name>.runtime.interpreter` | `null` |
| `targets.<name>.runtime.extra_env` | `{}` |
| `targets.<name>.limits.command_timeout_seconds` | `60` |
| `targets.<name>.limits.max_output_lines` | `200` |
| `targets.<name>.limits.max_output_bytes` | `10240` |

---

## MCP Exposure Surface

### Tools

Phase 1 exposes:

- `init_discover`
- `init_verify_and_probe`
- `init_save_config`
- `init_probe_target`
- `sync_push`
- `run_remote`
- `tunnel_start`
- `tunnel_status`
- `tunnel_stop`

Phase 2 exposes:

- `task_start`
- `task_status`
- `task_list`
- `task_kill`

Phase 3/4 exposes:

- `net_diagnose`
- `sync_pull`

### Resources

Must expose:

- `config://current-project`

The returned content should include:

- `mode`
- `project_name`
- `default_target`
- Target name, description, remote_path, hardware summary
- Execution hints for the Agent, e.g. whether to `sync_push` before running remote commands

Must not expose:

- The full sensitive values of `runtime.extra_env`
- Key contents beyond the SSH private-key path
- Unnecessary personal info from the user's local absolute paths

---

## Config Validation Details

### `project_name`

- Regex: `^[a-zA-Z0-9_-]{1,50}$`
- Used as the prefix for audit log and task sessions.

### `targets.<name>`

- Regex: `^[a-zA-Z0-9_-]{1,30}$`
- Must not collide with reserved words: `default`, `all`, `local`.

### `remote_path`

- Must be a POSIX absolute path.
- Must start with one of the following prefixes:
  - `/home/`
  - `/root/`
  - `/workspace/`
  - `/data/`
  - `/tmp/`
  - `/opt/`
- When used with `rsync --delete`, the path depth must be greater than or equal to 3.
- The remote path must not be a user's home root, e.g. `/root`, `/home/user`.

### Local CWD

- Running remote operations in system-sensitive directories is forbidden: `/`, `/etc`, `/usr`, `/bin`, `/sbin`, `/lib`, `/sys`, `/proc`, `/dev`.
- CWD validation runs before `init_save_config`, `run_remote`, `sync_push`, `sync_pull`, `task_*`.
- If CWD hits a sensitive directory, return `unsafe_local_cwd`.

### `runtime.extra_env`

- Key regex: `^[A-Z_][A-Z0-9_]*$`
- Value must be a string.
- In the audit log, if a key contains `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, or `AUTH`, the value must be redacted.

### `network`

- `jump_host` and `use_proxy_for_ssh=true` are mutually exclusive in the MVP; configuring both returns `invalid_config`.
- When `use_proxy_for_ssh=true`, a proxy port or proxy URL must be obtainable from the local network snapshot; otherwise return `ssh_proxy_unavailable`.
- Proxy URLs must be parsed by scheme; the MVP supports `socks5://`, `socks4://`, and a bare local-port form. Other schemes return `ssh_proxy_unavailable`; do not generate a speculative ProxyCommand.
- `reverse_tunnel.enabled=true` means the target allows and expects to use a persistent reverse tunnel so that remote commands and long tmux tasks can reach the local proxy.
- `reverse_tunnel.proxy_scheme` supports `socks5` and `http`; defaults to `socks5`.
- `reverse_tunnel.local_proxy_port` and `reverse_tunnel.remote_bind_port` must be integers in 1-65535; `remote_bind_port < 1024` is rejected by default to avoid needing remote privileges.
- The reverse tunnel bind address must be the remote `127.0.0.1`; exposing it on `0.0.0.0` is forbidden.

---

## Command Execution Spec

Execution order of `run_remote(cmd, target="default")`:

1. `load_config`
2. `guard_remote`
3. `resolve_target`
4. `verify_local_cwd_safety`
5. `verify_remote_path_safety`
6. `check_interactive_command`
7. `check_dangerous_command`
8. SSH preflight
9. Build env exports
10. Execute `cd remote_path && env && cmd`
11. Set a local subprocess timeout per `limits.command_timeout_seconds`
12. Truncate output
13. Write audit log

### Shell construction rules

- `ssh_host` and `jump_host` are passed as subprocess argv items, not via shell concatenation.
- `remote_path` uses `shlex.quote`.
- Env keys are regex-validated; env values use `shlex.quote`.
- The user-supplied `cmd` is a free-form shell fragment; it can only be appended as the final segment after passing security checks.

### SSH argv builder

All SSH calls must go through the same builder; do not hand-write SSH parameters inside individual tools.

Base parameters:

```text
ssh
-o ControlMaster=auto
-o ControlPath=/tmp/nomad_ssh_%C
-o ControlPersist=60s
-o ConnectTimeout=<timeout>
-o BatchMode=yes
```

Network parameters:

- When `jump_host` is present, append `-J <jump_host>`.
- When `use_proxy_for_ssh=true`, append `-o ProxyCommand=...`; the ProxyCommand is structurally generated from the local proxy snapshot.
- In the MVP, `jump_host` and `use_proxy_for_ssh=true` are mutually exclusive.
- Ordinary SSH commands do not carry an ad-hoc `-R`. When a target has `reverse_tunnel.enabled=true`, manage the persistent tunnel first via `tunnel_start`/`tunnel_status`, then inject the proxy environment into the remote command.

Reverse tunnel constraints:

- For `run_remote`, if the target has `reverse_tunnel.enabled`, ensure the persistent tunnel is running before injecting the proxy environment.
- `task_start` must not assume a short-lived `-R` can keep serving a detached tmux long task.
- Long-task proxying must rely on the persistent reverse tunnel established by `tunnel_start`; if the target config enables the reverse tunnel but the tunnel isn't running, `task_start` should auto-start the tunnel first; on failure return `tunnel_start_failed`.

---

## Persistent Reverse Tunnel Spec

The persistent reverse tunnel solves exactly one problem: **SSH is already reachable, but remote commands or long tasks need to reuse the local proxy to reach the external network.** It does not handle complex network diagnostics, nor does it change the SSH-reachability precondition.

### Tools

- `tunnel_start(target="default")`
- `tunnel_status(target="default")`
- `tunnel_stop(target="default")`

### Implementation

Uses a dedicated SSH master connection to carry the reverse tunnel; it does not reuse the ControlMaster socket of ordinary command execution.

```text
ssh
-f
-N
-M
-S /tmp/nomad_tunnel_<hash>
-o ExitOnForwardFailure=yes
-o ServerAliveInterval=30
-o ServerAliveCountMax=3
-R 127.0.0.1:<remote_bind_port>:127.0.0.1:<local_proxy_port>
<ssh_host>
```

Requirements:

- `<hash>` is derived from project, target, ssh_host, local_proxy_port, and remote_bind_port, to avoid over-long paths and cross-project collisions.
- `-R` must bind the remote `127.0.0.1`.
- Before starting, use a remote command to check whether `remote_bind_port` is already occupied; if it is, return `tunnel_port_in_use`.
- `tunnel_status` checks the master via `ssh -S /tmp/nomad_tunnel_<hash> -O check <ssh_host>`, optionally with a remote `nc -z 127.0.0.1 <remote_bind_port>` to verify the port.
- `tunnel_stop` closes the master via `ssh -S /tmp/nomad_tunnel_<hash> -O exit <ssh_host>`.

### Remote proxy environment

When the tunnel is running, nomad generates the proxy environment for remote commands:

| `proxy_scheme` | Injected environment |
|---|---|
| `socks5` | `ALL_PROXY=socks5://127.0.0.1:<remote_bind_port>` |
| `http` | `HTTP_PROXY=http://127.0.0.1:<remote_bind_port>` and `HTTPS_PROXY=http://127.0.0.1:<remote_bind_port>` |

Injection rules:

- `run_remote`: If the target has `reverse_tunnel.enabled`, ensure the tunnel is running before execution; on failure return a tunnel error.
- `task_start`: If the target has `reverse_tunnel.enabled`, ensure the tunnel is running before launching the tmux task, and write the proxy environment into the task script.
- The user's explicit `runtime.extra_env` takes precedence over the tunnel's auto-injected vars; on conflict, return a diagnostics reminder.

### Lifecycle boundaries

- The tunnel is target-level state, not task-level state.
- `task_kill` does not stop the tunnel automatically, because other tasks may still be using it.
- `tunnel_stop` does not kill tasks; it only closes the proxy channel.
- After the MCP process restarts, `tunnel_status` must still be able to detect an existing tunnel via its control socket.

---

## Interactive Command Interception

Bare commands hitting the list below are rejected:

- `vim`, `vi`, `nvim`, `nano`, `emacs`
- `top`, `htop`, `btop`
- `less`, `more`, `man`
- `mysql`, `psql`
- Bare `python`, `ipython`, `node`

Allowed:

- `python script.py`
- `python -m pytest`
- `node script.js`

---

## Sync Spec

Execution order of `sync_push(target="default")`:

1. Load and validate the config.
2. Resolve the target.
3. Validate the local CWD and `local_subpath`.
4. Validate the remote path whitelist and depth.
5. SSH preflight.
6. If `auto_create_remote_path=true`, execute `mkdir -p remote_path`.
7. Build the rsync filter file.
8. Execute rsync.
9. Return a sync summary.
10. Write the audit log.

### Built-in excludes

No matter how `.gitignore` is written, always exclude:

```text
.git/
.DS_Store
__pycache__/
*.pyc
*.pyo
.idea/
.vscode/
node_modules/
.pytest_cache/
*.egg-info/
dist/
build/
.mypy_cache/
.ruff_cache/
.nomad.json
```

### `.gitignore` support boundaries

The MVP supports:

- Ignoring blank lines and comments.
- Converting ordinary rules into rsync excludes.
- Converting `!pattern` into rsync includes.
- Preserving directory rules like `foo/` as directory exclusions.
- Treating root rules `/foo` as project-root-anchored.

The MVP does not commit to fully supporting:

- Merging nested subdirectory `.gitignore` files.
- All of Git's `**` edge semantics.
- The parent-directory auto-include inference that negation rules require.

The implementation must state the compatibility scope in the README or in tool returns. A more complete gitignore parser may be introduced separately later.

### `--delete` guard

- Phase 1 allows `--delete` by default, but it must pass the remote path depth check.
- If a dry-run deletion count exceeds 50, Phase 3 onward should refuse auto-execution and return a summary requiring confirmation.
- Deleting the root directory, a home root directory, or any path outside the whitelist is always rejected.

### `sync_pull` path safety

- `remote_relative_path` must be a relative path.
- Absolute paths, `..`, null bytes, and shell metacharacter injection are forbidden.
- `local_dest` defaults to `{cwd}/remote_artifacts/{target_name}/`.
- If the user specifies `local_dest`, it must resolve to inside the current project directory; writing outside the project is forbidden.
- `sync_pull` does not read file contents; it only returns the transfer result, file sizes, and the local save path.

---

## Long Task Spec

Task files are recommended to live under the remote project directory:

```text
{remote_path}/.nomad/tasks/{session}.sh
{remote_path}/.nomad/tasks/{session}.log
{remote_path}/.nomad/tasks/{session}.exit_code
```

Only if the remote directory is not writable does it fall back to `/tmp/nomad/{project_name}/`.

### Session naming

```text
{project_name}_{target_name}_{task_name}
```

- `task_name` regex: `^[a-z0-9_-]{1,40}$`
- Full session name length must not exceed 100.

### Status enum

| Status | How it's determined |
|---|---|
| `running` | tmux session exists |
| `finished_success` | session doesn't exist and exit code is 0 |
| `finished_error` | session doesn't exist and exit code is non-zero |
| `missing` | session, log, and exit code all absent |
| `unknown` | SSH failed or state files are inconsistent |

### `task_start`

- If a session with the same name already exists, return `task_exists`; do not launch a duplicate.
- The command must pass security checks.
- Locally Base64-encode the command; decode it into a script remotely.
- The script content must include:
  - `cd remote_path`
  - env exports
  - the user's command
  - exit code write-out

### `task_kill`

- By default, only kills the current session.
- A `force=true` to kill the process group may be added later, but not in the MVP.

---

## Network Diagnostics Spec

`net_diagnose(target="default")` outputs a structured diagnostic report:

- `ssh -G` parse result: hostname, port, user, proxyjump, proxycommand, identityfile.
- When HostName is a domain, record the DNS resolution result.
- Direct port test: `nc -z -w 3 host port`.
- If local proxy environment variables are detected, record the proxy URL but hide username/password.
- SSH batch test: `ssh -o BatchMode=yes -o ConnectTimeout=5 host "echo ok"`.
- Classify by stderr:
  - timeout
  - permission denied
  - host key failed
  - no route to host
  - connection refused

Network diagnostics do not modify the config and do not establish reverse tunnels; they only give advice.

---

## Audit and Redaction

Audit log path:

```text
~/.nomad/audit.log
```

Records:

- Time
- project_name
- target
- action_type
- sanitized command or summary
- returncode
- error_type

Redaction rules:

- Env var values are redacted by key.
- Usernames/passwords inside URLs are redacted.
- SSH private key contents are not recorded.
- The full text of `.nomad.json` is not recorded.

Log rotation:

- Rotate when a single file exceeds 10MB.
- Retain 5 historical files.

---

## Testing Requirements

Write unit tests for each module before writing the implementation.

Must cover:

- Missing config, local mode, multi-target default resolution.
- Remote path whitelist, path depth, home-root rejection.
- Dangerous-command and interactive-command interception.
- Env redaction.
- Line-based and byte-based output truncation.
- Common `.gitignore` rule conversion.
- SSH command argv construction; hosts are not concatenated via shell.
- Task session name validation and Base64 script generation.

Tests involving real SSH/rsync/tmux mock subprocess by default. Real integration tests live separately under `tests/integration/` and don't run by default.

### Phase 1 unit-test matrix

| Module | Must-test behaviors |
|---|---|
| `config.py` | Missing config returns `unconfigured`; `mode=local` guard; single-target default resolution; multi-target missing `default_target` errors; illegal project/target names rejected; hot reload after `.nomad.json` mtime changes |
| `security.py` | Local sensitive-CWD rejection; remote whitelist prefix; remote path depth; home-root rejection; dangerous-command hits; interactive-command hits; sensitive env redaction |
| `truncate.py` | Line-based truncation; byte-based truncation; UTF-8 truncation doesn't throw; blank-line and common-noise filtering; truncation message includes follow-up suggestions |
| `ssh.py` | SSH argv contains `ControlMaster`, `ControlPath`, `ControlPersist`, `ConnectTimeout`, `BatchMode`; `ControlPath` uses `/tmp/nomad_ssh_%C`; jump host argv; preflight error classification; host is not concatenated via shell; `jump_host` and `use_proxy_for_ssh` are mutually exclusive |
| `tools/init.py` | Local project type detection; SSH host alias extraction; connection failure does not write config; `.nomad.json.bak` is written before overwriting; local-mode config can be saved |
| `tools/commands.py` | Execution order matches the spec; env export escaping; remote-command failure returns `remote_command_failed`; output truncation; audit write |
| `tools/sync.py` | Built-in excludes include `.nomad.json`; common `.gitignore` conversion; illegal `local_subpath` rejected; `sync_pull` path-traversal rejected; SSH preflight before rsync; `--delete` path guard; returns a sync summary |
| `tools/network.py` | `tunnel_start` generates a dedicated control socket; remote port in use returns `tunnel_port_in_use`; `tunnel_status` recognizes running/stopped; `tunnel_stop` doesn't affect tasks; proxy environment generated by `proxy_scheme` |
| `server.py` | Registers Phase 1 tools; exposes `config://current-project`; resource redaction; tools return JSON strings |

---

## Extension Points

Future extensions should hang off existing boundaries wherever possible:

- New target types: extend the `.nomad.json` target fields; don't change tool parameter shapes.
- New runtime probes: extend `hardware.detected_runtimes`; don't change `runtime.interpreter` semantics.
- New network policies: extend the `network` fields; don't bypass the unified SSH argv builder.
- New task backends: keep the `task_*` status enum; swap tmux for systemd-run, nohup, or a scheduler.
- New sync policies: keep the `sync_push/sync_pull` contract; add dry-run, delete confirmation, artifact profiles.

---

## Pre-coding Checklist

Before implementing any module, confirm:

- The corresponding behavior is written in this document or the original design doc.
- High-risk operations have rejection conditions.
- Failure returns include `error_type` and `next_action`.
- Unit tests can run without a remote machine.
- No hardcoded personal paths, SSH hosts, or tokens.
