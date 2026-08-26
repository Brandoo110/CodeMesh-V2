"""Fixed-argv validation boundary for bounded remediation.

Checks are registered before a remediation run.  The run accepts only a check
ID; it never accepts a command, shell fragment, network target, or caller
provided working directory.  Execution is deliberately marked as a trusted
host process for development use, not as an OS sandbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from assurance.remediation_workspace import (
    CONTROLLER_PRIVATE_DIR,
    IsolatedWorkspace,
    PublicWorkspaceView,
    WorkspaceViolation,
)


class ValidationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    INFRA_ERROR = "infra_error"
    UNSUPPORTED = "unsupported"


class ValidationCheck(BaseModel):
    """One pre-registered fixed argv and its visibility/budget metadata."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        populate_by_name=True,
    )

    check_id: str = Field(alias="id", min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    visibility: Literal["agent", "controller"]
    timeout_seconds: float = Field(default=5.0, alias="timeout_s", gt=0, le=60)
    output_limit: int = Field(default=64 * 1024, strict=True, gt=0)

    @field_validator("check_id")
    @classmethod
    def _non_blank_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("check_id must not be blank")
        return value

    @field_validator("argv", mode="before")
    @classmethod
    def _fixed_argv(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (tuple, list)) or not value:
            raise ValueError("argv must be a non-empty tuple or list")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError("argv entries must be non-empty strings")
            if "\x00" in item:
                raise ValueError("argv entries must not contain NUL")
            result.append(item)
        executable = Path(result[0]).name.casefold()
        if executable in {
            "sh",
            "bash",
            "zsh",
            "fish",
            "cmd",
            "cmd.exe",
            "powershell",
            "pwsh",
            "curl",
            "wget",
            "nc",
            "netcat",
            "ssh",
        }:
            raise ValueError("shell or network executables are not valid validation argv")
        return tuple(result)

    @property
    def id(self) -> str:
        """V1-compatible read-only spelling for the registered check ID."""

        return self.check_id


@dataclass(frozen=True)
class ValidationResult:
    check_id: str
    status: ValidationStatus
    reason_code: str
    exit_code: int | None
    duration_ms: int
    stdout_tail: str
    stderr_tail: str
    truncated: bool
    failure_fingerprint: str
    execution_boundary: Literal["trusted_host_process"] = "trusted_host_process"
    intended_use: Literal["development_only"] = "development_only"
    os_sandboxed: bool = False

    def to_json(self) -> str:
        payload = asdict(self)
        payload["status"] = self.status.value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class _ProcessResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int


async def _run_fixed_argv(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    output_limit: int,
) -> _ProcessResult:
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return _ProcessResult(
            exit_code=None,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            timed_out=False,
            truncated=False,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    total_bytes = 0
    truncated = False

    def kill_group() -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass

    async def drain(
        stream: asyncio.StreamReader | None,
        target: bytearray,
    ) -> None:
        nonlocal total_bytes, truncated
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            remaining = max(0, output_limit - total_bytes)
            if remaining:
                target.extend(chunk[:remaining])
            total_bytes += len(chunk)
            if total_bytes > output_limit:
                truncated = True
                kill_group()
                return

    stdout_task = asyncio.create_task(drain(process.stdout, stdout_buf))
    stderr_task = asyncio.create_task(drain(process.stderr, stderr_buf))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=max(0.1, timeout_s))
    except asyncio.TimeoutError:
        timed_out = True
        kill_group()
        await process.wait()
    except asyncio.CancelledError:
        kill_group()
        await process.wait()
        raise
    finally:
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    return _ProcessResult(
        exit_code=process.returncode,
        stdout=stdout_buf.decode("utf-8", errors="replace"),
        stderr=stderr_buf.decode("utf-8", errors="replace"),
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _safe_child_env(workspace: IsolatedWorkspace) -> dict[str, str]:
    home = workspace.resolve(f"{CONTROLLER_PRIVATE_DIR}/home")
    temp = workspace.resolve(f"{CONTROLLER_PRIVATE_DIR}/tmp")
    home.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(temp),
        "PYTHONPATH": str(workspace.root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }


class ValidationExecutor:
    """Execute only a registered check ID in the temporary workspace."""

    def __init__(
        self,
        *,
        workspace: IsolatedWorkspace,
        checks: tuple[ValidationCheck, ...] | list[ValidationCheck],
    ) -> None:
        self.workspace = workspace
        self.checks = {check.check_id: check for check in checks}
        if len(self.checks) != len(checks):
            raise ValueError("validation check IDs must be unique")

    def _result(
        self,
        *,
        check_id: str,
        status: ValidationStatus,
        reason_code: str,
        process: _ProcessResult | None = None,
        stderr: str = "",
    ) -> ValidationResult:
        stdout = process.stdout if process is not None else ""
        if process is not None:
            stderr = process.stderr
        fingerprint = hashlib.sha256(
            f"{status.value}\0{reason_code}\0{stdout[-4096:]}\0{stderr[-4096:]}".encode()
        ).hexdigest()[:16]
        return ValidationResult(
            check_id=check_id,
            status=status,
            reason_code=reason_code,
            exit_code=process.exit_code if process is not None else None,
            duration_ms=process.duration_ms if process is not None else 0,
            stdout_tail=stdout[-8192:],
            stderr_tail=stderr[-8192:],
            truncated=process.truncated if process is not None else False,
            failure_fingerprint=fingerprint,
        )

    def _validate_argv_paths(self, check: ValidationCheck) -> None:
        """Reject absolute/path-like argv entries outside this workspace.

        The executable itself is pre-registered and may be absolute.  Any
        other absolute path must resolve inside the temporary workspace; a
        relative path is resolved by the fixed workspace cwd.
        """

        for argument in check.argv:
            if "://" in argument:
                raise WorkspaceViolation("URI arguments are not allowed")
        for index, argument in enumerate(check.argv[1:], start=1):
            if (
                len(argument) >= 3
                and argument[1] == ":"
                and argument[2] in "/\\"
            ):
                raise WorkspaceViolation("drive paths are not allowed in validation argv")
            if not (
                Path(argument).is_absolute()
                or (
                    len(argument) >= 3
                    and argument[1] == ":"
                    and argument[2] in "/\\"
                )
                or "/" in argument
                or "\\" in argument
                or ".." in Path(argument.replace("\\", "/")).parts
            ):
                continue
            normalized = argument.replace("\\", "/")
            resolved = (
                Path(normalized).resolve()
                if Path(normalized).is_absolute()
                else (self.workspace.root / normalized).resolve()
            )
            if not resolved.is_relative_to(self.workspace.root):
                raise WorkspaceViolation("validation argv path escapes workspace")

    async def validate(
        self,
        check_id: str,
        *,
        actor: Literal["agent", "controller"],
    ) -> ValidationResult:
        check = self.checks.get(check_id)
        if check is None:
            return self._result(
                check_id=check_id,
                status=ValidationStatus.BLOCKED,
                reason_code="unknown_check",
            )
        if check.visibility == "controller" and actor != "controller":
            return self._result(
                check_id=check_id,
                status=ValidationStatus.BLOCKED,
                reason_code="check_not_visible",
            )
        try:
            self._validate_argv_paths(check)
            process = await _run_fixed_argv(
                check.argv,
                cwd=self.workspace.root,
                env=_safe_child_env(self.workspace),
                timeout_s=check.timeout_seconds,
                output_limit=check.output_limit,
            )
        except (OSError, WorkspaceViolation) as exc:
            return self._result(
                check_id=check_id,
                status=ValidationStatus.INFRA_ERROR,
                reason_code="invalid_validation_fixture",
                stderr=f"{type(exc).__name__}: {exc}",
            )
        if process.timed_out:
            return self._result(
                check_id=check_id,
                status=ValidationStatus.TIMEOUT,
                reason_code="validation_timeout",
                process=process,
            )
        if process.truncated:
            return self._result(
                check_id=check_id,
                status=ValidationStatus.FAILED,
                reason_code="output_limit_exceeded",
                process=process,
            )
        if process.exit_code != 0:
            return self._result(
                check_id=check_id,
                status=ValidationStatus.FAILED,
                reason_code="validation_failed",
                process=process,
            )
        return self._result(
            check_id=check_id,
            status=ValidationStatus.PASSED,
            reason_code="validation_passed",
            process=process,
        )


class BudgetedValidationExecutor:
    """Per-attempt validation facade exposed to the agent."""

    def __init__(self, executor: object, max_calls: int) -> None:
        if max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.executor = executor
        self.max_calls = max_calls
        self.calls = 0

    async def validate(
        self,
        check_id: str,
        *,
        actor: Literal["agent", "controller"] = "agent",
    ) -> ValidationResult:
        if self.calls >= self.max_calls:
            return ValidationResult(
                check_id=check_id,
                status=ValidationStatus.BLOCKED,
                reason_code="validation_budget_exhausted",
                exit_code=None,
                duration_ms=0,
                stdout_tail="",
                stderr_tail="",
                truncated=False,
                failure_fingerprint="validation-budget-exhausted",
            )
        self.calls += 1
        result = self.executor.validate(check_id, actor=actor)
        if asyncio.iscoroutine(result):
            result = await result
        return result


class ScopedValidationTools:
    """Small explicit tool surface; no shell, delete, or network operation."""

    names = (
        "read_file",
        "write_file",
        "edit_file",
        "list_files",
        "run_validation",
    )

    __slots__ = ("_workspace", "_executor")

    def __init__(
        self,
        workspace: PublicWorkspaceView | IsolatedWorkspace,
        executor: BudgetedValidationExecutor,
    ):
        self._workspace = (
            workspace.public_view()
            if isinstance(workspace, IsolatedWorkspace)
            else workspace
        )
        self._executor = executor

    def read_file(self, path: str) -> str:
        return self._workspace.read_text(path)

    def write_file(self, path: str, content: str) -> str:
        self._workspace.write_text(path, content)
        return f"OK: wrote {len(content.encode('utf-8'))} bytes"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        original = self._workspace.read_text(path)
        if original.count(old_string) != 1:
            raise WorkspaceViolation("old_string must occur exactly once")
        self._workspace.write_text(path, original.replace(old_string, new_string, 1))
        return "OK: edited"

    def list_files(self) -> tuple[str, ...]:
        return self._workspace.public_paths()

    async def run_validation(self, check_id: str) -> str:
        result = await self._executor.validate(check_id, actor="agent")
        return result.to_json()


def make_validation_tool_registry(
    workspace: PublicWorkspaceView | IsolatedWorkspace,
    executor: BudgetedValidationExecutor,
) -> ScopedValidationTools:
    return ScopedValidationTools(workspace, executor)
