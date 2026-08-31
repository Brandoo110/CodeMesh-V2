"""Focused tests for the isolated Codex CLI reviewer adapter."""

import asyncio
import json
from pathlib import Path

import pytest

import assurance.codex_cli_reviewer_invoker as codex_module
from assurance.codex_cli_reviewer_invoker import CodexCliReviewerInvoker
from assurance.run_service import ReviewerInvocationResponse, ReviewerRoute


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


def test_codex_cli_transport_schema_uses_openai_supported_array_subset(
    tmp_path, monkeypatch
):
    prompt = _prompt()
    final = _valid_response(prompt).encode()
    process = _FakeProcess(stdout=_event_stream(final.decode()))
    captured_schema = {}

    async def launch(*argv, **kwargs):
        output_schema = Path(argv[argv.index("--output-schema") + 1])
        captured_schema.update(json.loads(output_schema.read_text(encoding="utf-8")))
        output_last_message = Path(
            argv[argv.index("--output-last-message") + 1]
        )
        output_last_message.write_bytes(final)
        return process

    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", launch)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={"HOME": "/safe"}
        ).invoke(prompt, run_id="run-transport-schema", route=_route())
    )

    assert result.status == "success"

    def schema_nodes(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from schema_nodes(child)
        elif isinstance(value, list):
            for child in value:
                yield from schema_nodes(child)

    arrays = [
        node for node in schema_nodes(captured_schema) if node.get("type") == "array"
    ]
    assert arrays
    supported_array_keywords = {"type", "items", "minItems", "maxItems"}
    for array in arrays:
        assert set(array).issubset(supported_array_keywords)
        assert array["type"] == "array"
        assert "items" in array


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
            "REVIEWER_EVENT_STREAM_INVALID",
        ),
        (
            b'{"type":"future.event"}\n',
            b"{}",
            "failure",
            "REVIEWER_EVENT_STREAM_INVALID",
        ),
        (
            b"not-json\n",
            b"{}",
            "failure",
            "REVIEWER_EVENT_STREAM_INVALID",
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
    assert result.error_code == "REVIEWER_RESPONSE_SCHEMA_INVALID"


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
    assert result.error_code == "REVIEWER_PROCESS_NONZERO_EXIT"
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


def test_process_launch_failure_exposes_only_fixed_safe_failure_code(
    tmp_path, monkeypatch
):
    async def launch(*argv, **kwargs):
        raise OSError("launch secret /private/credential --token=do-not-leak")

    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", launch)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-launch", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROCESS_LAUNCH_FAILURE"
    assert result.raw_response is None
    assert result.error_message is None
    assert "credential" not in json.dumps(result.model_dump(mode="json"))


def test_process_communication_failure_exposes_only_fixed_safe_failure_code(
    tmp_path, monkeypatch
):
    class _ExplodingStream:
        async def read(self, _size):
            raise OSError("communication secret /tmp/raw-output")

    process = _FakeProcess(stdout=b"", stderr=b"")
    process.stdout = _ExplodingStream()
    _install_launcher(monkeypatch, process, final=b"{}")

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-communication", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROCESS_COMMUNICATION_FAILURE"
    assert result.raw_response is None
    assert result.error_message is None


def test_nonzero_exit_exposes_only_fixed_safe_failure_code(tmp_path, monkeypatch):
    prompt = _prompt()
    process = _FakeProcess(
        stdout=_event_stream(_valid_response(prompt)),
        returncode=17,
    )
    _install_launcher(monkeypatch, process, final=_valid_response(prompt).encode())

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(prompt, run_id="run-nonzero", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROCESS_NONZERO_EXIT"
    assert result.raw_response is None


@pytest.mark.parametrize(
    ("signal_text", "expected_category"),
    (
        ("authentication required: please log in", "auth"),
        ("rate limit exceeded for this account", "rate_or_quota"),
        ("model not found: gpt-5.6-luna", "model_availability"),
        ("network connection refused by transport", "network_or_transport"),
        ("provider internal server error", "provider_or_server"),
        ("permission denied by policy", "permission_or_policy"),
        ("cli failed without a known reason", "unknown"),
    ),
)
def test_nonzero_exit_classifies_bounded_stderr_to_fixed_category(
    tmp_path, monkeypatch, signal_text, expected_category
):
    process = _FakeProcess(
        stdout=b"",
        stderr=signal_text.encode("utf-8"),
        returncode=17,
    )
    _install_launcher(monkeypatch, process, final=None)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-classified-nonzero", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROCESS_NONZERO_EXIT"
    assert result.failure_category == expected_category
    assert result.raw_response is None
    assert result.error_message is None


def test_nonzero_failure_category_uses_explicit_precedence_for_mixed_signals(
    tmp_path, monkeypatch
):
    process = _FakeProcess(
        stderr=(
            b"permission denied; rate limit exceeded; authentication required; "
            b"network connection reset"
        ),
        returncode=17,
    )
    _install_launcher(monkeypatch, process, final=None)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-classified-precedence", route=_route())
    )

    assert result.failure_category == "auth"


def test_structured_jsonl_failure_reads_only_allowlisted_fields(
    tmp_path, monkeypatch
):
    event = json.dumps(
        {
            "type": "error",
            "code": "AUTH_REQUIRED",
            "status": "failed",
            "message": "authentication required",
            "details": {
                "secret": "token=do-not-leak",
                "path": "/private/hidden/provider-output",
            },
        },
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    process = _FakeProcess(stdout=event, returncode=17)
    _install_launcher(monkeypatch, process, final=None)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-structured-failure", route=_route())
    )

    assert result.failure_category == "auth"
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    assert "do-not-leak" not in serialized
    assert "/private/hidden/provider-output" not in serialized


def test_failure_classification_invalid_utf8_is_unknown_and_bounded(
    tmp_path, monkeypatch
):
    process = _FakeProcess(stderr=b"authentication required\xff", returncode=17)
    _install_launcher(monkeypatch, process, final=None)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-invalid-utf8", route=_route())
    )

    assert result.failure_category == "unknown"


