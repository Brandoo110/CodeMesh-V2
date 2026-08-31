"""Focused tests for the isolated Codex CLI reviewer adapter."""

import asyncio
import json
from pathlib import Path

import pytest

import assurance.codex_cli_reviewer_invoker as codex_module
from assurance.codex_cli_reviewer_invoker import CodexCliReviewerInvoker
from assurance.run_service import ReviewerRoute


def _route(**updates):
    values = {
        "provider": "openai-codex-desktop",
        "model_ref": "gpt-5.6-luna",
        "timeout_seconds": 5,
        "token_budget": 4096,
        "routing_rule": "single_general.v0:fixed",
    }
    values.update(updates)
    return ReviewerRoute.model_validate(values)


def _prompt():
    from tests.test_assurance_single_reviewer import _prompt as build_prompt

    return build_prompt()


def test_codex_cli_invoker_uses_fixed_safe_command_and_filtered_environment(
    tmp_path, monkeypatch
):
    """The first RED contract freezes the process boundary and argv."""

    binary = tmp_path / "codex"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    calls = []
    final = json.dumps(
        {
            "schema_version": "v1",
            "subject_digest": _prompt().input.subject.subject_digest,
            "rubric_hash": _prompt().rubric_hash,
            "findings": [],
            "questions": [],
        },
        separators=(",", ":"),
    )

    event = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": final},
        },
        separators=(",", ":"),
    ).encode() + b"\n"

    class _Stream:
        def __init__(self, value=b""):
            self.value = value

        async def read(self, _size):
            value, self.value = self.value, b""
            return value

    class _Stdin:
        def write(self, value):
            self.value = value

        async def drain(self):
            return None

        def close(self):
            return None

    class _Process:
        pid = 987654
        returncode = 0

        def __init__(self):
            self.stdin = _Stdin()
            self.stdout = _Stream(event)
            self.stderr = _Stream()

        async def wait(self):
            return self.returncode

    async def launch(*argv, **kwargs):
        calls.append((argv, kwargs))
        cwd = Path(kwargs["cwd"])
        assert list(cwd.iterdir()) == []
        output_schema = Path(argv[argv.index("--output-schema") + 1])
        output_last_message = Path(argv[argv.index("--output-last-message") + 1])
        assert output_schema.is_file()
        assert output_last_message.parent != cwd
        output_last_message.write_text(final, encoding="utf-8")
        return _Process()

    monkeypatch.setattr(
        "assurance.codex_cli_reviewer_invoker.asyncio.create_subprocess_exec",
        launch,
    )
    monkeypatch.setenv("HOME", "/safe/home")
    monkeypatch.setenv("CODEX_HOME", "/safe/codex")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("NO_COLOR", "1")
    for name in ("LANG", "LC_ALL", "LC_CTYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", "/must-not-pass")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass")
    monkeypatch.setenv("CODEMESH_ASSURANCE_REVIEWER_API_KEY", "must-not-pass")

    invoker = CodexCliReviewerInvoker(binary_path=binary, temp_root=tmp_path)
    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=_route()))

    assert result.status == "success"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == str(binary.resolve())
    assert argv[1:14] == (
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-luna",
        "--json",
        "--output-schema",
        argv[12],
        "--output-last-message",
    )
    assert "-" == argv[-1]
    assert 'approval_policy="never"' in argv
    assert 'shell_environment_policy.inherit="none"' in argv
    assert "shell_environment_policy.ignore_default_excludes=false" in argv
    assert 'model_reasoning_effort="max"' in argv
    assert 'service_tier="fast"' in argv
    assert 'web_search="disabled"' in argv
    assert "features.shell_tool=false" in argv
    assert "features.apps=false" in argv
    assert "features.multi_agent=false" in argv
    assert "features.hooks=false" in argv
    assert "features.remote_plugin=false" in argv
    assert "features.goals=false" in argv
    assert "features.memories=false" in argv
    for feature in (
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "enable_mcp_apps",
        "image_generation",
        "in_app_browser",
        "plugins",
        "skill_search",
        "view_image",
    ):
        assert f"features.{feature}=false" in argv
    assert "tools.view_image=false" in argv
    for incorrect in (
        "features.browser=false",
        "features.computer=false",
        "features.mcp=false",
        "features.image=false",
        "features.view=false",
    ):
        assert incorrect not in argv
    assert "shell" not in kwargs
    assert kwargs["start_new_session"] is True
    assert kwargs["env"] == {
        "HOME": "/safe/home",
        "CODEX_HOME": "/safe/codex",
        "TERM": "dumb",
        "NO_COLOR": "1",
    }


def _valid_response(prompt):
    return json.dumps(
        {
            "schema_version": "v1",
            "subject_digest": prompt.input.subject.subject_digest,
            "rubric_hash": prompt.rubric_hash,
            "findings": [],
            "questions": [],
        },
        separators=(",", ":"),
    )


