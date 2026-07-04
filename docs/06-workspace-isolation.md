# Workspace Isolation Mechanism

---

## Core Principle

> **Every tool call = read `.nomad.json` first, then execute the operation.**
>
> No tool is allowed to execute a remote operation without an explicit project context.

---

## Physical Basis of Isolation

When an IDE (Cursor, Claude Code, etc.) launches a project window, the **CWD (current working directory)** of the underlying MCP Server process automatically points to the currently open project root.

Therefore:
- For project A's MCP process, `os.getcwd()` returns A's root directory and reads A's `.nomad.json`
- For project B's MCP process, `os.getcwd()` returns B's root directory and reads B's `.nomad.json`

**No extra configuration needed; cross-talk is naturally impossible.**

---

## Config Loading Logic

```python
import os
import json
from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=1)
def _load_config_cached(config_mtime: float) -> dict:
    """mtime-aware cache: skip IO when the file hasn't changed."""
    config_path = Path(os.getcwd()) / ".nomad.json"
    return json.loads(config_path.read_text(encoding="utf-8"))

def load_config() -> dict:
    config_path = Path(os.getcwd()) / ".nomad.json"
    if not config_path.exists():
        return {"mode": "unconfigured"}
    mtime = config_path.stat().st_mtime
    return _load_config_cached(mtime)
```

---

## Mode Guard

All tools involving remote operations invoke this guard before executing:

```python
def guard_remote(config: dict) -> str | None:
    """
    Returns an error message (if any); returning None means it passed.
    """
    mode = config.get("mode", "unconfigured")

    if mode == "unconfigured":
        return (
            "[Rejected] No .nomad.json detected in the current directory; cannot perform remote operations.\n"
            "Please run init_discover first to initialize the project."
        )

    if mode == "local":
        project = config.get("project_name", "unknown project")
        return (
            f"[Rejected] The current project [{project}] is configured as pure local mode; "
            "any remote sync or command operation is forbidden."
        )

    return None  # mode == "remote", passes
```

---

## AI "Degraded" Behavior

When `.nomad.json` has `mode = local`, the AI should automatically degrade:
- It will not attempt to call `sync_push`, `run_remote`, `task_start`
- It only guides the user to use the local IDE's built-in command line for local tasks, without invoking any remote MCP tools
- At the start of a conversation, once the AI reads `mode=local`, it should proactively tell the user: "The current project is in local mode; I won't invoke remote sync or command-execution tools."

**Implementation**: Expose `.nomad.json` to the AI as a read-only resource via the MCP `Resources` mechanism. The AI reads it automatically at the start of a conversation, so it doesn't need to call a tool each time to learn the current mode.

```python
@mcp.resource("config://current-project")
def get_current_project_config() -> str:
    """Expose the current project config so the AI can perceive it at session start."""
    config = load_config()
    return json.dumps(config, ensure_ascii=False, indent=2)
```

---

## Cross-Project Misoperation Protection

Beyond the natural CWD isolation, these additional protections apply:

### Local Project Root Safety Check

Before startup and before each operation, verify that the current working directory `CWD` is not in the system sensitive-directory blocklist, to prevent accidental initialization or execution under a system root:

```python
BLOCKED_LOCAL_PREFIXES = ["/etc", "/usr", "/bin", "/sbin", "/lib", "/sys", "/proc", "/dev", "/"]

def verify_local_path_safety() -> str | None:
    cwd = os.path.abspath(os.getcwd())
    for prefix in BLOCKED_LOCAL_PREFIXES:
        if cwd == prefix or cwd.startswith(prefix + "/"):
            return (
                f"[Security block] The current working directory ({cwd}) is a system-sensitive directory; running remote operations here is rejected."
            )
    return None
```

### tmux Session Prefix Isolation

All remote tmux session names are enforced to start with `{project_name}_`. `task_list` only lists sessions belonging to the current project; tasks of other projects are never exposed.

---

## Initialization State Machine

Project lifecycle:

```
Uninitialized (no .nomad.json)
       │
       │ init_discover + init_save_config
       ▼
Initialized (mode=remote or mode=local)
       │
       │ User manual edit or init_save_config overwrite
       ▼
Reconfigured (server swap, path change, etc.)
```

**There is no "global config"**; each project carries its own independent ID.
