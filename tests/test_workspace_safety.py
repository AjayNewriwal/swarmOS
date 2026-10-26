from __future__ import annotations

import os
import sys

import pytest

from app.runtime.workspace import Workspace, WorkspaceError


def make_workspace(tmp_path, **kwargs) -> Workspace:
    return Workspace(tmp_path / "workspace", **kwargs)


def test_execution_can_be_disabled(tmp_path):
    workspace = make_workspace(tmp_path, execution_enabled=False)
    workspace.seed({"tests/test_ok.py": "def test_ok():\n    assert True\n"})

    result = workspace.run_command("python -m pytest -q", timeout_seconds=30)

    assert result.passed is False
    assert result.exit_code == 126
    assert "disabled" in result.stderr_tail


def test_disallowed_binary_is_refused_without_running(tmp_path):
    workspace = make_workspace(tmp_path, allowed_commands={"python"})
    result = workspace.run_command("curl --version", timeout_seconds=30)

    assert result.passed is False
    assert result.exit_code == 126
    assert "not in SWARMOS_ALLOWED_COMMANDS" in result.stderr_tail


def test_allowed_python_still_runs(tmp_path):
    workspace = make_workspace(tmp_path)
    workspace.seed({"tests/test_ok.py": "def test_ok():\n    assert 1 == 1\n"})
    result = workspace.run_command("python -m pytest -q", timeout_seconds=60)
    assert result.passed is True


def test_allowlist_accepts_python_exe_alias(tmp_path):
    workspace = make_workspace(tmp_path, allowed_commands={"python"})
    workspace.write("check.py", "print('ok')\n")
    result = workspace.run_command(f"{sys.executable} check.py", timeout_seconds=30)
    # The resolved executable name is python.exe, which normalises to python.
    assert result.exit_code == 0
    assert "ok" in result.stdout_tail


def test_snapshot_skips_symlinks(tmp_path):
    workspace = make_workspace(tmp_path)
    workspace.seed({"inside.txt": "safe"})
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace.root / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available in this environment")

    snapshot = workspace.snapshot()
    assert "inside.txt" in snapshot
    assert "link.txt" not in snapshot
    assert "secret" not in " ".join(snapshot.values())


def test_snapshot_truncation_is_explicit(tmp_path):
    workspace = make_workspace(tmp_path, char_limit=50)
    workspace.seed({"a.txt": "x" * 40, "b.txt": "y" * 40})
    snapshot = workspace.snapshot()
    assert workspace.last_truncated is True
    assert sum(len(value) for value in snapshot.values()) <= 50


def test_repo_checkout_requires_explicit_opt_in(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "main.py").write_text("print('hi')\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="disabled"):
        Workspace.from_repository(source, tmp_path / "copy")


def test_repo_checkout_skips_secrets_and_databases(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    (source / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (source / ".env").write_text("OPENAI_COMPAT_API_KEY=do-not-copy\n", encoding="utf-8")
    (source / "swarmos.db").write_text("binary", encoding="utf-8")
    (source / "server.pem").write_text("key", encoding="utf-8")
    (source / "credentials.json").write_text("{}", encoding="utf-8")

    workspace = Workspace.from_repository(
        source, tmp_path / "copy", allow=True
    )

    assert "main.py" in workspace.file_list()
    copied = set(workspace.file_list())
    for forbidden in {".env", "swarmos.db", "server.pem", "credentials.json"}:
        assert forbidden not in copied


def test_repo_checkout_rejects_non_directory(tmp_path):
    (tmp_path / "not-a-dir.txt").write_text("x", encoding="utf-8")
    with pytest.raises(WorkspaceError):
        Workspace.from_repository(
            tmp_path / "not-a-dir.txt", tmp_path / "copy", allow=True
        )


def test_workspace_root_is_contained(tmp_path):
    workspace = make_workspace(tmp_path)
    with pytest.raises(WorkspaceError):
        workspace.write("../escape.txt", "x")

    # The parent of the workspace still exists; nothing escaped.
    assert not (tmp_path / "escape.txt").exists()
    assert os.path.isdir(workspace.root)