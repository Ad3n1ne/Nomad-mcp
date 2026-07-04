"""
Command line entry point for nomad.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from importlib.util import find_spec

from nomad import __version__
from nomad.schema import get_config_schema_hints


PACKAGE_NAME = "nomad-mcp"


def main(argv: list[str] | None = None) -> int | None:
    """Runs helper CLI commands, or starts the MCP server when no args are given."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        from nomad.server import main as server_main

        server_main()
        return None

    parser = _build_parser()
    args = parser.parse_args(args_list)

    if args.version:
        print(__version__)
        return 0

    if args.command == "doctor":
        return _doctor()
    if args.command == "schema":
        print(json.dumps(get_config_schema_hints(), indent=2))
        return 0
    if args.command == "client-config":
        print(_client_config(args.runner, args.format))
        return 0

    parser.print_help()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nomad",
        description="Local MCP server for agentic remote development.",
    )
    parser.add_argument("--version", action="store_true", help="Print nomad version.")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("doctor", help="Check local runtime dependencies.")
    subparsers.add_parser("schema", help="Print .nomad.json schema hints as JSON.")

    config_parser = subparsers.add_parser(
        "client-config", help="Print an MCP client configuration snippet."
    )
    config_parser.add_argument(
        "--runner",
        choices=("uvx", "nomad"),
        default="uvx",
        help="Use uvx package execution or an already-installed nomad command.",
    )
    config_parser.add_argument(
        "--format",
        choices=("json", "toml"),
        default="json",
        help="Output config format.",
    )
    return parser


def _doctor() -> int:
    checks = [
        ("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0]),
        ("mcp package", find_spec("mcp") is not None, "import mcp"),
        ("ssh", shutil.which("ssh") is not None, shutil.which("ssh") or "missing"),
        ("rsync", shutil.which("rsync") is not None, shutil.which("rsync") or "missing"),
    ]
    ok = True
    for name, passed, detail in checks:
        ok = ok and passed
        mark = "ok" if passed else "missing"
        print(f"{mark:7} {name}: {detail}")
    print("note    remote tmux is required only when using task_start/task_status.")
    return 0 if ok else 1


def _client_config(runner: str, output_format: str) -> str:
    if runner == "uvx":
        command = "uvx"
        args = [PACKAGE_NAME]
    else:
        command = "nomad"
        args = []

    if output_format == "toml":
        rendered_args = ", ".join(json.dumps(arg) for arg in args)
        return (
            "[mcp_servers.nomad]\n"
            f"command = {json.dumps(command)}\n"
            f"args = [{rendered_args}]\n"
            "startup_timeout_sec = 120"
        )

    return json.dumps(
        {
            "mcpServers": {
                "nomad": {
                    "command": command,
                    "args": args,
                }
            }
        },
        indent=2,
    )


if __name__ == "__main__":
    raise SystemExit(main())
