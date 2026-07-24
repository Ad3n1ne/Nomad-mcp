"""
Command line entry point for nomad.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from importlib.util import find_spec

from nomad import __version__
from nomad.schema import get_config_schema_hints


PACKAGE_NAME = "nomad-mcp"
GITHUB_PACKAGE_URL = "git+https://github.com/Ad3n1ne/Nomad-mcp.git"


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
        return _doctor(kill_stale_mcp=args.kill_stale_mcp, dry_run=args.dry_run)
    if args.command == "schema":
        print(json.dumps(get_config_schema_hints(), indent=2))
        return 0
    if args.command == "client-config":
        print(_client_config(args.runner, args.format))
        return 0
    if args.command == "serve":
        from nomad.server import main as server_main

        server_main(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            path=args.path,
        )
        return None
    if args.command == "daemon":
        return _run_daemon_command(args)

    parser.print_help()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nomad",
        description="Local MCP server for agentic remote development.",
    )
    parser.add_argument("--version", action="store_true", help="Print nomad version.")

    subparsers = parser.add_subparsers(dest="command")
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check local runtime dependencies and optional MCP recovery."
    )
    doctor_parser.add_argument(
        "--kill-stale-mcp",
        action="store_true",
        help="Kill Nomad MCP processes spawned by Codex/ChatGPT so the client can reconnect.",
    )
    doctor_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show stale MCP processes without killing them.",
    )
    subparsers.add_parser("schema", help="Print .nomad.json schema hints as JSON.")

    config_parser = subparsers.add_parser(
        "client-config", help="Print an MCP client configuration snippet."
    )
    config_parser.add_argument(
        "--runner",
        choices=("uvx", "github", "nomad"),
        default="uvx",
        help="Use PyPI uvx, GitHub-tag uvx, or an already-installed nomad command.",
    )
    config_parser.add_argument(
        "--format",
        choices=("json", "toml"),
        default="json",
        help="Output config format.",
    )

    serve_parser = subparsers.add_parser(
        "serve", help="Run Nomad as a foreground Streamable HTTP MCP server."
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen host (default: 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port",
        type=_valid_port,
        default=8765,
        help="Listen port from 1 to 65535 (default: 8765).",
    )
    serve_parser.add_argument(
        "--path",
        type=_valid_path,
        default="/mcp",
        help="Streamable HTTP endpoint path (default: /mcp).",
    )
    serve_parser.add_argument("--daemon-id", help=argparse.SUPPRESS)

    daemon_parser = subparsers.add_parser(
        "daemon", help="Manage a persistent project-scoped Nomad MCP server."
    )
    daemon_subparsers = daemon_parser.add_subparsers(
        dest="daemon_command",
        required=True,
    )
    daemon_start_parser = daemon_subparsers.add_parser(
        "start", help="Start the project daemon."
    )
    _add_project_argument(daemon_start_parser)
    daemon_start_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen host (default: 127.0.0.1).",
    )
    daemon_start_parser.add_argument(
        "--port",
        type=_valid_port,
        default=8765,
        help="Listen port from 1 to 65535 (default: 8765).",
    )
    daemon_start_parser.add_argument(
        "--path",
        type=_valid_path,
        default="/mcp",
        help="Streamable HTTP endpoint path (default: /mcp).",
    )
    daemon_start_parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Explicitly allow a non-loopback listen host.",
    )
    for daemon_command in ("status", "restart", "stop"):
        command_parser = daemon_subparsers.add_parser(
            daemon_command,
            help=f"{daemon_command.capitalize()} the project daemon.",
        )
        _add_project_argument(command_parser)
    return parser


def _add_project_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        help="Project root (default: current working directory).",
    )


def _run_daemon_command(args: argparse.Namespace) -> int:
    from nomad.daemon import (
        DaemonError,
        restart_daemon,
        start_daemon,
        status_daemon,
        stop_daemon,
    )

    try:
        if args.daemon_command == "start":
            result = start_daemon(
                project=args.project,
                host=args.host,
                port=args.port,
                path=args.path,
                allow_remote=args.allow_remote,
            )
        elif args.daemon_command == "status":
            result = status_daemon(project=args.project)
        elif args.daemon_command == "restart":
            result = restart_daemon(project=args.project)
        else:
            result = stop_daemon(project=args.project)
    except DaemonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _valid_path(value: str) -> str:
    if not value.startswith("/"):
        raise argparse.ArgumentTypeError("path must start with '/'")
    return value


def _doctor(*, kill_stale_mcp: bool = False, dry_run: bool = False) -> int:
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
    if kill_stale_mcp or dry_run:
        stale = _find_stale_mcp_processes()
        if not stale:
            print("mcp     no stale Codex-spawned Nomad MCP processes found.")
        for proc in stale:
            if dry_run:
                print(f"mcp     would kill pid={proc['pid']} ppid={proc['ppid']} command={proc['command']}")
                continue
            try:
                os.kill(proc["pid"], signal.SIGTERM)
            except ProcessLookupError:
                print(f"mcp     already exited pid={proc['pid']}")
            except PermissionError as exc:
                ok = False
                print(f"mcp     failed pid={proc['pid']}: {exc}")
            else:
                print(f"mcp     killed pid={proc['pid']} ppid={proc['ppid']}")
    return 0 if ok else 1


def _find_stale_mcp_processes() -> list[dict[str, int | str]]:
    """Finds Nomad MCP processes owned by Codex/ChatGPT, excluding this doctor run."""
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []

    rows: list[dict[str, int | str]] = []
    commands_by_pid: dict[int, str] = {}
    current_pid = os.getpid()
    for line in completed.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        command = parts[2]
        commands_by_pid[pid] = command
        rows.append({"pid": pid, "ppid": ppid, "command": command})

    stale: list[dict[str, int | str]] = []
    for row in rows:
        pid = int(row["pid"])
        ppid = int(row["ppid"])
        command = str(row["command"])
        parent_command = commands_by_pid.get(ppid, "")
        if pid == current_pid:
            continue
        if " doctor" in command or " --kill-stale-mcp" in command:
            continue
        if not _looks_like_nomad_mcp_command(command):
            continue
        if not _looks_like_codex_parent(parent_command):
            continue
        stale.append(row)
    return stale


def _looks_like_nomad_mcp_command(command: str) -> bool:
    if "nomad" not in command:
        return False
    if "python" in command and "/nomad" in command:
        return True
    return command.endswith("/nomad") or command.endswith(" nomad") or command == "nomad"


def _looks_like_codex_parent(command: str) -> bool:
    lowered = command.lower()
    return "codex" in lowered or "chatgpt.app" in lowered


def _client_config(runner: str, output_format: str) -> str:
    if runner == "uvx":
        command = "uvx"
        args = [PACKAGE_NAME]
    elif runner == "github":
        command = "uvx"
        args = ["--from", f"{GITHUB_PACKAGE_URL}@v{__version__}", "nomad"]
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