def _event_stream(message, *, event_type="item.completed", item_type="agent_message"):
    if event_type == "item.completed":
        value = {"type": event_type, "item": {"type": item_type, "text": message}}
    else:
        value = {"type": event_type, "text": message}
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


class _FakeStream:
    def __init__(self, chunks=()):
        self._chunks = list(chunks)

    async def read(self, _size):
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, value):
        self.writes.append(value)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, *, stdout=b"", stderr=b"", returncode=0, wait_event=None):
        self.pid = 987655
        self.returncode = returncode
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream((stdout,))
        self.stderr = _FakeStream((stderr,))
        self.terminate_calls = 0
        self.kill_calls = 0
        self._wait_event = wait_event

    async def wait(self):
        if self.returncode is None:
            if self._wait_event is None:
                await asyncio.Future()
            else:
                await self._wait_event.wait()
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
        if self._wait_event is not None:
            self._wait_event.set()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        if self._wait_event is not None:
            self._wait_event.set()


def _fake_binary(tmp_path):
    binary = tmp_path / "codex"
    binary.write_bytes(b"#!/bin/sh\n")
    binary.chmod(0o755)
    return binary


def _install_launcher(monkeypatch, process, *, final=None, calls=None):
    calls = [] if calls is None else calls

    async def launch(*argv, **kwargs):
        calls.append((argv, kwargs, process))
        last_message = Path(argv[argv.index("--output-last-message") + 1])
        if final is not None:
            last_message.write_bytes(final)
        return process

    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", launch)
    return calls


def test_structured_success_maps_to_existing_invocation_response(tmp_path, monkeypatch):
    prompt = _prompt()
    final = _valid_response(prompt).encode()
    process = _FakeProcess(stdout=_event_stream(final.decode()))
    _install_launcher(monkeypatch, process, final=final)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={"HOME": "/safe"}
        ).invoke(prompt, run_id="run-1", route=_route())
    )

    assert result.status == "success"
    assert result.provider == "openai-codex-desktop"
    assert result.model_ref == "gpt-5.6-luna"
    assert result.raw_response == final
    assert result.schema_status == "unverified"
    assert result.usage_status == "unavailable"
    assert process.stdin.closed is True


@pytest.mark.parametrize(
    "stdout, final, expected_status, expected_error",
    (
        (
            _event_stream("{}", item_type="command_execution"),
            b"{}",
            "failure",
            "REVIEWER_PROVIDER_FAILURE",
        ),
        (
            b'{"type":"future.event"}\n',
            b"{}",
            "failure",
            "REVIEWER_PROVIDER_FAILURE",
        ),
        (
            b"not-json\n",
            b"{}",
            "failure",
            "REVIEWER_PROVIDER_FAILURE",
        ),
        (
            b'{"type":"turn.completed"}\n',
            b"{}",
            "failure",
            "REVIEWER_RESPONSE_MISSING",
        ),
        (
            _event_stream("{}") + _event_stream("{}"),
            b"{}",
            "failure",
            "REVIEWER_RESPONSE_MISSING",
        ),
    ),
)
def test_unsafe_or_missing_final_output_fails_closed(
    tmp_path,
    monkeypatch,
    stdout,
    final,
    expected_status,
    expected_error,
):
    process = _FakeProcess(stdout=stdout)
    _install_launcher(monkeypatch, process, final=final)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-1", route=_route())
    )

    assert (result.status, result.error_code) == (expected_status, expected_error)
    assert result.raw_response is None


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: value[:-1],
        lambda value: value.replace(b'"questions":[]', b'"extra":1,"questions":[]'),
        lambda value: value.replace(b'"schema_version":"v1"', b'"schema_version":"v2"'),
    ),
)
def test_final_schema_drift_is_failure(tmp_path, monkeypatch, mutate):
    prompt = _prompt()
    final = mutate(_valid_response(prompt).encode())
    process = _FakeProcess(stdout=_event_stream(final.decode(errors="ignore")))
    _install_launcher(monkeypatch, process, final=final)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(prompt, run_id="run-1", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROVIDER_FAILURE"


@pytest.mark.parametrize(
    "stream_name, value",
    (
        ("stdout", b"x" * (codex_module._MAX_STDOUT_BYTES + 1)),
        ("stderr", b"x" * (codex_module._MAX_STDERR_BYTES + 1)),
        ("stdout", b"x" * (codex_module._MAX_LINE_BYTES + 1)),
    ),
)
def test_stream_and_line_limits_are_budget_failures_and_terminate(
    tmp_path, monkeypatch, stream_name, value
):
    process = _FakeProcess(
        stdout=value if stream_name == "stdout" else b"",
        stderr=value if stream_name == "stderr" else b"",
        returncode=None,
        wait_event=asyncio.Event(),
    )
    monkeypatch.setattr(codex_module.os, "killpg", lambda *_: (_ for _ in ()).throw(ProcessLookupError))
    _install_launcher(monkeypatch, process, final=b"{}")

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-1", route=_route())
    )

    assert result.status == "budget_exceeded"
    assert result.error_code == "REVIEWER_BUDGET_EXCEEDED"
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_final_file_limit_is_a_budget_failure_and_terminates(tmp_path, monkeypatch):
    prompt = _prompt()
    final = b"x" * (codex_module._MAX_FINAL_BYTES + 1)
    process = _FakeProcess(stdout=_event_stream("{}"))
    _install_launcher(monkeypatch, process, final=final)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(prompt, run_id="run-1", route=_route())
    )

    assert result.status == "budget_exceeded"
    assert result.error_code == "REVIEWER_BUDGET_EXCEEDED"


