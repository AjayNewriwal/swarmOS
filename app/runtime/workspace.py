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
import signal
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

# Never copy these into a run workspace when mirroring a real repository.
_SENSITIVE = re.compile(
    r"^(\.env($|\..*)|.*\.(pem|key|p12|pfx|jks|db|sqlite3?)|id_rsa.*|credentials\.(json|ya?ml)|secrets\.(json|ya?ml))$",
    re.IGNORECASE,
)

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
    def __init__(
        self,
        root: Path,
        *,
        char_limit: int = 400_000,
        execution_enabled: bool = True,
        allowed_commands: set[str] | None = None,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.char_limit = char_limit
        self.execution_enabled = execution_enabled
        self.allowed_commands = allowed_commands or {"python", "python3", "py", "pytest"}
        self.last_truncated = False

    # -- creation ---------------------------------------------------------
    @classmethod
    def create_temp(cls, prefix: str) -> Workspace:
        base = Path(tempfile.mkdtemp(prefix=prefix))
        return cls(base / "workspace")

    @classmethod
    def from_directory(cls, source: Path, destination: Path) -> Workspace:
        """Mirror a directory (used for the bundled sample project)."""
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            for path in source.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                relative = path.relative_to(source)
                if any(part in _SKIP_DIRS for part in relative.parts):
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
        return cls(destination)

    @classmethod
    def from_repository(
        cls, source: Path, destination: Path, *, allow: bool = False
    ) -> Workspace:
        """Mirror a real repository checkout.

        Disabled by default. This is a copy for the orchestrator to edit, not a
        live checkout: there is no git history, no branch, and no push. Secrets
        and databases are skipped, and symlinks are not followed.
        """
        if not allow:
            raise WorkspaceError(
                "repository checkout is disabled. Set SWARMOS_ALLOW_REPO_CHECKOUT=1 "
                "to copy a repository into a run workspace. There is no tested "
                "live-repository workflow yet."
            )
        if not source.is_dir():
            raise WorkspaceError(f"not a directory: {source}")
        destination.mkdir(parents=True, exist_ok=True)
        for path in source.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source)
            if any(part in _SKIP_DIRS for part in relative.parts):
                continue
            if _SENSITIVE.match(relative.name):
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

    def snapshot(self, limit_bytes: int | None = None) -> dict[str, str]:
        limit = self.char_limit if limit_bytes is None else limit_bytes
        files: dict[str, str] = {}
        total = 0
        self.last_truncated = False
        for path in sorted(self.root.rglob("*")):
            # Never read through a symlink: it can point outside the workspace.
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(self.root)
            if any(part in _SKIP_DIRS for part in relative.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            total += len(content)
            if total > limit:
                self.last_truncated = True
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
        return Workspace(
            destination,
            char_limit=self.char_limit,
            execution_enabled=self.execution_enabled,
            allowed_commands=set(self.allowed_commands),
        )

    def destroy(self) -> None:
        shutil.rmtree(self.root.parent, ignore_errors=True)

    # -- verification -----------------------------------------------------
    def run_command(self, command: str, timeout_seconds: int) -> VerificationResult:
        argv = shlex.split(command, posix=False)
        if not argv:
            return self._execution_failure(command, "verification command is empty")
        if not self.execution_enabled:
            return self._execution_failure(
                command,
                "execution is disabled (SWARMOS_EXECUTION_ENABLED=0)",
            )
        if argv[0].lower() in {"python", "python3", "py"}:
            argv[0] = sys.executable
        binary = Path(argv[0]).name.lower()
        if binary.endswith(".exe"):
            binary = binary[: -len(".exe")]
        if binary not in self.allowed_commands:
            return self._execution_failure(
                command,
                f"command {binary!r} is not in SWARMOS_ALLOWED_COMMANDS "
                f"({', '.join(sorted(self.allowed_commands))})",
            )

        env = self._safe_env()
        started = time.perf_counter()
        timed_out = False
        try:
            process = subprocess.Popen(  # noqa: S603 - explicit argv, allow-listed binary
                argv,
                cwd=str(self.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except OSError as exc:
            return self._execution_failure(
                command, f"could not start verification command: {exc}"
            )

        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            # Kill the whole tree, not just the direct child, so a spawned
            # pytest worker cannot keep running after the timeout.
            self._kill_tree(process)
            stdout, stderr = process.communicate()
            exit_code = 124
            timed_out = True
            stderr = (stderr or "") + f"\nverification timed out after {timeout_seconds}s"

        duration_ms = int((time.perf_counter() - started) * 1000)
        passed_count = _count(_PASSED, stdout)
        failed_count = _count(_FAILED, stdout)
        error_count = _count(_ERRORS, stdout)
        passed = (
            not timed_out
            and exit_code == 0
            and passed_count > 0
            and failed_count == 0
        )

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

    def _execution_failure(self, command: str, message: str) -> VerificationResult:
        return VerificationResult(
            passed=False,
            command=command,
            exit_code=126,
            duration_ms=0,
            stdout_tail="",
            stderr_tail=message,
            workspace_files=self.file_list(),
        )

    @staticmethod
    def _kill_tree(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(  # noqa: S603,S607 - fixed system binary and integer pid
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass

    def _safe_env(self) -> dict[str, str]:
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        env["PYTHONPATH"] = str(self.root.resolve())
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["NO_COLOR"] = "1"
        env["PYTHONHASHSEED"] = "0"
        return env


def _count(pattern: re.Pattern[str], text: str) -> int:
    total = 0
    for match in pattern.finditer(text):
        total += int(match.group("n"))
    return total


def _tail(text: str, limit: int = 4000) -> str:
    cleaned = (text or "").strip()
    return cleaned[-limit:]