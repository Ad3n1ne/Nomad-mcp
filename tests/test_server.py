import inspect
import json
import os
import threading

import anyio
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from nomad.server import (
    DEFAULT_HOST,
    DEFAULT_PATH,
    DEFAULT_PORT,
    _safe_resource,
    _safe_tool,
    create_server,
    get_current_project_resource,
    health,
    is_loopback_host,
    log_server_shutdown,
    main,
    mcp_server,
)


def run_async(async_callable, *args, **kwargs):
    async def runner():
        return await async_callable(*args, **kwargs)

    return anyio.run(runner)


def test_server_tools_registered():
    registered_tools = set(mcp_server._tool_manager._tools.keys())
    expected_phase1_tools = {
        "init_discover",
        "init_verify_and_probe",
        "init_save_config",
        "init_probe_target",
        "sync_push",
        "sync_pull",
        "run_remote",
        "tunnel_start",
        "tunnel_status",
        "tunnel_stop",
        "net_diagnose",
        "health",
    }
    expected_phase2_tools = {
        "task_start",
        "task_status",
        "task_list",
        "task_kill",
    }
    assert expected_phase1_tools.issubset(registered_tools)
    assert expected_phase2_tools.issubset(registered_tools)


def test_create_server_configures_http_and_preserves_registrations():
    server = create_server(host="localhost", port=9876, path="/nomad")

    assert server.settings.host == "localhost"
    assert server.settings.port == 9876
    assert server.settings.streamable_http_path == "/nomad"
    assert server.settings.stateless_http is True
    assert set(server._tool_manager._tools) == set(mcp_server._tool_manager._tools)
    assert set(server._resource_manager._resources) == set(mcp_server._resource_manager._resources)


def test_create_server_can_disable_stateless_http():
    assert create_server(stateless_http=False).settings.stateless_http is False


@pytest.mark.parametrize("port", [0, 65536, -1, True, "8765"])
def test_create_server_rejects_invalid_port(port):
    with pytest.raises(ValueError, match="port"):
        create_server(port=port)


@pytest.mark.parametrize("path", ["mcp", "", 123])
def test_create_server_rejects_invalid_path(path):
    with pytest.raises(ValueError, match="path"):
        create_server(path=path)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.25", "nomad.local"])
@pytest.mark.parametrize("bearer_token", [None, "test-token"])
def test_create_server_rejects_non_loopback_host(host, bearer_token):
    with pytest.raises(ValueError, match="only supports loopback"):
        create_server(host=host, bearer_token=bearer_token)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost", "LOCALHOST."])
def test_is_loopback_host(host):
    assert is_loopback_host(host) is True


def test_server_main_rejects_non_loopback_before_creating_server(monkeypatch):
    created = []
    monkeypatch.setattr(
        "nomad.server.create_server",
        lambda **kwargs: created.append(kwargs),
    )

    with pytest.raises(ValueError, match="only supports loopback"):
        main(
            transport="streamable-http",
            host="0.0.0.0",
            bearer_token="test-token",
        )

    assert created == []


def test_server_main_defaults_to_stdio(monkeypatch):
    calls = {}

    class FakeServer:
        def run(self, *, transport):
            calls["transport"] = transport

    def fake_create_server(**kwargs):
        calls["config"] = kwargs
        return FakeServer()

    monkeypatch.setattr("nomad.server.create_server", fake_create_server)
    monkeypatch.setattr("nomad.server.log_server_startup", lambda *_: None)
    monkeypatch.setattr("nomad.server.atexit.register", lambda *_: None)

    main()

    assert calls == {
        "config": {
            "host": DEFAULT_HOST,
            "port": DEFAULT_PORT,
            "path": DEFAULT_PATH,
            "stateless_http": True,
            "bearer_token": None,
        },
        "transport": "stdio",
    }


def test_server_main_accepts_explicit_transport_and_http_options(monkeypatch):
    calls = {}

    class FakeServer:
        def run(self, *, transport):
            calls["transport"] = transport

    def fake_create_server(**kwargs):
        calls["config"] = kwargs
        return FakeServer()

    monkeypatch.setattr("nomad.server.create_server", fake_create_server)
    monkeypatch.setattr("nomad.server.log_server_startup", lambda *_: None)
    monkeypatch.setattr("nomad.server.atexit.register", lambda *_: None)

    main(
        transport="streamable-http",
        host="localhost",
        port=9999,
        path="/custom",
        bearer_token="secret",
    )

    assert calls == {
        "config": {
            "host": "localhost",
            "port": 9999,
            "path": "/custom",
            "stateless_http": True,
            "bearer_token": "secret",
        },
        "transport": "streamable-http",
    }


def test_server_main_handles_keyboard_interrupt_and_keeps_shutdown_hook(monkeypatch):
    registered = []

    class InterruptedServer:
        def run(self, *, transport):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        "nomad.server.create_server",
        lambda **_: InterruptedServer(),
    )
    monkeypatch.setattr("nomad.server.log_server_startup", lambda *_: None)
    monkeypatch.setattr(
        "nomad.server.atexit.register",
        lambda callback: registered.append(callback),
    )

    assert main(transport="streamable-http") is None
    assert registered == [log_server_shutdown]


