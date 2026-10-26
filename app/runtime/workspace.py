"""Isolated workspaces for coding agents and the verification runner.

A run gets a temporary directory. Each coding subtask checks out its own copy,
which keeps parallel branches from writing over each other. The canonical
workspace only changes when the scheduler merges a validated branch.

This is a *containment* boundary (paths, environment, timeouts), not a security
sandbox. It does not provide kernel isolation or network blocking.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from app.models import FileChange, VerificationResult, safe_relative_path

_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
}

# Only these variables reach code executed by the verification runner, so model
# credentials from the parent process cannot leak into test code.
_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
)

_PASSED = re.compile(r"(?P<n>\d+) passed")
_FAILED = re.compile(r"(?P<n>\d+) failed")
_ERRORS = re.compile(r"(?P<n>\d+) error")


class WorkspaceError(RuntimeError):
    pass


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # -- creation ---------------------------------------------------------
    @classmethod
    def create_temp(cls, prefix: str) -> Workspace:
        base = Path(tempfile.mkdtemp(prefix=prefix))
        return cls(base / "workspace")

    @classmethod
    def from_directory(cls, source: Path, destination: Path) -> Workspace:
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            for path in source.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(source)
                if any(part in _SKIP_DIRS for part in relative.parts):
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        return cls(destination)

    # -- paths ------------------------------------------------------------
    def _resolve(self, relative: str) -> Path:
        try:
            safe = safe_relative_path(relative)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc
        candidate = (self.root / safe).resolve()
        root = self.root.resolve()
        if candidate != root and root not in candidate.parents:
            raise WorkspaceError(f"path escapes the workspace: {relative!r}")
        return candidate

    # -- file operations --------------------------------------------------
    def seed(self, files: dict[str, str]) -> None:
        for path, content in files.items():
            self.write(path, content)

    def write(self, relative: str, content: str) -> None:
        target = self._resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def read(self, relative: str) -> str:
        return self._resolve(relative).read_text(encoding="utf-8")

    def delete(self, relative: str) -> None:
        target = self._resolve(relative)
        if target.is_file():
            target.unlink()

    def exists(self, relative: str) -> bool:
        return self._resolve(relative).is_file()

    def apply(self, changes: list[FileChange]) -> list[FileChange]:
        for change in changes:
            if change.action == "delete":
                self.delete(change.path)
            else:
                self.write(change.path, change.content)
        return changes

    def snapshot(self, limit_bytes: int = 400_000) -> dict[str, str]:
        files: dict[str, str] = {}
        total = 0
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if any(part in _SKIP_DIRS for part in relative.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            total += len(content)
            if total > limit_bytes:
                break
            files[relative.as_posix()] = content
        return files

    def file_list(self) -> list[str]:
        return sorted(self.snapshot().keys())

    def clone(self, destination: Path) -> Workspace:
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for relative, content in self.snapshot().items():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        return Workspace(destination)

    def destroy(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)

    # -- verification -----------------------------------------------------
    def run_command(self, command: str, timeout_seconds: int) -> VerificationResult:
        argv = shlex.split(command, posix=False)
        if not argv:
            raise WorkspaceError("verification command is empty")
        if argv[0].lower() in {"python", "python3", "py"}:
            argv[0] = sys.executable
        env = self._safe_env()
        started = time.perf_counter()
        try:
            completed = subprocess.run(  # noqa: S603 - explicit, allow-listed env
                argv,
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            stdout, stderr = completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            exit_code = 124
            stdout = _decode(exc.stdout)
            stderr = f"verification timed out after {timeout_seconds}s"
        except OSError as exc:
            exit_code = 127
            stdout = ""
            stderr = f"could not start verification command: {exc}"
        duration_ms = int((time.perf_counter() - started) * 1000)

        passed_count = _count(_PASSED, stdout)
        failed_count = _count(_FAILED, stdout)
        error_count = _count(_ERRORS, stdout)
        passed = exit_code == 0 and passed_count > 0 and failed_count == 0

        return VerificationResult(
            passed=passed,
            command=" ".join(argv),
            exit_code=exit_code,
            duration_ms=duration_ms,
            tests_passed=passed_count,
            tests_failed=failed_count + error_count,
            stdout_tail=_tail(stdout),
            stderr_tail=_tail(stderr),
            workspace_files=self.file_list(),
        )

    def _safe_env(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        env["PYTHONPATH"] = str(self.root.resolve())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env["PYTHONHASHSEED"] = "0"
        return env


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _count(pattern: re.Pattern[str], text: str) -> int:
    total = 0
    for match in pattern.finditer(text):
        total += int(match.group("n"))
    return total


def _tail(text: str, limit: int = 4000) -> str:
    cleaned = (text or "").strip()
    return cleaned[-limit:]