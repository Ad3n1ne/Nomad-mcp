import logging
import os
import stat

import pytest

import nomad.mcp_logging as mcp_logging
from nomad.mcp_logging import format_traceback, get_mcp_logger, redact_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("API_TOKEN=alpha", "API_TOKEN=[REDACTED]"),
        ("service_token: beta", "service_token: [REDACTED]"),
        ("PASSWORD = hunter2", "PASSWORD = [REDACTED]"),
        ("PASSWD='quoted value'", "PASSWD='[REDACTED]'"),
        ('{"API_KEY":"json-value"}', '{"API_KEY":"[REDACTED]"}'),
        (
            '{"AWS_SECRET_ACCESS_KEY": "aws-value"}',
            '{"AWS_SECRET_ACCESS_KEY": "[REDACTED]"}',
        ),
        ("KEY=private-value", "KEY=[REDACTED]"),
        ("PRIVATE_KEY=private-value", "PRIVATE_KEY=[REDACTED]"),
        ("SSH_PRIVATE_KEY: private-value", "SSH_PRIVATE_KEY: [REDACTED]"),
        ("SIGNING_KEY='private value'", "SIGNING_KEY='[REDACTED]'"),
        ("AUTH=Bearer bearer-value", "AUTH=[REDACTED]"),
        ("CREDENTIAL: credential-value", "CREDENTIAL: [REDACTED]"),
        ("client_secret=secret-value", "client_secret=[REDACTED]"),
    ],
)
def test_redact_text_masks_sensitive_assignments(text, expected):
    assert redact_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "The tokenizer counts tokens and authentication failed.",
        "TOKENIZER=value AUTHOR=alice MONKEY=banana KEYNOTE=value",
        "api_keynote=value credentialed_user=alice",
        "Keep this secret from ordinary log readers.",
    ],
)
def test_redact_text_preserves_non_sensitive_text(text):
    assert redact_text(text) == text


@pytest.mark.parametrize(
    "key_type",
    [
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
    ],
)
def test_redact_text_masks_multiline_pem_private_keys(key_type):
    private_body = "line-one-secret\nline-two-secret"
    text = (
        f"before\n-----BEGIN {key_type}-----\n"
        f"{private_body}\n"
        f"-----END {key_type}-----\nafter"
    )

    redacted = redact_text(text)

    assert private_body not in redacted
    assert redacted == (
        f"before\n-----BEGIN {key_type}-----\n"
        "[REDACTED]\n"
        f"-----END {key_type}-----\nafter"
    )


@pytest.mark.parametrize(
    "token",
    [
        "pypi-SYNTHETIC_TEST_TOKEN_000000000000",
        "ghp_SYNTHETICTESTTOKEN000000000000",
        "gho_SYNTHETICTESTTOKEN000000000000",
        "ghu_SYNTHETICTESTTOKEN000000000000",
        "ghs_SYNTHETICTESTTOKEN000000000000",
        "ghr_SYNTHETICTESTTOKEN000000000000",
        "github_pat_SYNTHETIC_TEST_TOKEN_000000000000",
        "sk-SYNTHETICTESTTOKEN000000000000",
        "sk-proj-SYNTHETIC_TEST_TOKEN_000000000000",
        "AKIA" + "SYNTHETIC" + "0" * 7,
        "ASIA" + "SYNTHETIC" + "0" * 7,
        "xoxb-SYNTHETIC-TEST-TOKEN-000000",
        "xoxp-SYNTHETIC-TEST-TOKEN-000000",
        "xoxa-SYNTHETIC-TEST-TOKEN-000000",
        "xoxr-SYNTHETIC-TEST-TOKEN-000000",
        "xoxs-SYNTHETIC-TEST-TOKEN-000000",
    ],
)
def test_format_traceback_masks_bare_token_prefixes(token):
    try:
        raise RuntimeError(f"upstream rejected {token}")
    except RuntimeError as exc:
        formatted = format_traceback(exc)

    assert token not in formatted
    assert "[REDACTED]" in formatted


@pytest.mark.parametrize(
    "text",
    [
        "Install the pypi-package-name package.",
        "The sk-short-value label is not a credential.",
        "The sk-project-planning-document name is ordinary text.",
        "AKIA is an AWS prefix, not a complete access key.",
        "xoxb-not-a-secret",
        "github_pat_documentation",
    ],
)
def test_redact_text_preserves_token_like_non_secrets(text):
    assert redact_text(text) == text