def test_health_returns_process_metadata(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    res = json.loads(health())

    assert res["ok"] is True
    assert res["tool"] == "health"
    assert res["data"]["pid"] == os.getpid()
    assert res["data"]["cwd"]
    assert res["data"]["version"]
    assert res["data"]["log_path"] == str(log_path)


def test_safe_tool_catches_exception_and_logs_traceback(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    def boom(target: str = "default") -> str:
        raise RuntimeError("kaboom")

    wrapped = _safe_tool(boom)
    assert inspect.iscoroutinefunction(wrapped)
    res = json.loads(run_async(wrapped, target="gpu"))

    assert res["ok"] is False
    assert res["tool"] == "boom"
    assert res["target"] == "gpu"
    assert res["error_type"] == "internal_error"
    assert str(log_path) in res["diagnostics"][1]
    log_content = log_path.read_text(encoding="utf-8")
    assert "tool entry name=boom" in log_content
    assert "tool exception name=boom" in log_content
    assert "RuntimeError: kaboom" in log_content
    assert "Traceback" in log_content


def test_safe_tool_redacts_sensitive_exception_and_traceback(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))
    secrets = {
        "API_TOKEN": "api-token-value",
        "PASSWORD": "password-value",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value",
        "API_KEY": "api-key-value",
        "AUTH": "auth-value",
        "CREDENTIAL": "credential-value",
    }

    def boom() -> str:
        message = " ".join(f"{key}={value}" for key, value in secrets.items())
        raise RuntimeError(message)

    result = json.loads(run_async(_safe_tool(boom)))

    log_content = log_path.read_text(encoding="utf-8")
    assert all(secret not in log_content for secret in secrets.values())
    assert all(secret not in result["diagnostics"][0] for secret in secrets.values())
    assert "API_TOKEN=[REDACTED]" in log_content
    assert "AWS_SECRET_ACCESS_KEY=[REDACTED]" in log_content


def test_safe_tool_redacts_sensitive_params_from_log(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    def accepts_sensitive(cmd: str, config_json: str, target: str = "default") -> str:
        return json.dumps({"ok": True, "tool": "accepts_sensitive", "target": target})

    wrapped = _safe_tool(accepts_sensitive)
    run_async(
        wrapped,
        "curl -H 'Authorization: Bearer secret-token' https://example.test",
        '{"runtime":{"extra_env":{"API_TOKEN":"supersecret"}}}',
        target="gpu",
    )

    log_content = log_path.read_text(encoding="utf-8")
    assert "secret-token" not in log_content
    assert "supersecret" not in log_content
    assert "Authorization" not in log_content
    assert '"cmd": "<redacted str len=' in log_content
    assert '"config_json": "<redacted str len=' in log_content
    assert '"target": "gpu"' in log_content


def test_safe_resource_catches_exception_and_logs_traceback(tmp_path, monkeypatch):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))

    def broken_resource() -> str:
        raise ValueError("resource broke")

    wrapped = _safe_resource(broken_resource)
    assert inspect.iscoroutinefunction(wrapped)
    res = json.loads(run_async(wrapped))

    assert res["ok"] is False
    assert res["tool"] == "broken_resource"
    assert res["error_type"] == "internal_error"
    log_content = log_path.read_text(encoding="utf-8")
    assert "resource exception name=broken_resource" in log_content
    assert "ValueError: resource broke" in log_content


@pytest.mark.parametrize(
    ("wrapper_factory", "entry_kind"),
    [(_safe_tool, "tool"), (_safe_resource, "resource")],
)
def test_wrapper_cancellation_is_not_structured_as_internal_error(
    tmp_path,
    monkeypatch,
    wrapper_factory,
    entry_kind,
):
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))
    started = threading.Event()
    release = threading.Event()
    cancellation_observed = threading.Event()
    cancelled_before_release = []
    events = []

    def blocking() -> str:
        started.set()
        release.wait(timeout=2)
        events.append("worker completed")
        return "{}"

    wrapped = wrapper_factory(blocking)

    async def scenario():
        async def invoke():
            try:
                await wrapped()
            except anyio.get_cancelled_exc_class():
                events.append("cancellation propagated")
                cancellation_observed.set()
                raise

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(invoke)
            while not started.is_set():
                await anyio.sleep(0.01)

            def release_worker():
                cancelled_before_release.append(cancellation_observed.is_set())
                release.set()

            threading.Timer(0.1, release_worker).start()
            task_group.cancel_scope.cancel()

    try:
        anyio.run(scenario)
    finally:
        release.set()

    assert cancelled_before_release == [False]
    assert events == ["worker completed", "cancellation propagated"]
    log_content = log_path.read_text(encoding="utf-8")
    assert (
        f"{entry_kind} request cancellation observed after worker completion "
        "name=blocking"
    ) in log_content
    assert f"{entry_kind} cancelled name=blocking" not in log_content
    assert f"{entry_kind} exception name=blocking" not in log_content


