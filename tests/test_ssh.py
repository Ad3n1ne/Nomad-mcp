import pytest
import subprocess

from nomad.result import failure_result
from nomad.ssh import (
    CONTROLMASTER_ENV_VAR,
    SshConfigError,
    build_ssh_args,
    execute_remote_cmd_sync,
    probe_ssh_connectivity,
    probe_ssh_connectivity_result,
)


def test_build_ssh_args_includes_controlmaster_defaults():
    argv = build_ssh_args("gpu-host", timeout=7)

    assert argv == [
        "ssh",
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPath=/tmp/nomad_ssh_%C",
        "-o",
        "ControlPersist=60s",
        "-o",
        "ConnectTimeout=7",
        "-o",
        "BatchMode=yes",
        "gpu-host",
    ]


def test_build_ssh_args_can_disable_controlmaster(monkeypatch):
    monkeypatch.setenv(CONTROLMASTER_ENV_VAR, "0")

    argv = build_ssh_args("gpu-host", timeout=7)

    assert "ControlMaster=no" in argv
    assert not any(arg.startswith("ControlPath=") for arg in argv)
    assert "ControlMaster=auto" not in argv


def test_build_ssh_args_appends_jump_host():
    argv = build_ssh_args("gpu-host", jump_host="bastion")

    assert "-J" in argv
    assert argv[argv.index("-J") + 1] == "bastion"
    assert argv[-1] == "gpu-host"


def test_build_ssh_args_rejects_jump_host_with_proxy():
    with pytest.raises(SshConfigError, match="jump_host"):
        build_ssh_args(
            "gpu-host",
            jump_host="bastion",
            use_proxy_for_ssh=True,
            proxy_snapshot={"proxy_url": "socks5://127.0.0.1:7890"},
        )


@pytest.mark.parametrize(
    ("proxy_url", "expected_proxy_command"),
    [
        (
            "socks5://127.0.0.1:7890",
            "ProxyCommand=nc -X 5 -x 127.0.0.1:7890 %h %p",
        ),
        (
            "socks4://127.0.0.1:7891",
            "ProxyCommand=nc -X 4 -x 127.0.0.1:7891 %h %p",
        ),
    ],
)
def test_build_ssh_args_supports_socks_proxy_snapshot(
    proxy_url, expected_proxy_command
):
    argv = build_ssh_args(
        "gpu-host",
        use_proxy_for_ssh=True,
        proxy_snapshot={"proxy_url": proxy_url},
    )

    assert "-o" in argv
    assert expected_proxy_command in argv
    assert argv[-1] == "gpu-host"


def test_build_ssh_args_supports_local_proxy_port_snapshot():
    argv = build_ssh_args(
        "gpu-host",
        use_proxy_for_ssh=True,
        proxy_snapshot={"proxy_port": 7890},
    )

    assert "ProxyCommand=nc -X 5 -x 127.0.0.1:7890 %h %p" in argv


def test_build_ssh_args_rejects_unsupported_proxy_scheme():
    with pytest.raises(SshConfigError, match="unsupported proxy"):
        build_ssh_args(
            "gpu-host",
            use_proxy_for_ssh=True,
            proxy_snapshot={"proxy_url": "http://127.0.0.1:7890"},
        )


def test_build_ssh_args_rejects_missing_proxy_snapshot():
    with pytest.raises(SshConfigError, match="proxy"):
        build_ssh_args("gpu-host", use_proxy_for_ssh=True, proxy_snapshot={})


@pytest.mark.parametrize(
    "ssh_host",
    [
        "",
        "-oProxyCommand=touch /tmp/pwn",
        "gpu\nhost",
        "gpu host",
        "gpu;host",
    ],
)
def test_build_ssh_args_rejects_unsafe_ssh_host(ssh_host):
    with pytest.raises(SshConfigError, match="ssh_host"):
        build_ssh_args(ssh_host)


