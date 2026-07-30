import json
import os
import sys

import anyio
import httpx
from nomad import daemon
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

from nomad.server import create_server


GENERIC_CLIENT = Implementation(name="generic-mcp-host", version="1.0")


def _assert_standard_session(initialize_result, tools_result, health_result):
    assert initialize_result.serverInfo.name == "nomad"
    assert "health" in {tool.name for tool in tools_result.tools}

    text_content = next(
        content.text for content in health_result.content if hasattr(content, "text")
    )
    payload = json.loads(text_content)
    assert payload["ok"] is True
    assert payload["tool"] == "health"
    assert isinstance(payload["data"]["pid"], int)


def test_generic_mcp_client_can_use_stdio_entrypoint(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nomad.cli"],
        cwd=workspace,
        env={"NOMAD_MCP_LOG_PATH": str(tmp_path / "stdio-mcp.log")},
    )

    async def scenario():
        with anyio.fail_after(20):
            async with stdio_client(parameters) as streams:
                async with ClientSession(
                    streams[0],
                    streams[1],
                    client_info=GENERIC_CLIENT,
                ) as session:
                    initialize_result = await session.initialize()
                    tools_result = await session.list_tools()
                    health_result = await session.call_tool("health")

        _assert_standard_session(
            initialize_result,
            tools_result,
            health_result,
        )

    anyio.run(scenario)


def test_generic_mcp_client_can_use_streamable_http_daemon(tmp_path, monkeypatch):
    token = "generic-client-test-token"
    monkeypatch.setenv(
        "NOMAD_MCP_LOG_PATH",
        str(tmp_path / "streamable-http-mcp.log"),
    )
    app = create_server(bearer_token=token).streamable_http_app()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": f"Bearer {token}"},
            ) as http_client:
                async with streamable_http_client(
                    "http://127.0.0.1:8765/mcp",
                    http_client=http_client,
                ) as streams:
                    async with ClientSession(
                        streams[0],
                        streams[1],
                        client_info=GENERIC_CLIENT,
                    ) as session:
                        initialize_result = await session.initialize()
                        tools_result = await session.list_tools()
                        health_result = await session.call_tool("health")

        _assert_standard_session(
            initialize_result,
            tools_result,
            health_result,
        )

    anyio.run(scenario)


def test_generic_mcp_client_can_use_real_daemon_process(
    tmp_path,
    monkeypatch,
    free_tcp_port_factory,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    isolated_home = tmp_path / "home"
    isolated_home.mkdir(mode=0o700)
    daemon_home = isolated_home / ".nomad" / "daemons"
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(path for path in sys.path if path),
    )
    monkeypatch.setattr(daemon, "DEFAULT_DAEMONS_DIR", daemon_home)

    started = None
    stopped = None
    try:
        started = daemon.start_daemon(
            project=workspace,
            port=free_tcp_port_factory(),
        )
        token = daemon.read_daemon_token(project=workspace)

        async def scenario():
            with anyio.fail_after(20):
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(10),
                ) as http_client:
                    async with streamable_http_client(
                        started["url"],
                        http_client=http_client,
                    ) as streams:
                        async with ClientSession(
                            streams[0],
                            streams[1],
                            client_info=GENERIC_CLIENT,
                        ) as session:
                            initialize_result = await session.initialize()
                            tools_result = await session.list_tools()
                            health_result = await session.call_tool("health")

            _assert_standard_session(
                initialize_result,
                tools_result,
                health_result,
            )
            health_payload = json.loads(health_result.content[0].text)
            assert health_payload["data"]["pid"] == started["pid"]

        anyio.run(scenario)
    finally:
        if started is not None:
            stopped = daemon.stop_daemon(
                project=workspace,
                expected_instance_id=started["instance_id"],
            )

    assert stopped is not None
    assert stopped["status"] == "stopped"
    assert stopped["running"] is False
