import json
import subprocess
from pathlib import Path
from nomad.tools.sync import (
    BUILTIN_EXCLUDES,
    RSYNC_DELETE_THRESHOLD,
    convert_gitignore_to_rsync,
    sync_pull,
    sync_push,
)


def _payload(result: str) -> dict:
    return json.loads(result)



def test_convert_gitignore_to_rsync_all_builtin_rules_present():
    required_builtins = [
        ".git/",
        ".DS_Store",
        "__pycache__/",
        "*.pyc",
        "*.pyo",
        ".idea/",
        ".vscode/",
        "node_modules/",
        ".pytest_cache/",
        "*.egg-info/",
        "dist/",
        "build/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".nomad.json",
        ".nomad.json.bak",
        ".nomad.local.json",
        ".venv/",
        "venv/",
    ]

    rules = convert_gitignore_to_rsync("")

    for item in required_builtins:
        assert item in BUILTIN_EXCLUDES
        assert f"- {item}" in rules


def test_convert_gitignore_to_rsync_negation_cannot_override_builtin_excludes():
    content = """
!.nomad.json
!node_modules/
!.git/
!*.pyc
!important.log
"""
    rules = convert_gitignore_to_rsync(content)

    # Builtin negation attempts must be ignored
    assert "+ .nomad.json" not in rules
    assert "+ node_modules/" not in rules
    assert "+ .git/" not in rules
    assert "+ *.pyc" not in rules

    # Non-builtin negation is still supported
    assert "+ important.log" in rules

    # Builtin exclude rules must remain in effect
    assert "- .nomad.json" in rules
    assert "- node_modules/" in rules
    assert "- .git/" in rules
    assert "- *.pyc" in rules


def test_convert_gitignore_to_rsync_ignores_comments_and_empty_lines():
    content = """
# This is a comment
  
  # Another comment
*.log
"""
    rules = convert_gitignore_to_rsync(content)
    assert "- *.log" in rules
    assert not any(r.startswith("#") for r in rules)
    assert not any(r == "" for r in rules)


def test_convert_gitignore_to_rsync_normal_and_negation_rules():
    content = """
*.log
!important.log
"""
    rules = convert_gitignore_to_rsync(content)
    assert "- *.log" in rules
    assert "+ important.log" in rules
    # Check that builtin comes first, followed by plus_idx then minus_idx
    plus_idx = rules.index("+ important.log")
    minus_idx = rules.index("- *.log")
    builtin_idx = rules.index("- .nomad.json")
    assert builtin_idx < plus_idx
    assert plus_idx < minus_idx


def test_convert_gitignore_to_rsync_root_and_dir_rules():
    content = """
/root_only
dir_only/
"""
    rules = convert_gitignore_to_rsync(content)
    assert "- /root_only" in rules
    assert "- dir_only/" in rules


def test_convert_gitignore_to_rsync_extra_excludes():
    content = "*.tmp"
    rules = convert_gitignore_to_rsync(content, extra_excludes=["custom.bak", "/build_out"])
    assert "- *.tmp" in rules
    assert "- custom.bak" in rules
    assert "- /build_out" in rules


