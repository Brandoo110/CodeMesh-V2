"""Bounded, read-only transport for the saved-auth Codex CLI reviewer.

The assurance service only sees the existing ``ReviewerInvoker`` contract.  All
CLI details, process-group cleanup, event filtering, and temporary files stay
inside this adapter so callers cannot accidentally opt into a shell, a tool,
or a second retry path.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .run_service import (
    ReviewerInvocationResponse,
    ReviewerRoute,
    _REVIEWER_FAILURE_STAGE_CODES,
)
from .single_reviewer import (
    SingleReviewerPrompt,
    _RESPONSE_SCHEMA_TEXT,
    _ResponseDraft,
    _parse_strict_json,
)


PROVIDER = "openai-codex-desktop"
MODEL_REF = "gpt-5.6-luna"

_DEFAULT_TEMP_ROOT = Path("/private/tmp")
_MAX_STDOUT_BYTES = 1024 * 1024
_MAX_STDERR_BYTES = 256 * 1024
_MAX_FINAL_BYTES = 1024 * 1024
_MAX_LINE_BYTES = 256 * 1024
_READ_CHUNK_BYTES = 32 * 1024
_TERMINATION_GRACE_SECONDS = 0.5
_KILL_GRACE_SECONDS = 1.0

_ALLOWED_ENV_KEYS = frozenset(
    {
        "HOME",
        "CODEX_HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "NO_COLOR",
    }
)

_SAFE_LIFECYCLE_EVENTS = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "item.started",
        "item.updated",
        "item.completed",
    }
)
_ITEM_EVENTS = frozenset(
    {"item.started", "item.updated", "item.completed"}
)
_SAFE_ITEM_TYPES = frozenset({"reasoning", "agent_message"})
_RESPONSE_KEYS = frozenset(
    {"schema_version", "subject_digest", "rubric_hash", "findings", "questions"}
)

_MISSING = object()


class _OutputLimit(Exception):
    """One bounded stream or final file exceeded its byte/line budget."""


class _MalformedOutput(Exception):
    """A CLI event or final response is not in the frozen safe shape."""


class _MissingFinal(Exception):
    """The CLI did not produce exactly one final agent message."""


@dataclass(eq=False)
class _InvocationState:
    task: asyncio.Task[Any] | None
    creation_task: asyncio.Task[Any] | None = None
    process: Any | None = None
    close_requested: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _safe_environment(source: Mapping[str, str] | None) -> dict[str, str]:
    """Copy only non-secret system variables needed by saved-auth Codex."""

    values = os.environ if source is None else source
    if not isinstance(values, Mapping):
        raise TypeError("environment must be a mapping")
    result: dict[str, str] = {}
    # Deliberately access only the allowlisted names.  In particular, do not
    # iterate over the source: that would needlessly read secret values.
    for name in sorted(_ALLOWED_ENV_KEYS):
        try:
            value = values.get(name, _MISSING)
        except Exception:
            continue
        if type(value) is str and value and "\x00" not in value:
            result[name] = value
    return result


def _resolve_binary(binary_path: str | Path | None) -> Path:
    candidate: str | Path | None = binary_path
    if candidate is None:
        candidate = shutil.which("codex")
    if candidate is None or not str(candidate).strip():
        raise FileNotFoundError("codex binary is unavailable")
    path = Path(candidate)
    if not path.is_absolute():
        raise ValueError("codex binary must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise ValueError("codex binary is invalid") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise ValueError("codex binary is not executable")
    return resolved


def _resolve_temp_root(temp_root: str | Path) -> Path:
    path = Path(temp_root)
    if not path.is_absolute():
        raise ValueError("temp_root must be absolute")
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
    except (OSError, RuntimeError):
        raise ValueError("temp_root is invalid") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("temp_root must be a directory")
    return resolved


def _build_argv(
    binary: Path,
    output_schema: Path,
    output_last_message: Path,
) -> tuple[str, ...]:
    configs = (
        'approval_policy="never"',
        'shell_environment_policy.inherit="none"',
        'shell_environment_policy.ignore_default_excludes=false',
        'model_reasoning_effort="max"',
        'service_tier="fast"',
        'web_search="disabled"',
        "features.shell_tool=false",
        "features.apps=false",
        "features.multi_agent=false",
        "features.hooks=false",
        "features.remote_plugin=false",
        "features.goals=false",
        "features.memories=false",
        "features.browser_use=false",
        "features.browser_use_external=false",
        "features.browser_use_full_cdp_access=false",
        "features.computer_use=false",
        "features.enable_mcp_apps=false",
        "features.image_generation=false",
        "features.in_app_browser=false",
        "features.plugins=false",
        "features.skill_search=false",
        "features.view_image=false",
        "tools.view_image=false",
    )
    argv: list[str] = [
        str(binary),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        MODEL_REF,
        "--json",
        "--output-schema",
        str(output_schema),
        "--output-last-message",
        str(output_last_message),
    ]
    for config in configs:
        argv.extend(("-c", config))
    argv.append("-")
    return tuple(argv)


def _line_length_check(data: bytes, line_length: int) -> int:
    for byte in data:
        if byte == 0x0A:
            line_length = 0
        else:
            line_length += 1
            if line_length > _MAX_LINE_BYTES:
                raise _OutputLimit
    return line_length


async def _read_stream(stream: Any, limit: int) -> bytes:
    collected = bytearray()
    line_length = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if type(chunk) is not bytes:
            raise _MalformedOutput
        if not chunk:
            break
        if len(collected) + len(chunk) > limit:
            raise _OutputLimit
        line_length = _line_length_check(chunk, line_length)
        collected.extend(chunk)
    return bytes(collected)


def _read_file(path: Path, limit: int) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise _MissingFinal from None
    except OSError:
        raise _MalformedOutput from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _MalformedOutput
    collected = bytearray()
    line_length = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                if len(collected) + len(chunk) > limit:
                    raise _OutputLimit
                line_length = _line_length_check(chunk, line_length)
                collected.extend(chunk)
    except (_OutputLimit, _MalformedOutput):
        raise
    except (OSError, ValueError):
        raise _MalformedOutput from None
    return bytes(collected)


def _parse_event_stream(raw: bytes) -> tuple[str, ...]:
    if not raw:
        raise _MissingFinal
    messages: list[str] = []
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        raise _MissingFinal
    for line in lines:
        if not line:
            raise _MalformedOutput
        if len(line) > _MAX_LINE_BYTES:
            raise _OutputLimit
        try:
            event = json.loads(
                line.decode("utf-8", errors="strict"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            raise _MalformedOutput from None
        if type(event) is not dict or type(event.get("type")) is not str:
            raise _MalformedOutput
        event_type = event["type"]
        if event_type == "agent_message":
            text = event.get("text")
            if type(text) is not str or not text.strip():
                raise _MissingFinal
            messages.append(text)
            continue
        if event_type == "reasoning":
            continue
        if event_type not in _SAFE_LIFECYCLE_EVENTS:
            raise _MalformedOutput
        if event_type not in _ITEM_EVENTS:
            if "item" in event:
                raise _MalformedOutput
            continue
        item = event.get("item", _MISSING)
        if type(item) is not dict:
            raise _MalformedOutput
        item_type = item.get("type")
        if item_type not in _SAFE_ITEM_TYPES:
            # This is the fail-closed branch for command/file/MCP/web/browser/
            # computer/image/tool events and for future unknown item types.
            raise _MalformedOutput
        if event_type == "item.completed" and item_type == "agent_message":
            text = item.get("text")
            if type(text) is not str or not text.strip():
                raise _MissingFinal
            messages.append(text)
    if len(messages) != 1:
        raise _MissingFinal
    return tuple(messages)


def _validate_final_response(raw: bytes) -> None:
    try:
        payload = _parse_strict_json(raw)
        if frozenset(payload) != _RESPONSE_KEYS:
            raise ValueError("response schema drift")
        _ResponseDraft.model_validate_json(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    except Exception:
        raise _MalformedOutput from None


def _process_returncode(process: Any) -> int | None:
    try:
        value = process.returncode
    except Exception:
        return None
    return value if type(value) is int else None


def _send_signal(process: Any, sig: signal.Signals) -> None:
    try:
        pid = process.pid
        os.killpg(pid, sig)
        return
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        if sig is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except (AttributeError, OSError, ProcessLookupError):
        pass


async def _terminate_process(process: Any) -> None:
    if _process_returncode(process) is not None:
        return
    _send_signal(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), _TERMINATION_GRACE_SECONDS)
        return
    except (asyncio.TimeoutError, TimeoutError):
        pass
    except (OSError, RuntimeError):
        pass
    _send_signal(process, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), _KILL_GRACE_SECONDS)
    except (asyncio.TimeoutError, TimeoutError, OSError, RuntimeError):
        # A real SIGKILL has already been sent.  The final best-effort wait is
        # deliberately bounded so cancellation and close never hang forever.
        pass


async def _feed_stdin(process: Any, payload: bytes) -> None:
    stream = getattr(process, "stdin", None)
    if stream is None:
        raise _MalformedOutput
    try:
        stream.write(payload)
        await stream.drain()
    finally:
        try:
            stream.close()
        except (AttributeError, OSError, ValueError):
            pass


async def _communicate(process: Any, payload: bytes) -> tuple[bytes, bytes, int | None]:
    stdout = getattr(process, "stdout", None)
    stderr = getattr(process, "stderr", None)
    if stdout is None or stderr is None:
        raise _MalformedOutput
    tasks = (
        asyncio.create_task(_read_stream(stdout, _MAX_STDOUT_BYTES)),
        asyncio.create_task(_read_stream(stderr, _MAX_STDERR_BYTES)),
        asyncio.create_task(_feed_stdin(process, payload)),
        asyncio.create_task(process.wait()),
    )
    try:
        stdout_data, stderr_data, _ignored, returncode = await asyncio.gather(*tasks)
        return stdout_data, stderr_data, returncode
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _await_owned_task(task: asyncio.Task[Any]) -> Any:
    """Drain an owned task even when its parent is already cancelling."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class CodexCliReviewerInvoker:
    """Invoke the fixed Luna/max Codex CLI route once per call."""

    provider = PROVIDER
    model_ref = MODEL_REF

    def __init__(
        self,
        binary_path: str | Path | None = None,
        *,
        temp_root: str | Path = _DEFAULT_TEMP_ROOT,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._binary = _resolve_binary(binary_path)
        self._temp_root = _resolve_temp_root(temp_root)
        if environ is not None and not isinstance(environ, Mapping):
            raise TypeError("environ must be a mapping")
        self._environ = environ
        self._closed = False
        self._active: set[_InvocationState] = set()

    async def invoke(
        self,
        prompt: SingleReviewerPrompt,
        *,
        run_id: str,
        route: ReviewerRoute,
    ) -> ReviewerInvocationResponse:
        if self._closed:
            raise RuntimeError("reviewer invoker is closed")
        if type(route) is not ReviewerRoute:
            raise TypeError("route must be an exact ReviewerRoute")
        if (
            route.provider != PROVIDER
            or route.model_ref != MODEL_REF
            or route.tool_grants != ()
        ):
            raise ValueError("reviewer route does not match the Codex CLI route")
        if type(run_id) is not str or not run_id.strip():
            raise ValueError("run_id must be a nonblank string")
        if type(prompt) is not SingleReviewerPrompt:
            raise TypeError("prompt must be an exact SingleReviewerPrompt")
        try:
            checked_prompt = SingleReviewerPrompt.model_validate_json(
                prompt.model_dump_json()
            )
        except Exception:
            raise ValueError("prompt failed deterministic JSON round-trip validation") from None

        state = _InvocationState(asyncio.current_task())
        self._active.add(state)
        try:
            return await self._invoke_once(state, checked_prompt, run_id, route)
        except asyncio.CancelledError:
            await asyncio.shield(self._cleanup_state(state))
            raise
        finally:
            self._active.discard(state)
            state.done.set()

    async def _invoke_once(
        self,
        state: _InvocationState,
        prompt: SingleReviewerPrompt,
        run_id: str,
        route: ReviewerRoute,
    ) -> ReviewerInvocationResponse:
        started = _now()
        process: Any | None = None
        try:
            with tempfile.TemporaryDirectory(
                prefix="codemesh-codex-reviewer-",
                dir=str(self._temp_root),
            ) as temporary_root:
                root = Path(temporary_root)
                cwd = root / "cwd"
                cwd.mkdir()
                output_schema = root / "output-schema.json"
                output_schema.write_text(_RESPONSE_SCHEMA_TEXT + "\n", encoding="utf-8")
                output_last_message = root / "output-last-message.json"
                argv = _build_argv(self._binary, output_schema, output_last_message)
                creation_task = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *argv,
                        cwd=str(cwd),
                        env=_safe_environment(self._environ),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        start_new_session=True,
                    )
                )
                state.creation_task = creation_task
                try:
                    process = await asyncio.shield(creation_task)
                except asyncio.CancelledError:
                    try:
                        process = await _await_owned_task(creation_task)
                    except BaseException:
                        process = None
                    if process is not None:
                        state.process = process
                        await _terminate_process(process)
                        state.process = None
                    raise
                finally:
                    state.creation_task = None
                state.process = process
                if state.close_requested:
                    await _terminate_process(process)
                    state.process = None
                    raise asyncio.CancelledError
                try:
                    stdout, _stderr, returncode = await asyncio.wait_for(
                        _communicate(
                            process,
                            prompt.prompt_text.encode("utf-8"),
                        ),
                        timeout=route.timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    return self._failure(
                        started,
                        _now(),
                        status="timeout",
                        error_code="REVIEWER_TIMEOUT",
                    )
                except _OutputLimit:
                    return self._failure(
                        started,
                        _now(),
                        status="budget_exceeded",
                        error_code="REVIEWER_BUDGET_EXCEEDED",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_PROVIDER_FAILURE",
                        failure_stage="process_communication",
                    )
                finally:
                    await _terminate_process(process)
                    state.process = None

                if type(returncode) is not int:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_PROVIDER_FAILURE",
                        failure_stage="process_communication",
                    )
                if returncode != 0:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_PROVIDER_FAILURE",
                        failure_stage="nonzero_exit",
                    )
                try:
                    messages = _parse_event_stream(stdout)
                except _OutputLimit:
                    return self._failure(
                        started,
                        _now(),
                        status="budget_exceeded",
                        error_code="REVIEWER_BUDGET_EXCEEDED",
                    )
                except _MissingFinal:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_RESPONSE_MISSING",
                        failure_stage="final_missing",
                    )
                except _MalformedOutput:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_PROVIDER_FAILURE",
                        failure_stage="event_stream_invalid",
                    )
                except Exception:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_PROVIDER_FAILURE",
                        failure_stage="event_stream_invalid",
                    )
                try:
                    final = _read_file(output_last_message, _MAX_FINAL_BYTES)
                    if not final.strip():
                        raise _MissingFinal
                    if messages[0].encode("utf-8").strip() != final.strip():
                        raise _MalformedOutput
                    _validate_final_response(final)
                except _OutputLimit:
                    return self._failure(
                        started,
                        _now(),
                        status="budget_exceeded",
                        error_code="REVIEWER_BUDGET_EXCEEDED",
                    )
                except _MissingFinal:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_RESPONSE_MISSING",
                        failure_stage="final_missing",
                    )
                except Exception:
                    return self._failure(
                        started,
                        _now(),
                        status="failure",
                        error_code="REVIEWER_PROVIDER_FAILURE",
                        failure_stage="final_schema_invalid",
                    )
                return ReviewerInvocationResponse(
                    status="success",
                    provider=PROVIDER,
                    model_ref=MODEL_REF,
                    started_at=started,
                    completed_at=_now(),
                    raw_response=final,
                    schema_status="unverified",
                    usage_status="unavailable",
                )
        except asyncio.CancelledError:
            raise
        except _OutputLimit:
            return self._failure(
                started,
                _now(),
                status="budget_exceeded",
                error_code="REVIEWER_BUDGET_EXCEEDED",
            )
        except Exception:
            return self._failure(
                started,
                _now(),
                status="failure",
                error_code="REVIEWER_PROVIDER_FAILURE",
                failure_stage="process_launch",
            )
        finally:
            if process is not None:
                await _terminate_process(process)
                state.process = None

    async def _cleanup_state(self, state: _InvocationState) -> None:
        process = state.process
        if process is not None:
            await _terminate_process(process)
            state.process = None

    async def aclose(self) -> None:
        """Close once and cancel/reap every invocation already in flight."""

        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        active = tuple(self._active)
        for state in active:
            state.close_requested = True
            if state.task is not None and state.task is not current:
                state.task.cancel()
        waits = tuple(state.done.wait() for state in active if state.task is not current)
        if waits:
            await asyncio.gather(*waits, return_exceptions=True)

    @staticmethod
    def _failure(
        started: datetime,
        completed: datetime,
        *,
        status: str,
        error_code: str,
        failure_stage: str | None = None,
    ) -> ReviewerInvocationResponse:
        if failure_stage is not None:
            error_code = _REVIEWER_FAILURE_STAGE_CODES[failure_stage]
        return ReviewerInvocationResponse(
            status=status,
            provider=PROVIDER,
            model_ref=MODEL_REF,
            started_at=started,
            completed_at=completed,
            schema_status="not_produced",
            error_code=error_code,
        )


__all__ = ("CodexCliReviewerInvoker",)
