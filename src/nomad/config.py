"""
Configuration loader, parser and schema validation for nomad (.nomad.json).
"""

from __future__ import annotations

import copy
import json
import os
import re

from pathlib import Path
from pathlib import PurePosixPath
from typing import Any


CONFIG_FILENAME = ".nomad.json"
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")
TARGET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,30}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
RESERVED_TARGET_NAMES = {"default", "all", "local"}

REMOTE_PATH_PREFIXES = (
    "/home/",
    "/root/",
    "/workspace/",
    "/data/",
    "/tmp/",
    "/opt/",
)

_CONFIG_CACHE: dict[str, Any] | None = None
_CONFIG_CACHE_PATH: Path | None = None
_CONFIG_CACHE_MTIME_NS: int | None = None


class ConfigError(Exception):
    """Raised when a config cannot satisfy the requested operation."""


def load_config() -> dict[str, Any]:
    """Loads .nomad.json with caching and hot-reload based on file mtime."""
    global _CONFIG_CACHE, _CONFIG_CACHE_PATH, _CONFIG_CACHE_MTIME_NS

    config_path = Path.cwd() / CONFIG_FILENAME
    if not config_path.exists():
        _CONFIG_CACHE = {"mode": "unconfigured"}
        _CONFIG_CACHE_PATH = config_path
        _CONFIG_CACHE_MTIME_NS = None
        return copy.deepcopy(_CONFIG_CACHE)

    stat = config_path.stat()
    if (
        _CONFIG_CACHE is not None
        and _CONFIG_CACHE_PATH == config_path
        and _CONFIG_CACHE_MTIME_NS == stat.st_mtime_ns
    ):
        return copy.deepcopy(_CONFIG_CACHE)

    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"failed to parse {CONFIG_FILENAME}: {exc}") from exc

    normalized = _normalize_config(raw_config)
    validate_config(normalized)
    _CONFIG_CACHE = normalized
    _CONFIG_CACHE_PATH = config_path
    _CONFIG_CACHE_MTIME_NS = stat.st_mtime_ns
    return copy.deepcopy(normalized)


def validate_config(config: dict[str, Any]) -> None:
    """Validates the normalized .nomad.json structure used by runtime tools."""
    mode = config.get("mode")
    if mode == "unconfigured":
        return
    if mode not in {"local", "remote"}:
        raise ConfigError("mode must be 'local' or 'remote'")

    _validate_project_name(config.get("project_name"))
    targets = config.get("targets") or {}
    if not isinstance(targets, dict):
        raise ConfigError("targets must be an object")

    if mode == "local":
        return

    if not targets:
        raise ConfigError("remote mode requires at least one target")

    if len(targets) > 1 and config.get("default_target") not in targets:
        raise ConfigError("default_target must reference an existing target")

    for target_name, target in targets.items():
        _validate_target_name(target_name)
        _validate_target(target_name, target)


def guard_remote(config: dict[str, Any]) -> str | None:
    """Ensures remote execution is allowed according to the mode."""
    mode = config.get("mode")
    if mode == "remote":
        return None
    if mode == "unconfigured":
        return "unconfigured"
    if mode == "local":
        return "local_mode"
    return "invalid_config"


def resolve_target(
    config: dict[str, Any], target_name: str = "default"
) -> dict[str, Any]:
    """Resolves target config with default fallback logic."""
    _, target = resolve_target_with_name(config, target_name)
    return target


def resolve_target_with_name(
    config: dict[str, Any], target_name: str = "default"
) -> tuple[str, dict[str, Any]]:
    """Resolves target config and returns the real target key used in config."""
    targets = config.get("targets") or {}
    if not isinstance(targets, dict) or not targets:
        raise ConfigError("target not found: no targets configured")

    resolved_name = target_name
    if target_name == "default":
        default_target = config.get("default_target")
        if default_target:
            resolved_name = default_target
        elif len(targets) == 1:
            resolved_name = next(iter(targets))
        else:
            raise ConfigError("target not found: default target is not configured")

    if resolved_name not in targets:
        raise ConfigError(f"target not found: {resolved_name}")

    return resolved_name, copy.deepcopy(targets[resolved_name])


