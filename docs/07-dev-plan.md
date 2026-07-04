# Development Plan, Technology Choices, and Risk Mitigations

---

## Technology Choices

| Component | Choice | Reason |
|---|---|---|
| MCP SDK | `mcp` (official Python SDK) + `FastMCP` | Minimal config, stdio mode, called directly by the IDE |
| SSH execution | `subprocess` + system `ssh` CLI | Reuses local `~/.ssh/config`; no need to manage keys ourselves; ControlMaster is directly usable |
| rsync | System `rsync` CLI | Mature, incremental, supports `--exclude-from` |
| Config file | JSON (`.nomad.json`) | Simple; easy for the AI to generate and parse; humans can edit directly too |
| Package manager | `uv` | Fast, isolated; no environment activation needed at startup |
| Python version | 3.11+ | Built-in `tomllib`, better type annotation support |

---

## Project Structure

```
nomad/
├── pyproject.toml
├── README.md
├── README.zh-CN.md
├── LICENSE
│
├── src/
│   └── nomad/
│       ├── __init__.py
│       ├── cli.py         # human-facing helper CLI; no args starts the MCP server
│       ├── server.py      # FastMCP entry point, tool registration
│       ├── config.py      # .nomad.json loading, validation, guards
│       ├── security.py    # command checks, path guards, audit log redaction
│       ├── truncate.py    # unified output truncation
│       ├── ssh.py         # SSH argv builders, ControlMaster, probing, bastion hosts
│       └── tools/
│           ├── init.py    # init_discover, init_verify_and_probe, init_save_config, init_probe_target
│           ├── commands.py
│           ├── sync.py
│           ├── tasks.py
│           └── network.py
│
├── tests/
└── docs/
```

---

## Phased Development Plan

### Phase 0 — Spec Freeze (before coding)

Goal: Nail down the implementation contract, failure modes, security boundaries, and extension points before entering feature implementation.

- [x] `08-implementation-spec.md`: unified return contract, initialization state machine, MCP exposure surface, command execution boundaries, sync/long-task/network-diagnostics specs
- [x] Fill in the per-module unit-test case list for each Phase 1 module
- [x] Review existing docs against the spec, resolve conflicts or over-promises

**Acceptance criteria**:
1. Before work starts on any core module, you can find the execution order, rejection conditions, and return format in the spec
2. Security-related behavior has tests first, then implementation
3. MVP simplification boundaries are written explicitly; unimplemented capabilities are not described as supported

---

### Phase 1 — Skeleton (usable MVP)

Goal: Complete the core loop of "initialize → push code → run commands" so you can use it day to day.

- [ ] `config.py`: `.nomad.json` loading, validation, guards
- [ ] `security.py`: blacklist pre-check, path whitelist (minimal version)
- [ ] `truncate.py`: unified truncation logic
- [ ] `ssh.py`: basic SSH wrapper (ConnectTimeout, BatchMode, ControlMaster)
- [ ] `tools/init.py`: `init_discover`, `init_verify_and_probe`, `init_save_config`, `init_probe_target`
- [ ] `tools/commands.py`: `run_remote`
- [ ] `tools/sync.py`: `sync_push` (including gitignore integration)
- [ ] `tools/network.py`: `tunnel_start`, `tunnel_status`, `tunnel_stop` (persistent reverse tunnel for remote outbound access)
- [ ] `server.py`: FastMCP entry point, registering the tools above and exposing a `config://current-project` resource so the AI auto-reads the config at session start

**Acceptance criteria**:
1. After mounting in Cursor / Claude Code, can initialize a new project
2. `sync_push` successfully incremental-syncs code to the remote
3. `run_remote("pytest tests/")` returns output
4. When a target enables `reverse_tunnel`, `tunnel_start` establishes a persistent reverse tunnel and the remote can use the local proxy via `127.0.0.1:{remote_bind_port}`

---

### Phase 2 — Long Task Support

Goal: The AI can manage long-running remote tasks without timing out.

- [ ] `tools/tasks.py`: `task_start`, `task_status`, `task_list`, `task_kill`
- [ ] tmux session naming convention validation
- [ ] exit_code file write logic
- [ ] Differentiated truncation (the `tail_lines` parameter of `task_status`)
- [ ] When a target enables `reverse_tunnel`, `task_start` auto-ensures the tunnel is running and writes the proxy environment into the task script

**Acceptance criteria**:
1. Start a 10-minute training task on the remote; the MCP tool returns immediately
2. Call `task_status` 30 seconds later and get the latest log
3. After the task ends, the status correctly shows `finished_success` or `finished_error`
4. With the reverse tunnel enabled, the tmux long task can still reach the external network via the local proxy after the SSH launch command returns

---

### Phase 3 — Diagnostics and Security Hardening

Goal: Enhance failure diagnostics, reconnect recovery, and security auditing. Complex network auto-repair is not a core goal.

- [ ] `tools/network.py`: `net_diagnose`
- [ ] Bastion host (`-J`) support
- [ ] Reverse-tunnel reconnect recovery and richer status diagnostics
- [ ] Complete security blacklist
- [ ] Audit log (with rotation)
- [ ] rsync `--delete` dry-run deletion-count threshold confirmation

---

### Phase 4 — Experience Polish

Goal: Smooth out edge cases and lower the first-use barrier.

- [ ] `tools/sync.py`: `sync_pull` (pull artifacts back to local)
- [ ] Noise filtering (pip progress bars, blank-line removal)
- [ ] Installation docs + per-IDE config examples (Cursor, Claude Code, Trae)

---

## Risk Mitigations

| Risk | Concrete scenario | Mitigation |
|---|---|---|
| rsync accidentally deletes remote files | AI passes the wrong remote_path; `--delete` clears a directory | Path whitelist + path depth check (≥3 levels) |
| AI hallucinates a high-risk command | Prompt injection or context confusion | Python-layer regex blacklist; a hit triggers an immediate circuit break |
| Long output blows up the AI context | `cat` on a big file, full compile errors | Unified 200-line / 10KB truncation |
| tmux session zombie buildup | AI repeatedly launches tasks | Unique naming + `task_list` exposed for the AI to self-clean |
| SSH connection hangs | TUN route conflict, target unreachable | All SSH calls add `ConnectTimeout=5`; pre-flight probe |
| Project config accidentally committed to Git | `.nomad.json` contains ssh_host info | README explicitly recommends adding it to `.gitignore` |
| ControlMaster socket leftover or mix-up | MCP process crashes; different users connect to the same host | Use `/tmp/nomad_ssh_%C`; detect and clean stale sockets at startup |
| tmux long task mistakenly assumes the reverse proxy is still alive | `ssh -R` short connection launches tmux and returns immediately; the tunnel closes with the connection | Phase 1 provides a persistent tunnel lifecycle; Phase 2's `task_start` only uses the persistent tunnel |

---

## IDE Integration

### Cursor

Add to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": ["nomad"]
    }
  }
}
```

### Claude Code (claude_desktop_config.json)

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": ["nomad"]
    }
  }
}
```

### Local Development Debug Mode

```bash
# After cloning the repo
uv run python -m nomad.server
```

---

## Release Plan

1. **Local self-use phase** (after Phase 1-2): use directly via `uv run` or `pip install -e .`
2. **Open-source release** (after Phase 3): publish to PyPI, install in one line via `uvx nomad`
3. **MCP Marketplace**: submit to each IDE's MCP plugin marketplace (e.g. the Cursor Marketplace)
