"""确定性命令收集器（V2-P2-03）。

本模块只做冻结 argv 的确定性命令收集；它不是 OS/网络沙箱，不声称阻止
被允许的可执行文件访问主机文件或网络。
"""

import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .artifacts import ArtifactStore
from .contracts import Evidence
from .digests import normalize_repo_path


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMAND_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_GLOBAL_MAX_OUTPUT_BYTES = 1024 * 1024
_MAX_COMMANDS = 16
_MAX_ARGV_ITEMS = 32
_MAX_ARGV_BYTES = 4096
_READ_CHUNK_BYTES = 65536
_EVENT_POLL_SECONDS = 0.05
_TERMINATION_WAIT_SECONDS = 2.0
_JOIN_TIMEOUT_SECONDS = 2.0

_JSON_KWARGS = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": False,
    "allow_nan": False,
}


class CommandCollectionError(Exception):
    """命令收集失败的基类异常。"""


class CommandSpecError(CommandCollectionError):
    """命令、工作区或 cwd 规格违反安全边界。"""


class CommandLaunchError(CommandCollectionError):
    """命令进程启动失败。"""


class CommandExecutionError(CommandCollectionError):
    """管道、读取、终止或回收等内部执行失败。"""


class _CommandGrammar(BaseModel):
    """CommandSpec 与 CommandObservation 共用的命令字段语法。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    command_id: str
    kind: Literal["test", "build", "lint", "static"]
    argv: tuple[str, ...]
    cwd: str

    @field_validator("command_id", mode="before")
    @classmethod
    def _validate_command_id(cls, value: object) -> str:
        if not isinstance(value, str) or _COMMAND_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "command_id must match ^[a-z][a-z0-9_.-]{0,63}$"
            )
        return value

    @field_validator("argv", mode="before")
    @classmethod
    def _validate_argv(cls, value: object) -> tuple[str, ...]:
        if not isinstance(value, (tuple, list)):
            raise ValueError("argv must be a tuple or list")
        items: list[str] = []
        total_bytes = 0
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"argv item {index} must be a str")
            if not item.strip():
                raise ValueError(
                    "argv items must not be empty or whitespace-only"
                )
            if "\x00" in item:
                raise ValueError("argv items must not contain NUL")
            total_bytes += len(item.encode("utf-8"))
            items.append(item)
        if not items:
            raise ValueError("argv must contain at least one item")
        if len(items) > _MAX_ARGV_ITEMS:
            raise ValueError(
                f"argv must contain at most {_MAX_ARGV_ITEMS} items"
            )
        if total_bytes > _MAX_ARGV_BYTES:
            raise ValueError(
                f"combined argv UTF-8 bytes must not exceed {_MAX_ARGV_BYTES}"
            )
        return tuple(items)

    @field_validator("cwd", mode="before")
    @classmethod
    def _validate_cwd(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("cwd must be a str")
        if value == ".":
            return value
        if normalize_repo_path(value) != value:
            raise ValueError(
                "cwd must be '.' or an exact canonical repo-relative path"
            )
        return value


class CommandSpec(_CommandGrammar):
    """一条确定性命令的不可变规格。"""

    timeout_seconds: float = Field(
        strict=True, gt=0, le=300, allow_inf_nan=False
    )
    max_output_bytes: int = Field(
        strict=True, ge=1, le=_GLOBAL_MAX_OUTPUT_BYTES
    )


class CommandObservation(_CommandGrammar):
    """一条已收集命令的不可变观察。"""

    outcome: Literal["success", "failure", "timeout", "output_limit"]
    exit_code: int | None = Field(default=None, strict=True)
    duration_ms: int = Field(strict=True, ge=0)
    stdout_artifact_digest: str
    stderr_artifact_digest: str
    stdout_bytes: int = Field(
        strict=True, ge=0, le=_GLOBAL_MAX_OUTPUT_BYTES
    )
    stderr_bytes: int = Field(
        strict=True, ge=0, le=_GLOBAL_MAX_OUTPUT_BYTES
    )
    stdout_truncated: bool = Field(strict=True)
    stderr_truncated: bool = Field(strict=True)

    @field_validator(
        "stdout_artifact_digest",
        "stderr_artifact_digest",
        mode="before",
    )
    @classmethod
    def _validate_artifact_digest(cls, value: object) -> str:
        if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(
            value
        ) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "CommandObservation":
        if self.outcome == "success" and self.exit_code != 0:
            raise ValueError("success requires exit_code exactly 0")
        if self.outcome == "failure" and (
            self.exit_code is None or self.exit_code == 0
        ):
            raise ValueError("failure requires a nonzero strict int exit_code")
        if self.outcome in ("timeout", "output_limit") and (
            self.exit_code is not None
        ):
            raise ValueError("timeout/output_limit require exit_code None")
        if self.outcome == "output_limit" and not (
            self.stdout_truncated or self.stderr_truncated
        ):
            raise ValueError(
                "output_limit requires at least one truncated flag true"
            )
        if self.outcome != "output_limit" and (
            self.stdout_truncated or self.stderr_truncated
        ):
            raise ValueError(
                "only output_limit observations may set truncated flags"
            )
        return self


class CommandBatchSnapshot(BaseModel):
    """一批命令快照的不可变领域合同。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    subject_digest: str
    commands: tuple[CommandObservation, ...] = Field(min_length=1)
    environment_fingerprint: str
    manifest_artifact_digest: str
    complete: bool = Field(strict=True)
    all_passed: bool = Field(strict=True)
    collected_at: AwareDatetime

    @field_validator(
        "subject_digest",
        "environment_fingerprint",
        "manifest_artifact_digest",
        mode="before",
    )
    @classmethod
    def _validate_digest(cls, value: object) -> str:
        if not isinstance(value, str) or _SHA256_DIGEST_RE.fullmatch(
            value
        ) is None:
            raise ValueError("must be a lowercase sha256:<64 hex> digest")
        return value

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "CommandBatchSnapshot":
        command_ids = [
            observation.command_id for observation in self.commands
        ]
        if len(set(command_ids)) != len(command_ids):
            raise ValueError("commands command_id values must be unique")
        has_truncation = any(
            observation.outcome in ("timeout", "output_limit")
            for observation in self.commands
        )
        if self.complete != (not has_truncation):
            raise ValueError(
                "complete must be true iff no timeout/output_limit observation"
            )
        if self.all_passed != all(
            observation.outcome == "success" for observation in self.commands
        ):
            raise ValueError(
                "all_passed must be true iff every observation is success"
            )
        return self


