import errno
import os

from pathlib import Path

import pytest

from nomad import codex_atomic, codex_config


def _replace_config_at(directory_fd: int, content: bytes) -> None:
    editor_name = ".config.toml.editor"
    fd = os.open(
        editor_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=directory_fd,
    )
    with os.fdopen(fd, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(
        editor_name,
        "config.toml",
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _update(snapshot) -> None:
    codex_config._update_project_config(
        snapshot,
        name="nomad",
        url="http://127.0.0.1:54321/mcp",
        token_env_var="NOMAD_TEST_TOKEN",
    )


def _preserved_contents(error) -> list[bytes]:
    return [
        Path(path).read_bytes()
        for path in error.details.get("preserved_concurrent_config_paths", [])
    ]


def test_atomic_exchange_restores_concurrent_editor_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    config_dir = project / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        '[mcp_servers.nomad]\ncommand = "nomad"\n',
        encoding="utf-8",
    )
    snapshot = codex_config._read_project_config(project)
    user_content = b"# concurrent editor content\n"
    original_exchange = codex_atomic._atomic_exchange
    raced = False

    def exchange(directory_fd: int, first: str, second: str) -> None:
        nonlocal raced
        if not raced and second == "config.toml":
            raced = True
            _replace_config_at(directory_fd, user_content)
        original_exchange(directory_fd, first, second)

    monkeypatch.setattr(codex_atomic, "_atomic_exchange", exchange)

    with pytest.raises(codex_config.CodexConfigError) as raised:
        _update(snapshot)

    assert raised.value.error_type == "concurrent_config_change"
    assert raised.value.details["config_committed"] is False
    assert config_path.read_bytes() == user_content
    proposed_path = Path(raised.value.details["preserved_config_path"])
    assert proposed_path.exists()
    assert b"url = " in proposed_path.read_bytes()


def test_edit_after_successful_exchange_is_reported_as_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    config_dir = project / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    original_content = b'[mcp_servers.nomad]\ncommand = "nomad"\n'
    config_path.write_bytes(original_content)
    snapshot = codex_config._read_project_config(project)
    user_content = b"# editor replaced proposed config after exchange\n"
    original_exchange = codex_atomic._atomic_exchange
    raced = False

    def exchange(directory_fd: int, first: str, second: str) -> None:
        nonlocal raced
        original_exchange(directory_fd, first, second)
        if not raced and second == "config.toml":
            raced = True
            _replace_config_at(directory_fd, user_content)

    monkeypatch.setattr(codex_atomic, "_atomic_exchange", exchange)

    with pytest.raises(codex_config.CodexConfigError) as raised:
        _update(snapshot)

    assert raised.value.error_type == "concurrent_config_change"
    assert config_path.read_bytes() == user_content
    proposed_path = Path(raised.value.details["preserved_config_path"])
    assert b"url = " in proposed_path.read_bytes()
    assert original_content in _preserved_contents(raised.value)


def test_second_edit_before_restore_preserves_both_user_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    config_dir = project / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        '[mcp_servers.nomad]\ncommand = "nomad"\n',
        encoding="utf-8",
    )
    snapshot = codex_config._read_project_config(project)
    first_user_content = b"# first concurrent edit\n"
    second_user_content = b"# second edit before restore\n"
    original_exchange = codex_atomic._atomic_exchange
    exchange_count = 0

    def exchange(directory_fd: int, first: str, second: str) -> None:
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            _replace_config_at(directory_fd, first_user_content)
        elif exchange_count == 2:
            _replace_config_at(directory_fd, second_user_content)
        original_exchange(directory_fd, first, second)

    monkeypatch.setattr(codex_atomic, "_atomic_exchange", exchange)

    with pytest.raises(codex_config.CodexConfigError) as raised:
        _update(snapshot)

    assert raised.value.error_type == "concurrent_config_change"
    assert config_path.read_bytes() == first_user_content
    proposed_path = Path(raised.value.details["preserved_config_path"])
    assert b"url = " in proposed_path.read_bytes()
    preserved = _preserved_contents(raised.value)
    assert first_user_content in preserved
    assert second_user_content in preserved