@pytest.mark.parametrize(
    "jump_host",
    [
        "",
        "-oProxyCommand=touch /tmp/pwn",
        "jump\nhost",
        "jump host",
        "jump|host",
    ],
)
def test_build_ssh_args_rejects_unsafe_jump_host(jump_host):
    with pytest.raises(SshConfigError, match="jump_host"):
        build_ssh_args("gpu-host", jump_host=jump_host)


@pytest.mark.parametrize(
    "proxy_url",
    [
        "socks5://-proxy:7890",
        "socks5://proxy%0Ahost:7890",
        "socks5://proxy%3Bhost:7890",
    ],
)
def test_build_ssh_args_rejects_unsafe_proxy_hostname(proxy_url):
    with pytest.raises(SshConfigError, match="proxy host"):
        build_ssh_args(
            "gpu-host",
            use_proxy_for_ssh=True,
            proxy_snapshot={"proxy_url": proxy_url},
        )


def test_build_ssh_args_returns_argv_list_without_shell_string():
    argv = build_ssh_args("gpu-host")

    assert isinstance(argv, list)
    assert all(isinstance(item, str) for item in argv)
    assert " ".join(argv) != argv


def test_probe_ssh_connectivity_result_success(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = probe_ssh_connectivity_result("gpu-host", timeout=3)

    assert result["ok"] is True
    assert result["error_type"] is None
    assert calls[0][0][0][-2:] == ["gpu-host", "echo ok"]
    assert calls[0][1]["timeout"] == 3
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["text"] is True


def test_probe_ssh_connectivity_keeps_bool_stub_compatibility(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="ok", stderr=""),
    )

    assert probe_ssh_connectivity("gpu-host", timeout=3) is True


def test_probe_ssh_connectivity_result_timeout(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = probe_ssh_connectivity_result("gpu-host", timeout=3)

    assert result["ok"] is False
    assert result["error_type"] == "ssh_timeout"
    assert result["recoverable"] is True
    failure_result(
        tool="init_probe_target",
        error_type=result["error_type"],
        message="SSH probe failed.",
        recoverable=result["recoverable"],
        diagnostics=result["diagnostics"],
    )


def test_probe_ssh_connectivity_result_oserror(monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("ssh missing")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = probe_ssh_connectivity_result("gpu-host", timeout=3)

    assert result["ok"] is False
    assert result["error_type"] == "ssh_unknown_failure"
    assert "ssh missing" in result["diagnostics"][0]


@pytest.mark.parametrize(
    ("stderr", "error_type"),
    [
        ("Permission denied (publickey).", "ssh_auth_failed"),
        ("Host key verification failed.", "ssh_host_key_failed"),
        ("ssh: connect to host gpu port 22: Connection refused", "ssh_connection_refused"),
        ("some unknown ssh problem", "ssh_unknown_failure"),
    ],
)
def test_probe_ssh_connectivity_result_classifies_failures(
    monkeypatch, stderr, error_type
):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 255, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = probe_ssh_connectivity_result("gpu-host", timeout=3)

    assert result["ok"] is False
    assert result["error_type"] == error_type
    assert stderr in result["diagnostics"][0]
    failure_result(
        tool="init_probe_target",
        error_type=result["error_type"],
        message="SSH probe failed.",
        recoverable=result["recoverable"],
        diagnostics=result["diagnostics"],
    )


def test_probe_ssh_connectivity_result_maps_builder_errors_to_invalid_config():
    result = probe_ssh_connectivity_result("-oProxyCommand=touch /tmp/pwn", timeout=3)

    assert result["ok"] is False
    assert result["error_type"] == "invalid_config"
    assert result["recoverable"] is True
    assert "ssh_host" in result["diagnostics"][0]
    failure_result(
        tool="init_probe_target",
        error_type=result["error_type"],
        message="SSH probe failed.",
        recoverable=result["recoverable"],
        diagnostics=result["diagnostics"],
    )


def test_execute_remote_cmd_sync_maps_oserror_to_failed_tuple(monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("ssh missing")

    monkeypatch.setattr(subprocess, "run", fake_run)

    returncode, stdout, stderr = execute_remote_cmd_sync("gpu-host", "pwd")

    assert returncode == 255
    assert stdout == ""
    assert "ssh missing" in stderr
