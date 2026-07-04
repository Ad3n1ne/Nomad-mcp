# Security Sandbox

---

## Design Principles

> Under extreme conditions (prompt injection, context confusion, hallucination loops), an AI Agent may execute destructive commands.
> Security defenses must be enforced at the **Python execution layer**; you can't rely solely on tool descriptions to tell the AI "don't do that".

Three lines of defense:

1. **Blacklist regex pre-check**: at the command-string level; a hit triggers a circuit break
2. **Path whitelist validation**: at the target-path level; reject if not in the whitelist
3. **Audit log**: every executed command is persisted and traceable

---

## I. High-Risk Command Blacklist

### Local command security filter (only for internally invoked local commands, e.g. ssh, rsync)

```python
LOCAL_DANGEROUS_PATTERNS = [
    # Root directory deletion (outside the whitelist)
    r"rm\s+-[rf]+\s+/(?!(home|root|workspace|tmp|Users))",
    # sudo rm
    r"sudo\s+rm",
    # fork bomb
    r":\(\)\s*\{.*?\}",
    # Overwriting system files
    r">\s*/etc/",
    r">\s*/usr/",
    r">\s*/bin/",
    # Writing to devices
    r"dd\s+if=.*of=/dev",
    r"mkfs\.",
]
```

### Remote command blacklist (`run_remote`, additionally appended)

```python
REMOTE_DANGEROUS_PATTERNS = LOCAL_DANGEROUS_PATTERNS + [
    # Reading SSH keys
    r"cat\s+.*\.ssh/id_",
    r"cat\s+.*\.ssh/authorized_keys",
    r"cat\s+.*\.ssh/config",
    # Modifying SSH auth
    r"echo\s+.*>>\s*.*authorized_keys",
    # Over-opening permissions
    r"chmod\s+[0-7]*7[0-7]*\s+/",
    r"chown\s+.*\s+/",
    # Reverse shells
    r"bash\s+-i\s+>&",
    r"/dev/tcp/",
    r"nc\s+-e\s+/bin",
]
```

### Execution logic

```python
import re

def check_dangerous(cmd: str, patterns: list[str]) -> str | None:
    """Returns the matched pattern (if any); None means safe."""
    for pattern in patterns:
        if re.search(pattern, cmd, re.IGNORECASE):
            return pattern
    return None

# Example call
if hit := check_dangerous(cmd, REMOTE_DANGEROUS_PATTERNS):
    return f"[Security circuit break] Command contains a high-risk pattern and was rejected.\nMatched rule: {hit}\nOriginal command: {cmd}"
```

---

## II. Interactive Command Interception

The remote cannot allocate a TTY; interactive commands will hang or error. Intercept them before execution:

```python
INTERACTIVE_COMMANDS = [
    "vim", "vi", "nvim", "nano", "emacs",
    "top", "htop", "btop",
    "less", "more", "man",
    "mysql", "psql",    # Database interactive shells
    "python",           # Bare python enters the REPL (use python script.py instead)
    "ipython",
    "node",             # Bare node enters the REPL
]

def is_interactive(cmd: str) -> bool:
    first_token = cmd.strip().split()[0].split("/")[-1]  # take the command name
    return first_token in INTERACTIVE_COMMANDS
```

Example interception return:
```
[Rejected] Interactive command "vim" detected; cannot run in a non-TTY environment.
Suggested alternatives:
  - View a file: cat {file} or head -n 50 {file}
  - Edit a file: edit it locally and then sync with sync_push
```

---

## III. Path Whitelist

### Remote path whitelist

`workspace.remote_path` and every parameter involving a remote path must start with one of the following prefixes:

```python
ALLOWED_REMOTE_PREFIXES = [
    "/home/",
    "/root/",
    "/workspace/",
    "/data/",
    "/tmp/",
    "/opt/",
]
```

Violation examples:
- `/etc/ssh/sshd_config` → rejected
- `/` → rejected
- `/usr/bin/` → rejected

### Local project root restriction

The local project root (i.e. the current working directory CWD when the MCP server starts) must not be one of the following system directories:

```python
BLOCKED_LOCAL_PREFIXES = ["/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/sys", "/proc", "/dev"]
```

---

## IV. Audit Log

Every command executed through the MCP, whether successful or failed, is appended to:

```
~/.nomad/audit.log
```

### Log format

```
[2026-06-30T20:00:00+08:00] [project=llm-finetune] [local]  git add .
[2026-06-30T20:00:01+08:00] [project=llm-finetune] [remote] cd /root/workspace && pytest tests/
[2026-06-30T20:00:05+08:00] [project=llm-finetune] [BLOCKED] rm -rf / (matched rule: rm\s+-rf\s+/)
[2026-06-30T20:00:10+08:00] [project=llm-finetune] [sync]   rsync local→remote (12 files, 0.8s)
```

### Log rotation

- When a single file exceeds 10MB, it is automatically renamed to `audit.log.1` and a fresh `audit.log` is created
- At most 5 historical files are retained

---

## V. rsync `--delete` Safety Guard

`sync_push` uses `--delete`, which deletes files present on the remote but absent locally — the most dangerous sync operation.

Extra protections:

1. **Path length protection**: If `remote_path` is too short (depth < 3, e.g. `/root`), reject any rsync carrying `--delete`
2. **Dry-run pre-check** (optional, future feature): First run `rsync --dry-run` to list the files that would be deleted; if it exceeds a threshold (e.g. 50 files), pause and ask the user to confirm
3. **Forbid home-directory roots as the target**: `remote_path` cannot be `/root` or `/home/user`; there must be a project subdirectory
