# Context Defense (Token Circuit Breaker)

---

## Problem

Linux command output has no upper bound. A single `cat large_file.log`, a `find / -name "*.py"`, or the full stack trace of a compile error can easily emit several MB of text.

If MCP tools return this content to the AI verbatim:
- The IDE's context window overflows; a single conversation reports a token limit error
- API call costs explode
- The AI starts "getting lost" in a sea of irrelevant output, and decision quality drops

---

## Unified Truncation Rule

Every tool's stdout/stderr output is processed uniformly in the Python layer before being returned to the AI:

```python
def safe_truncate(output: str, max_lines: int, max_bytes: int) -> str:
    # Prefer line-based truncation first
    lines = output.splitlines()
    if len(lines) > max_lines:
        kept = lines[:max_lines]
        truncated = len(lines) - max_lines
        return "\n".join(kept) + (
            f"\n\n... [Output too long; the last {truncated} lines were truncated. "
            f"Try filtering with grep/tail -n/head -n and retry]"
        )
    # Then byte-based truncation
    encoded = output.encode("utf-8")
    if len(encoded) > max_bytes:
        truncated_bytes = encoded[:max_bytes].decode("utf-8", errors="ignore")
        return truncated_bytes + (
            f"\n\n... [Output too long; truncated by bytes. "
            f"Try filtering with grep/tail -n/head -n and retry]"
        )
    return output
```

**Default thresholds** (overridable via the `limits` field of `.nomad.json`):

```python
DEFAULT_MAX_LINES = 200
DEFAULT_MAX_BYTES = 10_240  # 10KB
```

---

## Differentiated Truncation by Tool Type

| Tool | Max lines | Notes |
|---|---|---|
| `run_remote` | 200 lines / 10KB | General command output |
| `task_status` (log polling) | Decided by the `tail_lines` parameter, default 10 | The AI only needs the latest few lines while polling |
| `sync_push` | Only returns the last 5 lines of rsync output + statistics | Full rsync logs are usually very verbose |
| `net_diagnose` | Not truncated | Diagnostic reports are usually short and need full info |
| `init_discover` | Not truncated | Structured JSON, content is bounded |

---

## Noise Filtering

Some commands produce large amounts of meaningless progress output; filter before truncating:

```python
NOISE_PATTERNS = [
    r"^\s*$",                    # Pure blank lines
    r"^Downloading.*\d+%",       # pip download progress lines (keep only the last one)
    r"^Already up to date\.$",   # git pull with no update
]

def filter_noise(lines: list[str]) -> list[str]:
    """Strip pure-noise lines, keep meaningful output."""
    return [l for l in lines if not any(re.match(p, l) for p in NOISE_PATTERNS)]
```

---

## AI-Side Response Hints

Truncation messages should be specific enough that the AI knows how to continue:

**General hint**:
```
[Output truncated; {total} lines total, only the first 200 shown.
To view errors, use: task_status(task_name, tail_lines=50) or run_remote("grep -n 'Error\|error\|FAIL' {task_log_path} | tail -50")]
```

**rsync-specific hint**:
```
[Full rsync output omitted; synced {n} files in {t}s. To see the full transfer list, use run_remote("ls -la {remote_path}")]
```

---

## Large File Pull Scenario

`sync_pull` is used to pull remote artifacts. If the file is large (e.g. a model checkpoint), returning its content via the MCP is inappropriate — it does file transfer only, no content reading:

- `sync_pull` only returns "pull succeeded/failed + file size"
- The file content is viewed by the user locally
- If the AI wants to read a large remote file's content, it should be guided to use `run_remote("head -n 100 {file}")` or `run_remote("wc -l {file}")`
