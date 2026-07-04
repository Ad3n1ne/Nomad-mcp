"""
Security sandbox, dangerous command blacklists, and path whitelist guards for nomad.
"""
from __future__ import annotations

import os
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from collections.abc import Mapping
from typing import Optional, List


LOCAL_DANGEROUS_PATTERNS: List[str] = [
    r"rm\s+-[rf]+\s+/(?!(home|root|workspace|tmp|Users))",
    r"sudo\s+rm",
    r":\(\)\s*\{.*?\}",
    r">\s*/etc/",
    r">\s*/usr/",
    r">\s*/bin/",
    r"dd\s+if=.*of=/dev",
    r"mkfs\.",
]
REMOTE_DANGEROUS_PATTERNS: List[str] = LOCAL_DANGEROUS_PATTERNS + [
    r"cat\s+.*\.ssh/id_",
    r"cat\s+.*\.ssh/authorized_keys",
    r"cat\s+.*\.ssh/config",
    r"echo\s+.*>>\s*.*authorized_keys",
    r"chmod\s+[0-7]*7[0-7]*\s+/",
    r"chown\s+.*\s+/",
    r"bash\s+-i\s+>&",
    r"/dev/tcp/",
    r"nc\s+-e\s+/bin",
]
BLOCKED_LOCAL_PREFIXES: List[str] = [
    "/",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/sys",
    "/proc",
    "/dev",
]
ALLOWED_REMOTE_PREFIXES: List[str] = [
    "/home/",
    "/root/",
    "/workspace/",
    "/data/",
    "/tmp/",
    "/opt/",
]
INTERACTIVE_COMMANDS = {
    "vim",
    "vi",
    "nvim",
    "nano",
    "emacs",
    "top",
    "htop",
    "btop",
    "less",
    "more",
    "man",
    "mysql",
    "psql",
    "python",
    "python3",
    "ipython",
    "node",
}
AUDIT_LOG_MAX_BYTES = 10 * 1024 * 1024
AUDIT_LOG_HISTORY_COUNT = 5
REDACTED = "***REDACTED***"
SENSITIVE_KEYWORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "AUTH")


def check_dangerous_command(cmd: str, is_remote: bool = True) -> Optional[str]:
    """Checks cmd against patterns, returns hit rule description if dangerous, else None."""
    patterns = REMOTE_DANGEROUS_PATTERNS if is_remote else LOCAL_DANGEROUS_PATTERNS
    for pattern in patterns:
        if re.search(pattern, cmd, re.IGNORECASE):
            return pattern
    return None


def check_interactive_command(cmd: str) -> Optional[str]:
    """Returns the interactive command name when cmd would require a TTY."""
    tokens = _split_command(cmd)
    if not tokens:
        return None

    command = PurePosixPath(tokens[0]).name
    if command not in INTERACTIVE_COMMANDS:
        return None
    if command in {"python", "python3"}:
        if len(tokens) == 1 or "-i" in tokens[1:]:
            return command
        return None
    if command == "node":
        if len(tokens) == 1 or any(
            token in {"-i", "--interactive"} for token in tokens[1:]
        ):
            return command
        return None
    return command


def verify_local_cwd_safety() -> Optional[str]:
    """Ensures CWD is not in blocked local prefix directory."""
    cwd = os.path.abspath(os.getcwd())
    for blocked in BLOCKED_LOCAL_PREFIXES:
        if blocked == "/":
            if cwd == "/":
                return "unsafe_local_cwd"
            continue
        if cwd == blocked or cwd.startswith(f"{blocked}/"):
            return "unsafe_local_cwd"
    return None


def verify_remote_path_safety(remote_path: str) -> Optional[str]:
    """Ensures remote_path matches whitelisted prefix and has depth >= 3."""
    if not isinstance(remote_path, str) or "\x00" in remote_path:
        return "unsafe_remote_path"

    path = PurePosixPath(remote_path)
    if not path.is_absolute():
        return "unsafe_remote_path"
    if not remote_path.startswith(tuple(ALLOWED_REMOTE_PREFIXES)):
        return "unsafe_remote_path"
    if len(path.parts) < 3:
        return "unsafe_remote_path"
    if _is_home_root(path):
        return "unsafe_remote_path"
    return None


