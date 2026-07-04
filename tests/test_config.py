import json
import os
import time

import pytest

from nomad.config import (
    ConfigError,
    guard_remote,
    load_config,
    resolve_target,
    validate_config,
)


def test_load_config_returns_unconfigured_without_nomad_file(temp_workdir):
    config = load_config()

    assert config == {"mode": "unconfigured"}


def test_load_config_supports_local_mode(write_nomad_config):
    write_nomad_config({"mode": "local", "project_name": "demo"})

    config = load_config()

    assert config["mode"] == "local"
    assert config["project_name"] == "demo"
    assert config["targets"] == {}
    assert guard_remote(config) == "local_mode"


def test_load_config_wraps_invalid_json_as_config_error(temp_workdir):
    (temp_workdir / ".nomad.json").write_text("{bad json", encoding="utf-8")

    with pytest.raises(ConfigError, match="failed to parse"):
        load_config()


def test_guard_remote_allows_remote_mode(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                }
            },
        }
    )

    config = load_config()

    assert guard_remote(config) is None


def test_guard_remote_rejects_unconfigured(temp_workdir):
    assert guard_remote(load_config()) == "unconfigured"


def test_resolve_target_uses_single_target_default_fallback(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "default_target": None,
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                }
            },
        }
    )

    target = resolve_target(load_config())

    assert target["ssh_host"] == "devbox"
    assert target["remote_path"] == "/workspace/demo"


def test_resolve_target_uses_multi_target_default(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "default_target": "cpu",
            "targets": {
                "gpu": {
                    "ssh_host": "gpu-host",
                    "remote_path": "/workspace/gpu",
                },
                "cpu": {
                    "ssh_host": "cpu-host",
                    "remote_path": "/workspace/cpu",
                },
            },
        }
    )

    config = load_config()

    assert resolve_target(config)["ssh_host"] == "cpu-host"
    assert resolve_target(config, "gpu")["ssh_host"] == "gpu-host"


def test_resolve_target_raises_for_missing_target(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "gpu-host",
                    "remote_path": "/workspace/gpu",
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="target not found"):
        resolve_target(load_config(), "cpu")


def test_load_config_reloads_when_mtime_changes(write_nomad_config):
    config_path = write_nomad_config({"mode": "local", "project_name": "first"})
    assert load_config()["project_name"] == "first"

    config_path.write_text(
        json.dumps({"mode": "local", "project_name": "second"}),
        encoding="utf-8",
    )
    next_mtime = time.time() + 2
    os.utime(config_path, (next_mtime, next_mtime))

    assert load_config()["project_name"] == "second"


def test_remote_target_defaults_are_normalized(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    "network": {"use_proxy_for_ssh": True},
                    "sync": {"extra_excludes": ["data/"]},
                    "runtime": {"interpreter": "python"},
                    "limits": {"max_output_lines": 50},
                }
            },
        }
    )

    target = resolve_target(load_config(), "gpu")

    assert target["description"] == ""
    assert target["local_subpath"] is None
    assert target["auto_create_remote_path"] is True
    assert target["network"] == {
        "use_proxy_for_ssh": True,
        "jump_host": None,
        "reverse_tunnel": {
            "enabled": False,
            "proxy_scheme": "socks5",
        },
    }
    assert target["sync"] == {
        "respect_gitignore": True,
        "extra_excludes": ["data/"],
    }
    assert target["runtime"] == {
        "interpreter": "python",
        "extra_env": {},
    }
    assert target["limits"] == {
        "command_timeout_seconds": 60,
        "max_output_lines": 50,
        "max_output_bytes": 10240,
    }


def test_validate_config_rejects_invalid_project_name(write_nomad_config):
    write_nomad_config({"mode": "local", "project_name": "bad name"})

    with pytest.raises(ConfigError, match="project_name"):
        load_config()


def test_validate_config_requires_project_name_for_local_mode():
    with pytest.raises(ConfigError, match="project_name"):
        validate_config({"mode": "local", "targets": {}})


def test_validate_config_rejects_invalid_target_name(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "bad name": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="target name"):
        load_config()


def test_validate_config_rejects_reserved_target_name(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "default": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="reserved"):
        load_config()


def test_validate_config_requires_remote_targets(write_nomad_config):
    write_nomad_config({"mode": "remote", "project_name": "demo", "targets": {}})

    with pytest.raises(ConfigError, match="at least one target"):
        load_config()


def test_validate_config_requires_valid_default_target_for_multi_target(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "default_target": "missing",
            "targets": {
                "gpu": {
                    "ssh_host": "gpu-host",
                    "remote_path": "/workspace/gpu",
                },
                "cpu": {
                    "ssh_host": "cpu-host",
                    "remote_path": "/workspace/cpu",
                },
            },
        }
    )

    with pytest.raises(ConfigError, match="default_target"):
        load_config()


@pytest.mark.parametrize(
    ("remote_path", "message"),
    [
        ("workspace/demo", "remote_path"),
        ("/var/demo", "remote_path"),
        ("/root", "remote_path"),
        ("/home/user", "remote_path"),
    ],
)
def test_validate_config_rejects_invalid_remote_path(
    write_nomad_config, remote_path, message
):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": remote_path,
                }
            },
        }
    )

    with pytest.raises(ConfigError, match=message):
        load_config()


@pytest.mark.parametrize("local_subpath", ["/absolute", "../outside", "nested/../out"])
def test_validate_config_rejects_invalid_local_subpath(
    write_nomad_config, local_subpath
):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    "local_subpath": local_subpath,
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="local_subpath"):
        load_config()


def test_validate_config_rejects_null_byte_in_local_subpath(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    "local_subpath": "bad\u0000path",
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="local_subpath"):
        load_config()


def test_validate_config_rejects_jump_host_with_proxy(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    "network": {
                        "use_proxy_for_ssh": True,
                        "jump_host": "bastion",
                    },
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="jump_host"):
        load_config()


@pytest.mark.parametrize("port", [0, 65536, "8080"])
def test_validate_config_rejects_invalid_reverse_tunnel_port(
    write_nomad_config, port
):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    "network": {
                        "reverse_tunnel": {
                            "enabled": True,
                            "local_proxy_port": port,
                        }
                    },
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="reverse_tunnel"):
        load_config()


def test_validate_config_rejects_privileged_reverse_tunnel_remote_port(
    write_nomad_config,
):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    "network": {
                        "reverse_tunnel": {
                            "enabled": True,
                            "local_proxy_port": 7890,
                            "remote_bind_port": 443,
                        }
                    },
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="remote_bind_port"):
        load_config()


def test_load_config_rejects_target_value_that_is_not_object(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {"gpu": "bad"},
        }
    )

    with pytest.raises(ConfigError, match="target gpu"):
        load_config()


@pytest.mark.parametrize("field", ["network", "sync", "runtime", "limits"])
def test_load_config_rejects_nested_target_object_with_wrong_type(
    write_nomad_config, field
):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    field: "bad",
                }
            },
        }
    )

    with pytest.raises(ConfigError, match=field):
        load_config()


def test_load_config_rejects_reverse_tunnel_with_wrong_type(write_nomad_config):
    write_nomad_config(
        {
            "mode": "remote",
            "project_name": "demo",
            "targets": {
                "gpu": {
                    "ssh_host": "devbox",
                    "remote_path": "/workspace/demo",
                    "network": {"reverse_tunnel": "bad"},
                }
            },
        }
    )

    with pytest.raises(ConfigError, match="reverse_tunnel"):
        load_config()