def test_safe_tool_structures_non_cancellation_base_exception():
    class WorkerExit(BaseException):
        pass

    def exits() -> str:
        raise WorkerExit("worker stopped")

    payload = json.loads(run_async(_safe_tool(exits)))

    assert payload["ok"] is False
    assert payload["error_type"] == "internal_error"
    assert payload["diagnostics"][0] == "WorkerExit: worker stopped"


def test_slow_tool_does_not_block_health():
    started = threading.Event()
    release = threading.Event()
    slow_result = {}

    def slow_tool() -> str:
        started.set()
        release.wait(timeout=2)
        return json.dumps({"ok": True})

    wrapped_slow = _safe_tool(slow_tool)
    wrapped_health = _safe_tool(health)

    async def scenario():
        async def run_slow():
            slow_result["value"] = await wrapped_slow()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_slow)
            with anyio.fail_after(0.5):
                while not started.is_set():
                    await anyio.sleep(0.01)
                health_payload = json.loads(await wrapped_health())
            assert health_payload["ok"] is True
            release.set()

    try:
        anyio.run(scenario)
    finally:
        release.set()

    assert json.loads(slow_result["value"])["ok"] is True


def test_registered_tools_and_resource_use_async_wrappers():
    assert all(
        inspect.iscoroutinefunction(tool.fn)
        for tool in mcp_server._tool_manager._tools.values()
    )
    resource = mcp_server._resource_manager._resources[
        "config://current-project"
    ]
    assert inspect.iscoroutinefunction(resource.fn)
    assert not inspect.iscoroutinefunction(health)
    assert not inspect.iscoroutinefunction(get_current_project_resource)


def test_streamable_http_bearer_authentication(tmp_path, monkeypatch):
    token = "correct-test-token"
    log_path = tmp_path / "nomad-mcp.log"
    monkeypatch.setenv("NOMAD_MCP_LOG_PATH", str(log_path))
    server = create_server(bearer_token=token)
    app = server.streamable_http_app()

    async def initialize_response(client: httpx.AsyncClient) -> httpx.Response:
        return await client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "nomad-test", "version": "1"},
                },
            },
        )

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8765",
            ) as unauthenticated:
                assert (await initialize_response(unauthenticated)).status_code == 401

            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer wrong-test-token"},
            ) as wrong_token:
                assert (await initialize_response(wrong_token)).status_code == 401

            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8765",
                headers={"Authorization": f"Bearer {token}"},
            ) as authenticated:
                async with streamable_http_client(
                    "http://127.0.0.1:8765/mcp",
                    http_client=authenticated,
                ) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        result = await session.call_tool("health")
                        payload = json.loads(result.content[0].text)
                        assert payload["ok"] is True
                        assert payload["tool"] == "health"

    anyio.run(scenario)
    assert token not in log_path.read_text(encoding="utf-8")


def test_resource_unconfigured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    res_str = get_current_project_resource()
    res = json.loads(res_str)

    assert res["mode"] == "unconfigured"
    assert "agent_hints" in res
    assert "config_schema" in res
    assert "minimal_remote_template" in res["config_schema"]


def test_resource_invalid_json_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text("{bad json", encoding="utf-8")

    res_str = get_current_project_resource()
    res = json.loads(res_str)

    assert res["mode"] == "invalid_config"
    assert "agent_hints" in res
    assert "config_schema" in res


def test_resource_configured_redacted(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    cfg = {
        "project_name": "my_app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/my_app",
                "hardware": {"gpu": "A100", "cpu_cores": 16},
                "runtime": {
                    "extra_env": {
                        "API_TOKEN": "super_secret_value",
                        "NORMAL": "visible_value"
                    }
                }
            }
        }
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    res_str = get_current_project_resource()
    res = json.loads(res_str)

    assert res["project_name"] == "my_app"
    assert res["mode"] == "remote"
    assert res["default_target"] == "gpu"
    assert "gpu" in res["targets"]
    
    target_info = res["targets"]["gpu"]
    assert target_info["ssh_host"] == "myhost"
    assert target_info["remote_path"] == "/workspace/my_app"
    assert target_info["hardware"] == {"gpu": "A100", "cpu_cores": 16}
    assert target_info["extra_env_keys"] == ["API_TOKEN", "NORMAL"]

    # Critical security assertion: secret values MUST NOT be exposed in the resource
    assert "super_secret_value" not in res_str
    assert "visible_value" not in res_str
    assert "agent_hints" in res
    assert "sync_pull" in res["agent_hints"]
    assert "task_start" in res["agent_hints"]
    assert "net_diagnose" in res["agent_hints"]
    assert res["config_schema"]["minimal_remote_template"]["project_name"] == "my_app"
    assert "run_remote" in res["config_schema"]["fields"]["targets.<name>.limits.command_timeout_seconds"]
