from __future__ import annotations

import os

import pytest

from app.runtime.workspace import Workspace, WorkspaceError


def make_workspace(tmp_path, files: dict[str, str]) -> Workspace:
    workspace = Workspace(tmp_path / "workspace")
    workspace.seed(files)
    return workspace


def test_workspace_rejects_path_traversal(tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    with pytest.raises(WorkspaceError):
        workspace.write("../escape.txt", "nope")
    with pytest.raises(WorkspaceError):
        workspace.read("....//secret.txt")


def test_snapshot_excludes_generated_directories(tmp_path):
    workspace = make_workspace(
        tmp_path,
        {
            "a.py": "x = 1\n",
            "__pycache__/a.cpython-313.pyc": "binary-ish",
            ".pytest_cache/v/cache": "x",
        },
    )
    assert sorted(workspace.snapshot()) == ["a.py"]


def test_verification_passes_on_a_real_pytest_run(tmp_path):
    workspace = make_workspace(
        tmp_path, {"tests/test_ok.py": "def test_ok():\n    assert 1 + 1 == 2\n"}
    )
    result = workspace.run_command("python -m pytest -q", timeout_seconds=60)

    assert result.passed is True
    assert result.exit_code == 0
    assert result.tests_passed == 1
    assert result.tests_failed == 0
    assert "passed" in result.stdout_tail


def test_verification_reports_failures(tmp_path):
    workspace = make_workspace(
        tmp_path, {"tests/test_bad.py": "def test_bad():\n    assert 1 == 2\n"}
    )
    result = workspace.run_command("python -m pytest -q", timeout_seconds=60)

    assert result.passed is False
    assert result.exit_code == 1
    assert result.tests_failed == 1
    assert result.stdout_tail


def test_empty_test_suite_is_not_a_pass(tmp_path):
    workspace = make_workspace(tmp_path, {"module.py": "value = 1\n"})
    result = workspace.run_command("python -m pytest -q", timeout_seconds=60)

    assert result.passed is False
    assert result.tests_passed == 0


def test_verification_timeout_is_reported(tmp_path):
    workspace = make_workspace(
        tmp_path,
        {
            "tests/test_slow.py": (
                "import time\n\n"
                "def test_slow():\n"
                "    time.sleep(5)\n"
            )
        },
    )
    result = workspace.run_command("python -m pytest -q", timeout_seconds=1)

    assert result.passed is False
    assert result.exit_code == 124
    assert "timed out" in result.stderr_tail


def test_provider_secrets_do_not_reach_verified_code(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMOS_TEST_SECRET", "do-not-leak")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-secret")
    workspace = make_workspace(
        tmp_path,
        {
            "tests/test_env.py": (
                "import os\n\n"
                "def test_no_secret():\n"
                "    assert os.getenv('SWARMOS_TEST_SECRET') is None\n"
                "    assert os.getenv('OPENAI_COMPAT_API_KEY') is None\n"
            )
        },
    )
    result = workspace.run_command("python -m pytest -q", timeout_seconds=60)

    assert result.passed is True, result.stdout_tail + result.stderr_tail
    assert "sk-secret" not in result.stdout_tail


def test_seed_files_cannot_escape_the_workspace(tmp_path):
    from pydantic import ValidationError

    from app.models import FileChange

    with pytest.raises(ValidationError):
        FileChange(path="../../outside.txt", content="x")


def test_clone_creates_an_isolated_copy(tmp_path):
    workspace = make_workspace(tmp_path, {"a.txt": "original"})
    clone = workspace.clone(tmp_path / "clone")

    clone.write("a.txt", "changed")
    assert workspace.read("a.txt") == "original"
    assert clone.read("a.txt") == "changed"
    assert os.path.exists(workspace.root / "a.txt")