def test_failure_classification_over_cap_is_unknown(tmp_path, monkeypatch):
    value = b"authentication required" + b"x" * codex_module._MAX_FAILURE_SIGNAL_BYTES
    process = _FakeProcess(stderr=value, returncode=17)
    _install_launcher(monkeypatch, process, final=None)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-over-cap", route=_route())
    )

    assert result.failure_category == "unknown"


@pytest.mark.parametrize(
    "stream_name, signal_text",
    (
        ("stdout", b"provider failed token=do-not-leak"),
        ("stderr", b"provider failed OPENAI_API_KEY=do-not-leak"),
        ("stderr", b"provider failed /private/local-output"),
        ("stdout", b"provider failed \\\\server\\\\share\\\\local-output"),
        ("stderr", b"provider failed file:///private/local-output"),
    ),
)
def test_unsafe_failure_signal_only_returns_fixed_category_without_leak(
    tmp_path, monkeypatch, stream_name, signal_text
):
    process = _FakeProcess(
        stdout=signal_text if stream_name == "stdout" else b"",
        stderr=signal_text if stream_name == "stderr" else b"",
        returncode=17,
    )
    _install_launcher(monkeypatch, process, final=None)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-unsafe-signal", route=_route())
    )

    assert result.failure_category == "unknown"
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    for marker in ("do-not-leak", "/private/local-output", "\\\\server", "file://"):
        assert marker not in serialized


def test_historical_generic_nonzero_response_round_trips_without_category():
    response = ReviewerInvocationResponse(
        status="failure",
        provider="openai-codex-desktop",
        model_ref="gpt-5.6-luna",
        error_code="REVIEWER_PROCESS_NONZERO_EXIT",
    )

    restored = ReviewerInvocationResponse.model_validate(
        response.model_dump(mode="python")
    )

    assert restored.error_code == "REVIEWER_PROCESS_NONZERO_EXIT"
    assert restored.failure_category is None


def test_event_stream_invalid_exposes_only_fixed_safe_failure_code(
    tmp_path, monkeypatch
):
    process = _FakeProcess(stdout=b'{"type":"future.event"}\n')
    _install_launcher(monkeypatch, process, final=b"{}")

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-event-invalid", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_EVENT_STREAM_INVALID"
    assert result.raw_response is None


def test_final_missing_keeps_existing_safe_missing_code(tmp_path, monkeypatch):
    process = _FakeProcess(stdout=b'{"type":"turn.completed"}\n')
    _install_launcher(monkeypatch, process, final=b"{}")

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-final-missing", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_RESPONSE_MISSING"
    assert result.raw_response is None


def test_final_schema_invalid_exposes_only_fixed_safe_failure_code(
    tmp_path, monkeypatch
):
    final = b'{"schema_version":"v1","findings":[]}'
    process = _FakeProcess(stdout=_event_stream(final.decode()))
    _install_launcher(monkeypatch, process, final=final)

    result = asyncio.run(
        CodexCliReviewerInvoker(
            _fake_binary(tmp_path), temp_root=tmp_path, environ={}
        ).invoke(_prompt(), run_id="run-final-schema", route=_route())
    )

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_RESPONSE_SCHEMA_INVALID"
    assert result.raw_response is None
