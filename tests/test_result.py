import json

from nomad.result import failure_result, success_result


def test_success_result_serializes_contract():
    payload = json.loads(
        success_result(
            tool="run_remote",
            target="gpu",
            message="Command completed.",
            data={"exit_code": 0},
            diagnostics=["stdout truncated"],
        )
    )

    assert payload == {
        "ok": True,
        "tool": "run_remote",
        "target": "gpu",
        "message": "Command completed.",
        "data": {"exit_code": 0},
        "diagnostics": ["stdout truncated"],
        "next_action": None,
    }
    assert "error_type" not in payload


def test_success_result_defaults_empty_data_and_diagnostics():
    payload = json.loads(success_result(tool="init_discover", message="Ready."))

    assert payload["data"] == {}
    assert payload["diagnostics"] == []
    assert payload["target"] is None
    assert payload["next_action"] is None


def test_failure_result_serializes_contract():
    payload = json.loads(
        failure_result(
            tool="sync_push",
            target="gpu",
            error_type="ssh_timeout",
            message="SSH preflight failed before rsync.",
            recoverable=True,
            details={"timeout_seconds": 3},
        )
    )

    assert payload == {
        "ok": False,
        "tool": "sync_push",
        "target": "gpu",
        "error_type": "ssh_timeout",
        "message": "SSH preflight failed before rsync.",
        "details": {"timeout_seconds": 3},
        "recoverable": True,
        "data": {},
        "diagnostics": [],
        "next_action": None,
    }


def test_failure_result_supports_next_action():
    payload = json.loads(
        failure_result(
            tool="run_remote",
            target="gpu",
            error_type="tunnel_not_running",
            message="Persistent tunnel is not running.",
            recoverable=True,
            next_action={"tool": "tunnel_start", "args": {"target": "gpu"}},
        )
    )

    assert payload["next_action"] == {
        "tool": "tunnel_start",
        "args": {"target": "gpu"},
    }


def test_result_preserves_unicode_message():
    payload = json.loads(
        failure_result(
            tool="run_remote",
            error_type="local_mode",
            message="Remote command failed: café",
            recoverable=False,
        )
    )

    assert payload["message"] == "Remote command failed: café"


def test_diagnostics_string_is_single_entry():
    payload = json.loads(
        success_result(
            tool="run_remote",
            message="Command completed.",
            diagnostics="stdout truncated",
        )
    )

    assert payload["diagnostics"] == ["stdout truncated"]