def test_nonzero_process_is_deterministic_failure(tmp_path, monkeypatch):
    prompt = _prompt()
    final = _valid_response(prompt).encode()
    process = _FakeProcess(stdout=_event_stream(final.decode()), returncode=17)
    _install_launcher(monkeypatch, process, final=final)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(prompt, run_id="run-1", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROVIDER_FAILURE"
    assert result.raw_response is None


def test_timeout_terminates_process_group_and_returns_existing_timeout_status(
    tmp_path, monkeypatch
):
    process = _FakeProcess(
        returncode=None,
        wait_event=asyncio.Event(),
    )
    monkeypatch.setattr(codex_module.os, "killpg", lambda *_: (_ for _ in ()).throw(ProcessLookupError))
    _install_launcher(monkeypatch, process, final=None)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-1", route=_route(timeout_seconds=1))
    )

    assert result.status == "timeout"
    assert result.error_code == "REVIEWER_TIMEOUT"
    assert process.terminate_calls == 1


def test_cancelled_invocation_cleans_process_and_propagates_cancelled_error(
    tmp_path, monkeypatch
):
    process = _FakeProcess(returncode=None, wait_event=asyncio.Event())
    started = asyncio.Event()

    async def launch(*argv, **kwargs):
        started.set()
        return process

    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(codex_module.os, "killpg", lambda *_: (_ for _ in ()).throw(ProcessLookupError))

    async def scenario():
        invoker = CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        )
        task = asyncio.create_task(
            invoker.invoke(_prompt(), run_id="run-1", route=_route())
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await invoker.aclose()
        await invoker.aclose()

    asyncio.run(scenario())
    assert process.terminate_calls == 1


@pytest.mark.parametrize("cancel_mode", ("cancel", "aclose"))
def test_cancellation_during_process_creation_drains_owned_process_and_temp(
    tmp_path, monkeypatch, cancel_mode
):
    process = _FakeProcess(returncode=None, wait_event=asyncio.Event())
    creation_started = asyncio.Event()
    release_creation = asyncio.Event()
    temp_roots = []

    async def launch(*argv, **kwargs):
        temp_roots.append(Path(kwargs["cwd"]).parent)
        creation_started.set()
        await release_creation.wait()
        return process

    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(
        codex_module.os,
        "killpg",
        lambda *_: (_ for _ in ()).throw(ProcessLookupError),
    )

    async def scenario():
        loop = asyncio.get_running_loop()
        unhandled = []
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        invoker = CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        )
        invocation = asyncio.create_task(
            invoker.invoke(_prompt(), run_id="run-creation-race", route=_route())
        )
        await creation_started.wait()
        if cancel_mode == "cancel":
            invocation.cancel()
            await asyncio.sleep(0)
            assert not invocation.done()
            close_task = None
        else:
            close_task = asyncio.create_task(invoker.aclose())
            await asyncio.sleep(0)
            assert not close_task.done()
        release_creation.set()
        with pytest.raises(asyncio.CancelledError):
            await invocation
        if close_task is not None:
            await close_task
        else:
            await invoker.aclose()
        assert unhandled == []

    asyncio.run(scenario())
    assert process.terminate_calls == 1
    assert len(temp_roots) == 1
    assert not temp_roots[0].exists()


def test_aclose_cancels_active_invocation_and_is_idempotent(tmp_path, monkeypatch):
    process = _FakeProcess(returncode=None, wait_event=asyncio.Event())
    started = asyncio.Event()

    async def launch(*argv, **kwargs):
        started.set()
        return process

    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", launch)
    monkeypatch.setattr(codex_module.os, "killpg", lambda *_: (_ for _ in ()).throw(ProcessLookupError))

    async def scenario():
        invoker = CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        )
        task = asyncio.create_task(
            invoker.invoke(_prompt(), run_id="run-1", route=_route())
        )
        await started.wait()
        await invoker.aclose()
        await invoker.aclose()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError):
            await invoker.invoke(_prompt(), run_id="run-2", route=_route())

    asyncio.run(scenario())
    assert process.terminate_calls == 1