def write_audit_log(project_name: str, action_type: str, detail: str) -> None:
    """Logs action to ~/.nomad/audit.log with rotation."""
    log_dir = Path.home() / ".nomad"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "audit.log"
    _rotate_audit_log(log_path)

    timestamp = datetime.now(timezone.utc).isoformat()
    safe_detail = redact_audit_detail(detail).replace("\n", "\\n")
    line = f"[{timestamp}] [project={project_name}] [{action_type}] {safe_detail}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def redact_env(env: Mapping[str, str]) -> dict[str, str]:
    """Returns a copy of env with sensitive values masked."""
    redacted = {}
    for key, value in env.items():
        redacted[key] = REDACTED if _is_sensitive_key(key) else value
    return redacted


def redact_audit_detail(detail: str) -> str:
    """Masks secrets in an audit log detail string."""
    redacted = str(detail)
    redacted = _redact_nomad_json(redacted)
    redacted = _redact_url_credentials(redacted)
    redacted = _redact_authorization_tokens(redacted)
    redacted = _redact_sensitive_assignments(redacted)
    redacted = _redact_sensitive_json_pairs(redacted)
    return redacted


def _split_command(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd.strip())
    except ValueError:
        return cmd.strip().split()


def _is_home_root(path: PurePosixPath) -> bool:
    parts = path.parts
    return parts == ("/", "root") or (
        len(parts) == 3 and parts[0] == "/" and parts[1] == "home"
    )


def _rotate_audit_log(log_path: Path) -> None:
    if not log_path.exists() or log_path.stat().st_size <= AUDIT_LOG_MAX_BYTES:
        return

    oldest = log_path.with_name(f"{log_path.name}.{AUDIT_LOG_HISTORY_COUNT}")
    if oldest.exists():
        oldest.unlink()

    for index in range(AUDIT_LOG_HISTORY_COUNT - 1, 0, -1):
        source = log_path.with_name(f"{log_path.name}.{index}")
        if source.exists():
            source.rename(log_path.with_name(f"{log_path.name}.{index + 1}"))

    log_path.rename(log_path.with_name(f"{log_path.name}.1"))


def _is_sensitive_key(key: str) -> bool:
    upper_key = key.upper()
    return any(keyword in upper_key for keyword in SENSITIVE_KEYWORDS)


def _redact_nomad_json(detail: str) -> str:
    return re.sub(
        r"(\.nomad\.json)\s+\{.*?\}",
        r"\1 [REDACTED_CONFIG]",
        detail,
        flags=re.DOTALL,
    )


def _redact_url_credentials(detail: str) -> str:
    return re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s:@]+):([^/\s@]+)@",
        r"\1***:***@",
        detail,
    )


def _redact_authorization_tokens(detail: str) -> str:
    pattern = re.compile(
        r"((?:authorization|auth_token)\s*(?::|=)\s*(?:Bearer|Basic|Token)\s+)"
        r"[^\s\"']+",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", detail)


def _redact_sensitive_assignments(detail: str) -> str:
    pattern = re.compile(
        r"\b([A-Z_][A-Z0-9_]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s]+)",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if not _is_sensitive_key(key):
            return match.group(0)
        if _is_redacted_authorization_value(match, detail, value_group=2):
            return match.group(0)
        return f"{key}={REDACTED}"

    return pattern.sub(replace, detail)


def _redact_sensitive_json_pairs(detail: str) -> str:
    pattern = re.compile(
        r"((?:\"|')?([A-Z_][A-Z0-9_]*)(?:\"|')?\s*:\s*)"
        r"(\"[^\"]*\"|'[^']*'|[^\s,}]+)",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        if not _is_sensitive_key(match.group(2)):
            return match.group(0)
        if _is_redacted_authorization_value(match, detail, value_group=3):
            return match.group(0)
        return f'{match.group(1)}"{REDACTED}"'

    return pattern.sub(replace, detail)


def _is_redacted_authorization_value(
    match: re.Match[str], detail: str, *, value_group: int
) -> bool:
    value = match.group(value_group).strip("\"'").lower()
    remainder = detail[match.end() :].lstrip()
    return value in {"bearer", "basic", "token"} and remainder.startswith(REDACTED)