def test_sync_push_unconfigured(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    result = sync_push()
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "unconfigured"


def test_sync_push_local_mode(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text('{"project_name": "app", "mode": "local"}', encoding="utf-8")

    result = sync_push()
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "local_mode"


def test_sync_push_invalid_json_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".nomad.json").write_text("{bad json", encoding="utf-8")

    result = sync_push()
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"


def test_sync_push_invalid_local_subpath(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "local_subpath": "nonexistent_dir",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    result = sync_push()
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"


def test_sync_push_ssh_preflight_failed(tmp_path, monkeypatch):
    import subprocess
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode=255, stdout="", stderr="Permission denied (publickey).")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_push("gpu")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "ssh_auth_failed"
    assert payload["next_action"] == {"tool": "net_diagnose", "args": {"target": "gpu"}}


def test_sync_push_success(tmp_path, monkeypatch):
    import subprocess
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    (workspace / ".gitignore").write_text("*.log\n", encoding="utf-8")

    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "auto_create_remote_path": True,
                "sync": {
                    "extra_excludes": ["*.bak"]
                }
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            # preflight
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        elif len(calls) == 2:
            # mkdir -p
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        elif "--dry-run" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        else:
            # rsync
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="sent 100 bytes  received 20 bytes", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_push("gpu")
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["tool"] == "sync_push"
    assert payload["target"] == "gpu"

    dry_run_cmd = calls[2]
    assert dry_run_cmd[0] == "rsync"
    assert "--dry-run" in dry_run_cmd
    assert "--itemize-changes" in dry_run_cmd

    rsync_cmd = calls[3]
    assert rsync_cmd[0] == "rsync"
    assert "-az" in rsync_cmd
    assert "--delete" in rsync_cmd
    assert "--dry-run" not in rsync_cmd
    assert f"myhost:/workspace/app/" in rsync_cmd[-1]
    assert payload["data"]["delete_summary"]["delete_count"] == 0


def test_sync_push_rsync_failed(tmp_path, monkeypatch):
    import subprocess
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "auto_create_remote_path": False,
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            # preflight
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        if "--dry-run" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        else:
            # rsync fail
            return subprocess.CompletedProcess(cmd, returncode=12, stdout="", stderr="rsync connection error")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_push("gpu")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "rsync_failed"
    assert "rsync connection error" in payload["diagnostics"][0]


def test_sync_push_allows_small_delete_dry_run(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "auto_create_remote_path": False,
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        if "--dry-run" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                returncode=0,
                stdout="*deleting old.log\ndeleting cache/tmp.bin\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="sent 10 bytes", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_push("gpu")
    payload = _payload(result)

    assert payload["ok"] is True
    summary = payload["data"]["delete_summary"]
    assert summary["delete_count"] == 2
    assert summary["threshold"] == RSYNC_DELETE_THRESHOLD
    assert summary["deleted_preview"] == ["old.log", "cache/tmp.bin"]
    assert len(calls) == 3
    assert "--dry-run" in calls[1]
    assert "--dry-run" not in calls[2]


def test_sync_push_blocks_delete_over_threshold(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "auto_create_remote_path": False,
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    delete_lines = "\n".join(
        f"*deleting stale/file_{idx}.txt"
        for idx in range(RSYNC_DELETE_THRESHOLD + 1)
    )

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        if "--dry-run" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=delete_lines, stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="should not run", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_push("gpu")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "rsync_delete_threshold_exceeded"
    assert payload["data"]["delete_summary"]["delete_count"] == RSYNC_DELETE_THRESHOLD + 1
    assert payload["data"]["delete_summary"]["preview_truncated"] is True
    assert "Manual confirmation is required" in payload["message"]
    assert len(calls) == 2
    assert "--dry-run" in calls[1]


def test_sync_push_dry_run_failed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "auto_create_remote_path": False,
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        if "--dry-run" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=12, stdout="", stderr="dry-run failed")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="should not run", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_push("gpu")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "rsync_failed"
    assert "dry-run failed" in payload["diagnostics"][0]
    assert len(calls) == 2
    assert "--dry-run" in calls[1]


def test_sync_push_disables_respect_gitignore(tmp_path, monkeypatch):
    import subprocess
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)

    # Local .gitignore contains secret.txt
    (workspace / ".gitignore").write_text("secret.txt\n", encoding="utf-8")

    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
                "auto_create_remote_path": False,
                "sync": {
                    "respect_gitignore": False,
                    "extra_excludes": ["extra.bak"],
                },
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    filter_content_captured = None

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        nonlocal filter_content_captured
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        else:
            # Capture filter file path from rsync_cmd: --filter=merge /path/to/filter
            for arg in cmd:
                if arg.startswith("--filter=merge "):
                    filter_file = Path(arg.split("--filter=merge ")[1])
                    if filter_file.exists():
                        filter_content_captured = filter_file.read_text(encoding="utf-8")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="sent 10 bytes", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_push("gpu")
    payload = _payload(result)

    assert payload["ok"] is True
    assert filter_content_captured is not None

    # secret.txt in .gitignore must NOT be included in filter rules
    assert "- secret.txt" not in filter_content_captured

    # Builtin excludes must still exist
    assert "- .git/" in filter_content_captured
    assert "- .nomad.json" in filter_content_captured

    # extra_excludes must still exist
    assert "- extra.bak" in filter_content_captured


def test_sync_pull_success_default_dest(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        dest = Path(cmd[-1])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "report.json").write_text('{"ok": true}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="received 12 bytes", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_pull("artifacts/report.json", "gpu")
    payload = _payload(result)

    assert payload["ok"] is True
    assert payload["tool"] == "sync_pull"
    assert payload["target"] == "gpu"
    assert payload["data"]["remote_path"] == "/workspace/app/artifacts/report.json"
    assert payload["data"]["local_dest"] == str(workspace / "remote_artifacts" / "gpu")
    assert payload["data"]["saved_path"] == str(workspace / "remote_artifacts" / "gpu" / "report.json")
    assert payload["data"]["bytes"] == len('{"ok": true}')

    rsync_cmd = calls[1]
    assert rsync_cmd[0] == "rsync"
    assert "-az" in rsync_cmd
    assert "--delete" not in rsync_cmd
    assert rsync_cmd[-2] == "myhost:/workspace/app/artifacts/report.json"
    assert rsync_cmd[-1] == f"{workspace / 'remote_artifacts' / 'gpu'}/"


def test_sync_pull_rejects_remote_path_traversal(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    bad_paths = [
        "../secret.txt",
        "/etc/passwd",
        "reports/ok.txt;rm -rf /",
        "reports/\x00secret",
    ]
    for bad_path in bad_paths:
        result = sync_pull(bad_path, "gpu")
        payload = _payload(result)
        assert payload["ok"] is False
        assert payload["error_type"] == "path_traversal"

    assert calls == []


def test_sync_pull_rejects_local_dest_outside_project(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    result = sync_pull("artifacts/report.json", "gpu", str(outside))
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "path_traversal"
    assert calls == []


def test_sync_pull_rejects_local_dest_existing_file_before_preflight(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    file_dest = workspace / "downloads_file"
    file_dest.write_text("not a directory", encoding="utf-8")
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: calls.append(args))

    result = sync_pull("artifacts/report.json", "gpu", str(file_dest))
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "invalid_config"
    assert "not a directory" in payload["message"]
    assert calls == []


def test_sync_pull_rsync_failed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    cfg = {
        "project_name": "app",
        "mode": "remote",
        "default_target": "gpu",
        "targets": {
            "gpu": {
                "ssh_host": "myhost",
                "remote_path": "/workspace/app",
            }
        },
    }
    (workspace / ".nomad.json").write_text(json.dumps(cfg), encoding="utf-8")

    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="ok\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=23, stdout="", stderr="rsync read error")

    monkeypatch.setattr("subprocess.run", fake_run)

    result = sync_pull("artifacts/report.json", "gpu", "downloads")
    payload = _payload(result)

    assert payload["ok"] is False
    assert payload["error_type"] == "rsync_failed"
    assert "rsync read error" in payload["diagnostics"][0]
    assert calls[1][-2] == "myhost:/workspace/app/artifacts/report.json"
    assert calls[1][-1] == f"{workspace / 'downloads'}/"
