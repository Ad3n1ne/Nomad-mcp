import json
import subprocess

import pytest


@pytest.fixture
def temp_workdir(tmp_path, monkeypatch):
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    return workdir


@pytest.fixture
def write_nomad_config(temp_workdir):
    def _write(config=None, filename=".nomad.json"):
        data = config or {"mode": "local", "project_name": "test_project"}
        path = temp_workdir / filename
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def mock_subprocess_run(monkeypatch):
    calls = []

    def _install(returncode=0, stdout="", stderr="", side_effect=None):
        def fake_run(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            if side_effect is not None:
                if callable(side_effect):
                    return side_effect(*args, **kwargs)
                raise side_effect
            completed_args = args[0] if args else kwargs.get("args")
            return subprocess.CompletedProcess(
                completed_args,
                returncode,
                stdout=stdout,
                stderr=stderr,
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    return _install