def _normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigError("config must be an object")
    normalized = copy.deepcopy(config)
    normalized.setdefault("default_target", None)
    targets = normalized.get("targets") or {}
    if not isinstance(targets, dict):
        raise ConfigError("targets must be an object")
    normalized["targets"] = {
        name: _normalize_target(name, target) for name, target in targets.items()
    }
    return normalized


def _normalize_target(target_name: str, target: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise ConfigError(f"target {target_name} must be an object")
    normalized = copy.deepcopy(target)
    normalized.setdefault("description", "")
    normalized.setdefault("local_subpath", None)
    normalized.setdefault("auto_create_remote_path", True)
    normalized["network"] = _merge_defaults(
        normalized.get("network"),
        {
            "use_proxy_for_ssh": False,
            "jump_host": None,
            "reverse_tunnel": {
                "enabled": False,
                "proxy_scheme": "socks5",
            },
        },
        f"target {target_name} network",
    )
    _normalize_reverse_tunnel_defaults(normalized["network"]["reverse_tunnel"])
    normalized["sync"] = _merge_defaults(
        normalized.get("sync"),
        {
            "respect_gitignore": True,
            "extra_excludes": [],
        },
        f"target {target_name} sync",
    )
    normalized["runtime"] = _merge_defaults(
        normalized.get("runtime"),
        {
            "interpreter": None,
            "extra_env": {},
        },
        f"target {target_name} runtime",
    )
    normalized["limits"] = _merge_defaults(
        normalized.get("limits"),
        {
            "command_timeout_seconds": 60,
            "max_output_lines": 200,
            "max_output_bytes": 10240,
        },
        f"target {target_name} limits",
    )
    return normalized


def _merge_defaults(
    value: dict[str, Any] | None, defaults: dict[str, Any], field_path: str
) -> dict[str, Any]:
    if value is not None and not isinstance(value, dict):
        raise ConfigError(f"{field_path} must be an object")
    merged = copy.deepcopy(defaults)
    for key, item in (value or {}).items():
        if isinstance(item, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(item, merged[key], f"{field_path}.{key}")
        elif isinstance(merged.get(key), dict) and item is not None:
            raise ConfigError(f"{field_path}.{key} must be an object")
        else:
            merged[key] = copy.deepcopy(item)
    return merged


def _normalize_reverse_tunnel_defaults(reverse_tunnel: dict[str, Any]) -> None:
    if not isinstance(reverse_tunnel, dict):
        raise ConfigError("reverse_tunnel must be an object")
    if (
        reverse_tunnel.get("enabled") is True
        and "local_proxy_port" in reverse_tunnel
        and "remote_bind_port" not in reverse_tunnel
    ):
        reverse_tunnel["remote_bind_port"] = reverse_tunnel["local_proxy_port"]


def _validate_project_name(project_name: Any) -> None:
    if not isinstance(project_name, str) or not NAME_RE.fullmatch(project_name):
        raise ConfigError("project_name must match ^[a-zA-Z0-9_-]{1,50}$")


def _validate_target_name(target_name: Any) -> None:
    if not isinstance(target_name, str) or not TARGET_NAME_RE.fullmatch(target_name):
        raise ConfigError("target name must match ^[a-zA-Z0-9_-]{1,30}$")
    if target_name in RESERVED_TARGET_NAMES:
        raise ConfigError(f"target name is reserved: {target_name}")


def _validate_target(target_name: str, target: Any) -> None:
    if not isinstance(target, dict):
        raise ConfigError(f"target {target_name} must be an object")
    _validate_remote_path(target_name, target.get("remote_path"))
    _validate_local_subpath(target_name, target.get("local_subpath"))
    _validate_network(target_name, target.get("network") or {})
    _validate_runtime(target_name, target.get("runtime") or {})


def _validate_runtime(target_name: str, runtime: Any) -> None:
    if not isinstance(runtime, dict):
        raise ConfigError(f"target {target_name} runtime must be an object")
    extra_env = runtime.get("extra_env")
    if extra_env is not None:
        if not isinstance(extra_env, dict):
            raise ConfigError(f"target {target_name} extra_env must be an object")
        for key, val in extra_env.items():
            if not isinstance(key, str) or not ENV_KEY_RE.fullmatch(key):
                raise ConfigError(
                    f"target {target_name} extra_env key '{key}' is invalid, must match ^[A-Z_][A-Z0-9_]*$"
                )
            if not isinstance(val, str):
                raise ConfigError(
                    f"target {target_name} extra_env value for '{key}' must be a string"
                )



def _validate_remote_path(target_name: str, remote_path: Any) -> None:
    if not isinstance(remote_path, str):
        raise ConfigError(f"target {target_name} remote_path must be a string")
    path = PurePosixPath(remote_path)
    if not path.is_absolute():
        raise ConfigError(f"target {target_name} remote_path must be absolute")
    if not remote_path.startswith(REMOTE_PATH_PREFIXES):
        raise ConfigError(f"target {target_name} remote_path prefix is not allowed")
    if _is_home_root(path):
        raise ConfigError(f"target {target_name} remote_path cannot be a home root")


def _is_home_root(path: PurePosixPath) -> bool:
    parts = path.parts
    return parts == ("/", "root") or (
        len(parts) == 3 and parts[0] == "/" and parts[1] == "home"
    )


def _validate_local_subpath(target_name: str, local_subpath: Any) -> None:
    if local_subpath is None:
        return
    if not isinstance(local_subpath, str):
        raise ConfigError(f"target {target_name} local_subpath must be a string")
    path = PurePosixPath(local_subpath)
    if "\x00" in local_subpath or path.is_absolute() or ".." in path.parts:
        raise ConfigError(
            f"target {target_name} local_subpath must be relative and stay inside cwd"
        )


def _validate_network(target_name: str, network: dict[str, Any]) -> None:
    if network.get("use_proxy_for_ssh") is True and network.get("jump_host"):
        raise ConfigError(
            f"target {target_name} jump_host conflicts with use_proxy_for_ssh"
        )
    _validate_reverse_tunnel(target_name, network.get("reverse_tunnel") or {})


def _validate_reverse_tunnel(
    target_name: str, reverse_tunnel: dict[str, Any]
) -> None:
    if reverse_tunnel.get("enabled") is not True:
        return

    proxy_scheme = reverse_tunnel.get("proxy_scheme")
    if proxy_scheme not in {"socks5", "http"}:
        raise ConfigError(
            f"target {target_name} reverse_tunnel proxy_scheme is not supported"
        )

    for key in ("local_proxy_port", "remote_bind_port"):
        port = reverse_tunnel.get(key)
        if not isinstance(port, int) or isinstance(port, bool):
            raise ConfigError(f"target {target_name} reverse_tunnel {key} must be int")
        if port < 1 or port > 65535:
            raise ConfigError(
                f"target {target_name} reverse_tunnel {key} must be 1-65535"
            )

    if reverse_tunnel["remote_bind_port"] < 1024:
        raise ConfigError(
            f"target {target_name} remote_bind_port below 1024 is not allowed"
        )


def invalidate_config_cache() -> None:
    """Invalidates the config cache so that subsequent load_config() calls reload from disk."""
    global _CONFIG_CACHE, _CONFIG_CACHE_PATH, _CONFIG_CACHE_MTIME_NS
    _CONFIG_CACHE = None
    _CONFIG_CACHE_PATH = None
    _CONFIG_CACHE_MTIME_NS = None


def save_config_file(config: dict[str, Any]) -> Path:
    """Normalizes, validates and writes config to .nomad.json atomically with backup."""
    normalized = _normalize_config(config)
    validate_config(normalized)

    cwd = Path.cwd()
    config_path = cwd / CONFIG_FILENAME
    bak_path = cwd / f"{CONFIG_FILENAME}.bak"
    tmp_path = cwd / f".{CONFIG_FILENAME}.tmp.{os.getpid()}"

    if config_path.exists():
        bak_path.write_text(config_path.read_text(encoding="utf-8"), encoding="utf-8")

    content = json.dumps(normalized, ensure_ascii=False, indent=2)
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(config_path)

    invalidate_config_cache()
    return config_path