def _reset_mcp_logger() -> None:
    logger = logging.getLogger(mcp_logging.LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    mcp_logging._LOGGER = None


@pytest.fixture(autouse=True)
def reset_mcp_logger(monkeypatch):
    _reset_mcp_logger()
    monkeypatch.delenv(mcp_logging.LOG_ENV_VAR, raising=False)
    yield
    _reset_mcp_logger()


def test_secure_logger_creates_private_parent_and_file(tmp_path, monkeypatch):
    log_path = tmp_path / "private" / "nested" / "mcp.log"
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(log_path))

    logger = get_mcp_logger()
    logger.info("ready")
    logger.handlers[0].flush()

    assert isinstance(logger.handlers[0], logging.FileHandler)
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert log_path.stat().st_uid == os.getuid()
    assert log_path.stat().st_nlink == 1


def test_secure_logger_applies_same_policy_to_default_path(tmp_path, monkeypatch):
    log_path = tmp_path / "default" / "nomad-mcp.log"
    log_path.parent.mkdir(mode=0o755)
    log_path.parent.chmod(0o755)
    monkeypatch.setattr(mcp_logging, "DEFAULT_LOG_PATH", log_path)

    logger = get_mcp_logger()
    logger.info("default path")
    logger.handlers[0].flush()

    assert log_path.read_text(encoding="utf-8").endswith("default path\n")
    assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_secure_logger_preserves_safe_custom_parent_and_tightens_file(
    tmp_path, monkeypatch
):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o755)
    log_dir.chmod(0o755)
    log_path = log_dir / "mcp.log"
    log_path.write_text("existing\n", encoding="utf-8")
    log_path.chmod(0o644)
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(log_path))

    get_mcp_logger()

    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("mode", [0o775, 0o707, 0o777])
def test_secure_logger_rejects_writable_custom_parent(
    tmp_path, monkeypatch, mode
):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_dir.chmod(mode)
    log_path = log_dir / "mcp.log"
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(log_path))

    with pytest.raises(PermissionError, match="group/world-writable"):
        get_mcp_logger()

    assert stat.S_IMODE(log_dir.stat().st_mode) == mode
    assert not log_path.exists()


def test_secure_logger_rejects_symlink_parent_without_stdio_output(
    tmp_path, monkeypatch, capsys
):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(real_dir, target_is_directory=True)
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(linked_dir / "mcp.log"))

    with pytest.raises(OSError):
        get_mcp_logger()

    assert not (real_dir / "mcp.log").exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_secure_logger_rejects_symlink_file(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    target = log_dir / "target.log"
    target.write_text("do not touch\n", encoding="utf-8")
    log_path = log_dir / "mcp.log"
    log_path.symlink_to(target)
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(log_path))

    with pytest.raises(OSError):
        get_mcp_logger()

    assert target.read_text(encoding="utf-8") == "do not touch\n"


def test_secure_logger_rejects_hardlinked_file(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    target = log_dir / "target.log"
    target.write_text("do not touch\n", encoding="utf-8")
    log_path = log_dir / "mcp.log"
    os.link(target, log_path)
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(log_path))

    with pytest.raises(PermissionError, match="hard link"):
        get_mcp_logger()

    assert target.read_text(encoding="utf-8") == "do not touch\n"


def test_secure_logger_rejects_foreign_owned_parent(tmp_path, monkeypatch):
    log_path = tmp_path / "mcp.log"
    current_uid = os.getuid()
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(log_path))
    monkeypatch.setattr(mcp_logging.os, "getuid", lambda: current_uid + 1)

    with pytest.raises(PermissionError, match="parent is not owned"):
        get_mcp_logger()

    assert not log_path.exists()


def test_secure_logger_rejects_foreign_owned_file(tmp_path, monkeypatch):
    log_path = tmp_path / "mcp.log"
    log_path.write_text("do not touch\n", encoding="utf-8")
    monkeypatch.setenv(mcp_logging.LOG_ENV_VAR, str(log_path))
    current_uid = os.getuid()
    real_fstat = os.fstat

    def foreign_file_fstat(fd):
        result = real_fstat(fd)
        if not stat.S_ISREG(result.st_mode):
            return result
        values = list(result)
        values[4] = current_uid + 1
        return os.stat_result(values)

    monkeypatch.setattr(mcp_logging.os, "fstat", foreign_file_fstat)

    with pytest.raises(PermissionError, match="log is not owned"):
        get_mcp_logger()

    assert log_path.read_text(encoding="utf-8") == "do not touch\n"


def test_server_lifecycle_logging_degrades_quietly(monkeypatch, capsys):
    def unavailable():
        raise PermissionError("synthetic unsafe log path")

    monkeypatch.setattr(mcp_logging, "get_mcp_logger", unavailable)

    assert mcp_logging.log_server_startup("/synthetic/cwd", "0.test") is None
    assert mcp_logging.log_server_shutdown() is None

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