class CommandBatchResult(BaseModel):
    """命令批次快照及其确定性 Evidence 的不可变结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    snapshot: CommandBatchSnapshot
    evidence: Evidence

    @model_validator(mode="after")
    def _cross_field_rules(self) -> "CommandBatchResult":
        snapshot = self.snapshot
        evidence = self.evidence
        if snapshot.subject_digest != evidence.subject_digest:
            raise ValueError("snapshot and evidence subject digests must match")
        if snapshot.manifest_artifact_digest != evidence.artifact_digest:
            raise ValueError(
                "snapshot and evidence artifact digests must match"
            )
        if evidence.kind != "command_batch":
            raise ValueError("evidence kind must be command_batch")
        if evidence.producer != "collector.command":
            raise ValueError("evidence producer must be collector.command")
        if evidence.trust_level != "deterministic":
            raise ValueError("evidence trust_level must be deterministic")
        if evidence.source_ref != f"command_batch:{snapshot.subject_digest}":
            raise ValueError(
                "evidence source_ref must be command_batch:<subject_digest>"
            )
        if evidence.collected_at != snapshot.collected_at:
            raise ValueError(
                "evidence collected_at must equal snapshot collected_at"
            )
        outcomes = [
            observation.outcome for observation in snapshot.commands
        ]
        if any(outcome in ("timeout", "output_limit") for outcome in outcomes):
            expected_status = "truncated"
        elif any(outcome == "failure" for outcome in outcomes):
            expected_status = "failure"
        else:
            expected_status = "success"
        if evidence.status != expected_status:
            raise ValueError("evidence status does not match command outcomes")
        return self


def _restricted_environment() -> dict[str, str]:
    """构造精确八键的确定性执行环境。"""
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONHASHSEED": "0",
        "LC_ALL": "C",
        "LANG": "C",
        "NO_COLOR": "1",
        "TERM": "dumb",
        "GIT_TERMINAL_PROMPT": "0",
        "PIP_NO_INPUT": "1",
    }


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, **_JSON_KWARGS).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _environment_fingerprint(env: dict[str, str]) -> str:
    return _sha256_bytes(_canonical_json_bytes(env))


class _BoundedPipeReader(threading.Thread):
    """并发排空一条管道，且每条流最多保留 limit 字节。"""

    def __init__(self, stream, limit: int, event: threading.Event) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._event = event
        self.data = b""
        self.truncated = False
        self.error: BaseException | None = None

    def run(self) -> None:
        collected = bytearray()
        try:
            while True:
                remaining = self._limit - len(collected)
                request = min(_READ_CHUNK_BYTES, remaining + 1)
                chunk = self._stream.read(request)
                if not chunk:
                    break
                if len(chunk) > remaining:
                    collected += chunk[:remaining]
                    self.truncated = True
                    self._event.set()
                    break
                collected += chunk
        except BaseException as exc:
            self.error = exc
            self._event.set()
        finally:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass
            self.data = bytes(collected)


class _TerminationController:
    """串行化对子进程进程组的 SIGTERM/SIGKILL。"""

    def __init__(self, proc) -> None:
        self._proc = proc
        self._lock = threading.Lock()
        self._sigterm_sent = False
        self._sigkill_sent = False

    def terminate(self) -> None:
        with self._lock:
            if self._sigterm_sent:
                return
            self._sigterm_sent = True
        _send_group_signal(self._proc, signal.SIGTERM)

    def kill(self) -> None:
        with self._lock:
            if self._sigkill_sent:
                return
            self._sigkill_sent = True
        _send_group_signal(self._proc, signal.SIGKILL)


def _send_group_signal(proc, sig: signal.Signals) -> None:
    if hasattr(os, "killpg"):
        try:
            os.killpg(proc.pid, sig)
            return
        except (OSError, ValueError):
            pass
    try:
        if sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise CommandExecutionError(
            "failed to signal command process group"
        ) from exc


@dataclass(frozen=True)
class _RawObservation:
    spec: CommandSpec
    outcome: Literal["success", "failure", "timeout", "output_limit"]
    exit_code: int | None
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool


class DeterministicCommandCollector:
    """冻结的确定性命令收集器：按命令 ID 选择、按请求顺序串行执行。"""

    def __init__(self, allowed_commands: tuple[CommandSpec, ...]) -> None:
        if type(allowed_commands) is not tuple:
            raise TypeError("allowed_commands must be an exact tuple")
        if not allowed_commands:
            raise ValueError(
                "allowed_commands must contain at least one CommandSpec"
            )
        if len(allowed_commands) > _MAX_COMMANDS:
            raise ValueError(
                "allowed_commands must contain at most "
                f"{_MAX_COMMANDS} CommandSpec values"
            )
        seen: set[str] = set()
        for index, spec in enumerate(allowed_commands):
            if type(spec) is not CommandSpec:
                raise TypeError(
                    "allowed_commands must contain exact CommandSpec values"
                )
            if spec.command_id in seen:
                raise ValueError(
                    "allowed_commands command_id values must be unique"
                )
            seen.add(spec.command_id)
        self._allowed_commands = allowed_commands
        self._lookup = MappingProxyType(
            {spec.command_id: spec for spec in allowed_commands}
        )

    def collect(
        self,
        workspace_path: Path,
        *,
        subject_digest: str,
        artifact_store: ArtifactStore,
        command_ids: tuple[str, ...],
        collected_at: datetime | None = None,
    ) -> CommandBatchResult:
        if not isinstance(workspace_path, Path):
            raise TypeError("workspace_path must be a pathlib.Path")
        if (
            not isinstance(subject_digest, str)
            or _SHA256_DIGEST_RE.fullmatch(subject_digest) is None
        ):
            raise ValueError(
                "subject_digest must be a lowercase sha256:<64 hex> digest"
            )
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        if type(command_ids) is not tuple:
            raise TypeError("command_ids must be an exact tuple")
        if not command_ids:
            raise ValueError("command_ids must contain at least one command id")
        if len(command_ids) > _MAX_COMMANDS:
            raise ValueError(
                f"command_ids must contain at most {_MAX_COMMANDS} command ids"
            )
        seen: set[str] = set()
        for command_id in command_ids:
            if type(command_id) is not str:
                raise TypeError("command_ids must contain exact str values")
            if command_id in seen:
                raise ValueError("command_ids values must be unique")
            seen.add(command_id)
            if command_id not in self._lookup:
                raise ValueError("command_ids must reference known commands")
        if collected_at is None:
            collected_at = datetime.now(timezone.utc)
        elif not isinstance(collected_at, datetime):
            raise TypeError("collected_at must be a datetime")
        elif collected_at.tzinfo is None or collected_at.utcoffset() is None:
            raise ValueError("collected_at must be timezone-aware")

        root = self._resolve_workspace_root(workspace_path)
        env = _restricted_environment()
        environment_fingerprint = _environment_fingerprint(env)

        raw_records: list[_RawObservation] = []
        for command_id in command_ids:
            spec = self._lookup[command_id]
            resolved_cwd = self._resolve_cwd(root, spec.cwd)
            raw_records.append(self._execute_command(spec, resolved_cwd, env))

        observations: list[CommandObservation] = []
        for raw in raw_records:
            stdout_digest = self._store_and_verify(
                artifact_store, raw.stdout
            )
            stderr_digest = self._store_and_verify(
                artifact_store, raw.stderr
            )
            observations.append(
                CommandObservation(
                    schema_version="v1",
                    command_id=raw.spec.command_id,
                    kind=raw.spec.kind,
                    argv=raw.spec.argv,
                    cwd=raw.spec.cwd,
                    outcome=raw.outcome,
                    exit_code=raw.exit_code,
                    duration_ms=raw.duration_ms,
                    stdout_artifact_digest=stdout_digest,
                    stderr_artifact_digest=stderr_digest,
                    stdout_bytes=len(raw.stdout),
                    stderr_bytes=len(raw.stderr),
                    stdout_truncated=raw.stdout_truncated,
                    stderr_truncated=raw.stderr_truncated,
                )
            )
        complete = not any(
            observation.outcome in ("timeout", "output_limit")
            for observation in observations
        )
        all_passed = all(
            observation.outcome == "success" for observation in observations
        )
        manifest = {
            "schema_version": "v1",
            "subject_digest": subject_digest,
            "observations": [
                observation.model_dump(mode="json")
                for observation in observations
            ],
            "environment_fingerprint": environment_fingerprint,
            "complete": complete,
            "all_passed": all_passed,
            "limits": {
                "max_commands": _MAX_COMMANDS,
                "read_chunk_bytes": _READ_CHUNK_BYTES,
            },
        }
        manifest_bytes = _canonical_json_bytes(manifest)
        manifest_artifact_digest = self._store_and_verify(
            artifact_store, manifest_bytes
        )
        status = (
            "truncated"
            if not complete
            else ("failure" if not all_passed else "success")
        )
        evidence_id = (
            "ev_command_"
            + hashlib.sha256(
                (subject_digest + manifest_artifact_digest).encode("ascii")
            ).hexdigest()[:32]
        )
        snapshot = CommandBatchSnapshot(
            schema_version="v1",
            subject_digest=subject_digest,
            commands=tuple(observations),
            environment_fingerprint=environment_fingerprint,
            manifest_artifact_digest=manifest_artifact_digest,
            complete=complete,
            all_passed=all_passed,
            collected_at=collected_at,
        )
        evidence = Evidence(
            schema_version="v1",
            evidence_id=evidence_id,
            subject_digest=subject_digest,
            kind="command_batch",
            producer="collector.command",
            artifact_digest=manifest_artifact_digest,
            source_ref=f"command_batch:{subject_digest}",
            status=status,
            trust_level="deterministic",
            collected_at=collected_at,
        )
        return CommandBatchResult(
            schema_version="v1",
            snapshot=snapshot,
            evidence=evidence,
        )

    @staticmethod
    def _resolve_workspace_root(workspace_path: Path) -> Path:
        root = Path(os.path.abspath(workspace_path))
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise CommandSpecError(
                "workspace path must be an existing directory"
            ) from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise CommandSpecError("workspace path must not be a symlink")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise CommandSpecError("workspace path must be a directory")
        return root

    @staticmethod
    def _resolve_cwd(root: Path, spec_cwd: str) -> Path:
        current = root
        if spec_cwd == ".":
            return current
        for part in spec_cwd.split("/"):
            current = current / part
            try:
                current_stat = current.lstat()
            except OSError as exc:
                raise CommandSpecError(
                    "command cwd must be an existing directory "
                    "under the workspace"
                ) from exc
            if stat.S_ISLNK(current_stat.st_mode):
                raise CommandSpecError(
                    "command cwd must not traverse symlinks"
                )
            if not stat.S_ISDIR(current_stat.st_mode):
                raise CommandSpecError("command cwd must be a directory")
        return current

    def _execute_command(
        self,
        spec: CommandSpec,
        resolved_cwd: Path,
        env: dict[str, str],
    ) -> _RawObservation:
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                list(spec.argv),
                cwd=resolved_cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise CommandLaunchError("failed to launch command") from exc

        controller = _TerminationController(proc)
        cap_event = threading.Event()
        stdout_reader = _BoundedPipeReader(
            proc.stdout, spec.max_output_bytes, cap_event
        )
        stderr_reader = _BoundedPipeReader(
            proc.stderr, spec.max_output_bytes, cap_event
        )
        stdout_reader.start()
        stderr_reader.start()
        timed_out = False
        try:
            deadline = start + spec.timeout_seconds
            while True:
                if cap_event.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    proc.wait(
                        timeout=min(_EVENT_POLL_SECONDS, remaining)
                    )
                except subprocess.TimeoutExpired:
                    continue
                except OSError as exc:
                    raise CommandExecutionError(
                        "failed to wait for command"
                    ) from exc
                # The process exited. Give readers a brief bounded moment
                # to report a cap/error that raced with the exit.
                cap_event.wait(
                    min(
                        _EVENT_POLL_SECONDS,
                        max(0.0, deadline - time.monotonic()),
                    )
                )
                break
            if timed_out or cap_event.is_set():
                controller.terminate()
                try:
                    proc.wait(timeout=_TERMINATION_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    controller.kill()
                    try:
                        proc.wait()
                    except OSError as exc:
                        raise CommandExecutionError(
                            "failed to reap command"
                        ) from exc
            self._join_readers((stdout_reader, stderr_reader))
            self._close_streams(proc)
            self._raise_reader_error(stdout_reader, "stdout")
            self._raise_reader_error(stderr_reader, "stderr")
            truncated = stdout_reader.truncated or stderr_reader.truncated
            if truncated:
                outcome: Literal[
                    "success", "failure", "timeout", "output_limit"
                ] = "output_limit"
                exit_code: int | None = None
            elif timed_out:
                outcome = "timeout"
                exit_code = None
            elif proc.returncode == 0:
                outcome = "success"
                exit_code = 0
            else:
                outcome = "failure"
                exit_code = proc.returncode
            duration_ms = max(
                0, int(round((time.monotonic() - start) * 1000))
            )
            return _RawObservation(
                spec=spec,
                outcome=outcome,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stdout=stdout_reader.data,
                stderr=stderr_reader.data,
                stdout_truncated=stdout_reader.truncated,
                stderr_truncated=stderr_reader.truncated,
            )
        finally:
            try:
                if proc.poll() is None:
                    controller.kill()
                    try:
                        proc.wait(timeout=_TERMINATION_WAIT_SECONDS)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait()
                        except OSError as exc:
                            raise CommandExecutionError(
                                "failed to reap command"
                            ) from exc
                self._join_readers((stdout_reader, stderr_reader))
                self._close_streams(proc)
            except OSError as exc:
                raise CommandExecutionError(
                    "failed to clean up command process"
                ) from exc

    @staticmethod
    def _join_readers(readers) -> None:
        lingering = False
        for reader in readers:
            reader.join(timeout=_JOIN_TIMEOUT_SECONDS)
            if reader.is_alive():
                try:
                    reader._stream.close()
                except (OSError, ValueError):
                    pass
                reader.join(timeout=_JOIN_TIMEOUT_SECONDS)
                if reader.is_alive():
                    lingering = True
        if lingering:
            raise CommandExecutionError(
                "command pipe reader failed to stop"
            )

    @staticmethod
    def _close_streams(proc) -> None:
        for stream in (proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _raise_reader_error(reader, name: str) -> None:
        if reader.error is not None:
            raise CommandExecutionError(
                f"command {name} pipe reader failed"
            ) from reader.error

    @staticmethod
    def _store_and_verify(
        artifact_store: ArtifactStore, data: bytes
    ) -> str:
        digest = artifact_store.put_bytes(data)
        if not artifact_store.verify(digest):
            raise CommandExecutionError("artifact verification failed")
        if artifact_store.get_bytes(digest) != data:
            raise CommandExecutionError("artifact verification failed")
        return digest