def test_second_edit_after_restore_preserves_restored_user_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    config_dir = project / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        '[mcp_servers.nomad]\ncommand = "nomad"\n',
        encoding="utf-8",
    )
    snapshot = codex_config._read_project_config(project)
    first_user_content = b"# first concurrent edit\n"
    second_user_content = b"# second edit after restore\n"
    original_exchange = codex_atomic._atomic_exchange
    exchange_count = 0

    def exchange(directory_fd: int, first: str, second: str) -> None:
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            _replace_config_at(directory_fd, first_user_content)
        original_exchange(directory_fd, first, second)
        if exchange_count == 2:
            _replace_config_at(directory_fd, second_user_content)

    monkeypatch.setattr(codex_atomic, "_atomic_exchange", exchange)

    with pytest.raises(codex_config.CodexConfigError) as raised:
        _update(snapshot)

    assert raised.value.error_type == "concurrent_config_change"
    assert config_path.read_bytes() == second_user_content
    proposed_path = Path(raised.value.details["preserved_config_path"])
    assert b"url = " in proposed_path.read_bytes()
    assert first_user_content in _preserved_contents(raised.value)


def test_atomic_create_does_not_replace_concurrent_editor_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    config_dir = project / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    snapshot = codex_config._read_project_config(project)
    user_content = b"# editor created this first\n"
    original_no_replace = codex_atomic._atomic_no_replace
    raced = False

    def no_replace(directory_fd: int, source: str, destination: str) -> None:
        nonlocal raced
        if not raced and destination == "config.toml":
            raced = True
            _replace_config_at(directory_fd, user_content)
        original_no_replace(directory_fd, source, destination)

    monkeypatch.setattr(codex_atomic, "_atomic_no_replace", no_replace)

    with pytest.raises(codex_config.CodexConfigError) as raised:
        _update(snapshot)

    assert raised.value.error_type == "concurrent_config_change"
    assert raised.value.details["config_committed"] is False
    assert config_path.read_bytes() == user_content
    proposed_path = Path(raised.value.details["preserved_config_path"])
    assert proposed_path.exists()
    assert b"url = " in proposed_path.read_bytes()


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EIO])
def test_preservation_write_failure_restores_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    project = tmp_path / "project"
    config_dir = project / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        '[mcp_servers.nomad]\ncommand = "nomad"\n',
        encoding="utf-8",
    )
    snapshot = codex_config._read_project_config(project)
    user_content = b"# concurrent user config\n"
    original_exchange = codex_atomic._atomic_exchange
    raced = False

    def exchange(directory_fd: int, first: str, second: str) -> None:
        nonlocal raced
        if not raced:
            raced = True
            _replace_config_at(directory_fd, user_content)
        original_exchange(directory_fd, first, second)

    def fail_copy(*args, **kwargs) -> str:
        raise OSError(error_number, os.strerror(error_number))

    monkeypatch.setattr(codex_atomic, "_atomic_exchange", exchange)
    monkeypatch.setattr(codex_atomic, "_copy_config_at", fail_copy)

    with pytest.raises(codex_config.CodexConfigError) as raised:
        _update(snapshot)

    error = raised.value
    assert error.error_type == "config_conflict_recovery_required"
    assert error.details["config_committed"] is False
    assert config_path.read_bytes() == user_content
    proposed_path = Path(error.details["preserved_config_path"])
    assert proposed_path.exists()
    assert b"url = " in proposed_path.read_bytes()
    retained_paths = error.details["preserved_concurrent_config_paths"]
    assert str(config_path) in retained_paths
    assert all(Path(path).exists() for path in retained_paths)


def test_preservation_and_restore_failure_retains_displaced_user_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    config_dir = project / ".codex"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    config_path.write_text(
        '[mcp_servers.nomad]\ncommand = "nomad"\n',
        encoding="utf-8",
    )
    snapshot = codex_config._read_project_config(project)
    user_content = b"# user config retained after failed restore\n"
    original_exchange = codex_atomic._atomic_exchange
    exchange_count = 0

    def exchange(directory_fd: int, first: str, second: str) -> None:
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            _replace_config_at(directory_fd, user_content)
            original_exchange(directory_fd, first, second)
            return
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    def fail_copy(*args, **kwargs) -> str:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(codex_atomic, "_atomic_exchange", exchange)
    monkeypatch.setattr(codex_atomic, "_copy_config_at", fail_copy)

    with pytest.raises(codex_config.CodexConfigError) as raised:
        _update(snapshot)

    error = raised.value
    assert error.error_type == "config_conflict_recovery_required"
    assert error.details["config_committed"] is True
    assert b"url = " in config_path.read_bytes()
    proposed_path = Path(error.details["preserved_config_path"])
    assert proposed_path.exists()
    retained_paths = [
        Path(path)
        for path in error.details["preserved_concurrent_config_paths"]
    ]
    assert retained_paths
    assert all(path.exists() for path in retained_paths)
    assert user_content in [path.read_bytes() for path in retained_paths]
