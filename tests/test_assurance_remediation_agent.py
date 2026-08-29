from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from assurance.remediation import AgentAttemptResult
from assurance.remediation_agent import (
    RemediationAgent,
    RemediationAgentBudgets,
    RemediationAgentBudgetError,
    RemediationAgentProtocolError,
)
from assurance.remediation_validation import (
    BudgetedValidationExecutor,
    ScopedValidationTools,
)
from assurance.remediation_workspace import (
    IsolatedWorkspace,
    WorkspaceGrant,
    WorkspaceViolation,
)


def _request(*, check_id: str = "authoritative", max_iterations: int = 8) -> object:
    return SimpleNamespace(
        remediation_id="remediation-1",
        old_case_id="case-1",
        policy=SimpleNamespace(
            authoritative_check_id=check_id,
            max_agent_iterations=max_iterations,
        ),
    )


def _selected_finding(*, claim: str = "ordinary finding claim") -> object:
    return SimpleNamespace(
        finding_id="finding-1",
        claim=claim,
        severity="high",
        basis="deterministic",
        reviewer_role="architecture",
        status="open",
        evidence_refs=("evidence-1",),
        subject_digest="sha256:" + "1" * 64,
        model_ref="reviewer-v1",
    )


class _Adapter:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict[str, str]], str]] = []

    async def complete(
        self, messages: list[dict[str, str]], system: str = ""
    ) -> object:
        self.calls.append(([dict(message) for message in messages], system))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _Tools:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.read_result: object = "old"
        self.list_result: object = ("fix.py",)
        self.write_result: object = "written"
        self.edit_result: object = "edited"
        self.validation_result: object = '{"status":"passed"}'
        self.scoped = ScopedValidationTools(
            _TestWorkspace(self),
            BudgetedValidationExecutor(_TestValidationExecutor(self), 2),
        )

    def list_files(self) -> object:
        self.calls.append(("list", ()))
        return self.list_result

    def read_file(self, path: str) -> object:
        self.calls.append(("read", (path,)))
        return self.read_result

    def edit_file(self, path: str, old: str, new: str) -> object:
        self.calls.append(("replace", (path, old, new)))
        return self.edit_result

    def write_file(self, path: str, content: str) -> object:
        self.calls.append(("write", (path, content)))
        return self.write_result

    async def run_validation(self, check_id: str) -> object:
        self.calls.append(("validate", (check_id,)))
        return self.validation_result


class _TestWorkspace:
    def __init__(self, owner: _Tools) -> None:
        self.owner = owner

    def public_paths(self) -> object:
        return self.owner.list_files()

    def read_text(self, path: str) -> object:
        return self.owner.read_file(path)

    def write_text(self, path: str, content: str) -> object:
        return self.owner.write_file(path, content)


class _TestValidationResult:
    def __init__(self, value: object) -> None:
        self.value = value

    def to_json(self) -> str:
        if isinstance(self.value, str):
            return self.value
        return json.dumps(self.value, ensure_ascii=False, allow_nan=False)


class _TestValidationExecutor:
    def __init__(self, owner: _Tools) -> None:
        self.owner = owner

    async def validate(self, check_id: str, *, actor: str) -> object:
        value = self.owner.run_validation(check_id)
        if inspect.isawaitable(value):
            value = await value
        return _TestValidationResult(value)


def _workspace(paths: tuple[str, ...] = ("fix.py",)) -> object:
    return SimpleNamespace(public_paths=lambda: paths)


def _repair(
    adapter: object,
    tools: object,
    *,
    workspace: object | None = None,
    request: object | None = None,
    feedback: object = "failed",
    max_iterations: int = 8,
    budgets: RemediationAgentBudgets | None = None,
    selected_finding: object | None = None,
) -> AgentAttemptResult:
    agent = RemediationAgent(adapter, budgets=budgets or RemediationAgentBudgets())
    if isinstance(tools, _Tools):
        tools_for_agent = tools.scoped
    else:
        tools_for_agent = tools
    repair_kwargs: dict[str, object] = {
        "request": request or _request(max_iterations=max_iterations),
        "finding_id": "finding-1",
        "attempt": 1,
        "workspace": workspace or _workspace(),
        "tools": tools_for_agent,
        "validation_feedback": feedback,
        "max_iterations": max_iterations,
    }
    if selected_finding is not None:
        repair_kwargs["selected_finding"] = selected_finding
    return asyncio.run(agent.repair(**repair_kwargs))  # type: ignore[arg-type]


def test_selected_finding_prompt_contains_only_safe_context_fields() -> None:
    adapter = _Adapter(['{"action":"finalize","summary":"done"}'])
    finding = _selected_finding()

    _repair(adapter, _Tools(), selected_finding=finding, max_iterations=1)

    prompt = adapter.calls[0][0][0]["content"]
    payload = json.loads(prompt.split("\n", 1)[1])
    assert "untrusted data" in prompt
    assert payload["selected_finding"] == {
        "finding_id": "finding-1",
        "claim": "ordinary finding claim",
        "severity": "high",
        "basis": "deterministic",
        "reviewer_role": "architecture",
        "status": "open",
    }
    for excluded in ("evidence_refs", "subject_digest", "model_ref"):
        assert excluded not in prompt


def test_selected_finding_claim_is_redacted_in_initial_prompt() -> None:
    adapter = _Adapter(['{"action":"finalize","summary":"done"}'])
    finding = _selected_finding(
        claim="Authorization: Bearer secret /Users/junjieli/private/finding.txt"
    )

    _repair(adapter, _Tools(), selected_finding=finding, max_iterations=1)

    prompt = adapter.calls[0][0][0]["content"]
    assert "Authorization: Bearer secret" not in prompt
    assert "/Users/junjieli/private/finding.txt" not in prompt


def test_structured_loop_executes_one_action_per_model_call() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.responses = [
                '{"action":"list"}',
                '{"action":"read","path":"fix.py"}',
                '{"action":"replace","path":"fix.py","old_text":"old","new_text":"new"}',
                '{"action":"write","path":"fix.py","content":"newer"}',
                '{"action":"run_validation","check_id":"authoritative"}',
                '{"action":"finalize","summary":"done"}',
            ]
            self.calls: list[tuple[list[dict[str, str]], str]] = []

        async def complete(
            self, messages: list[dict[str, str]], system: str = ""
        ) -> str:
            self.calls.append(([dict(message) for message in messages], system))
            return self.responses.pop(0)

    adapter = Adapter()
    tools = _Tools()
    workspace = _workspace()
    request = _request(max_iterations=6)

    result = _repair(
        adapter,
        tools,
        workspace=workspace,
        request=request,
        max_iterations=6,
    )

    assert type(result) is AgentAttemptResult
    assert result.iterations == 6
    assert [call[0] for call in tools.calls] == [
        "list",
        "read",
        "read",
        "write",
        "write",
        "validate",
    ]
    assert "fix.py" in adapter.calls[1][0][-1]["content"]
    assert "old" in adapter.calls[2][0][-1]["content"]
    assert "edited" in adapter.calls[3][0][-1]["content"]
    assert "wrote" in adapter.calls[4][0][-1]["content"]
    assert "untrusted" in adapter.calls[1][1].lower()


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        "```json\n{\"action\":\"list\"}\n```",
        '{"action":"delete","path":"fix.py"}',
        '{"action":"list","extra":true}',
        '{"action":"read","path":123}',
        '[{"action":"list"}]',
    ],
)
def test_invalid_response_is_rejected_before_any_tool(
    response: str,
) -> None:
    adapter = _Adapter([response])
    tools = _Tools()

    with pytest.raises(RemediationAgentProtocolError):
        _repair(adapter, tools, max_iterations=2)

    assert len(adapter.calls) == 1
    assert tools.calls == []


@pytest.mark.parametrize(
    ("responses", "request_value", "expected_type"),
    (
        (["not json"], None, "RemediationAgentResponseError"),
        (
            ['{"action":"delete","path":"fix.py"}'],
            None,
            "RemediationAgentActionSchemaError",
        ),
        (
            [json.dumps({"action": "read", "path": "../fix.py"})],
            None,
            "RemediationAgentPathError",
        ),
        (
            ['{"action":"replace","path":"fix.py","old_text":"","new_text":"new"}'],
            None,
            "RemediationAgentActionPolicyError",
        ),
        (
            [
                '{"action":"write","path":"fix.py","content":"new"}',
                '{"action":"write","path":"fix.py","content":"new"}',
            ],
            None,
            "RemediationAgentRepeatedActionError",
        ),
        (
            ['{"action":"run_validation","check_id":"authoritative"}'],
            _request(check_id=None),
            "RemediationAgentInternalProtocolError",
        ),
    ),
)
def test_protocol_failures_use_specific_safe_subclasses(
    responses: list[object],
    request_value: object | None,
    expected_type: str,
) -> None:
    adapter = _Adapter(responses)
    tools = _Tools()

    with pytest.raises(RemediationAgentProtocolError) as raised:
        _repair(
            adapter,
            tools,
            request=request_value,
            max_iterations=len(responses),
        )

    assert type(raised.value).__name__ == expected_type


def test_model_adapter_surface_is_complete_only() -> None:
    class CompleteOnlyAdapter:
        def __init__(self) -> None:
            self.responses = ['{"action":"finalize","summary":"done"}']
            self.complete_calls = 0

        def __getattribute__(self, name: str) -> object:
            if name in {"client", "model", "stream", "complete_stream", "api_key"}:
                raise AssertionError(f"forbidden adapter attribute: {name}")
            return object.__getattribute__(self, name)

        async def complete(
            self, messages: list[dict[str, str]], system: str = ""
        ) -> str:
            self.complete_calls += 1
            return self.responses.pop(0)

    adapter = CompleteOnlyAdapter()
    result = _repair(adapter, _Tools(), max_iterations=1)

    assert result.iterations == 1
    assert adapter.complete_calls == 1


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/fix.py",
        "../fix.py",
        "src/../fix.py",
        "https://example.invalid/fix.py",
        "file:fix.py",
        "C:/fix.py",
        r"C:\\fix.py",
        "fix\x00.py",
        ".codemesh_eval/secret",
        "src/.codemesh_eval/secret",
        "./fix.py",
        "src//fix.py",
        "src\\fix.py",
    ],
)
def test_noncanonical_or_private_path_is_rejected_before_tool(path: str) -> None:
    adapter = _Adapter([json.dumps({"action": "read", "path": path})])
    tools = _Tools()

    with pytest.raises(RemediationAgentProtocolError):
        _repair(adapter, tools, max_iterations=1)

    assert tools.calls == []


def test_symlink_escape_is_left_to_public_workspace_tool_and_fails_closed(
    tmp_path: object,
) -> None:
    # ``PublicWorkspaceView`` remains the final allowed-path/symlink authority;
    # the agent only validates canonical spelling before invoking its tool.
    seed = tmp_path / "seed"  # type: ignore[operator]
    seed.mkdir()
    outside = tmp_path / "outside.txt"  # type: ignore[operator]
    outside.write_text("outside", encoding="utf-8")
    (seed / "link.txt").symlink_to(outside)
    grant = WorkspaceGrant(allowed_paths=("link.txt",))

    with IsolatedWorkspace.prepare(seed, grant) as isolated:
        executor = BudgetedValidationExecutor(
            SimpleNamespace(validate=lambda *_args, **_kwargs: None), 1
        )
        tools = ScopedValidationTools(isolated.public_view(), executor)
        adapter = _Adapter(['{"action":"read","path":"link.txt"}'])

        with pytest.raises(WorkspaceViolation):
            _repair(adapter, tools, workspace=isolated.public_view(), max_iterations=1)


def test_old_text_non_unique_and_workspace_quota_errors_propagate() -> None:
    adapter = _Adapter(
        ['{"action":"replace","path":"fix.py","old_text":"old","new_text":"new"}']
    )
    tools = _Tools()
    tools.read_result = "old old"
    with pytest.raises(WorkspaceViolation, match="old_string"):
        _repair(adapter, tools, max_iterations=1)
    assert [name for name, _ in tools.calls] == ["read"]

    class QuotaTools(_Tools):
        def write_file(self, path: str, content: str) -> object:
            self.calls.append(("write", (path, content)))
            raise WorkspaceViolation("workspace quota exceeded")

    quota_adapter = _Adapter(['{"action":"write","path":"fix.py","content":"new"}'])
    quota_tools = QuotaTools()
    with pytest.raises(WorkspaceViolation, match="quota"):
        _repair(quota_adapter, quota_tools, max_iterations=1)


def test_unauthorized_validation_does_not_call_validation_tool() -> None:
    adapter = _Adapter(['{"action":"run_validation","check_id":"other"}'])
    tools = _Tools()

    with pytest.raises(RemediationAgentProtocolError):
        _repair(adapter, tools, max_iterations=1)

    assert tools.calls == []


def test_validation_budget_and_duplicate_action_both_fail_closed() -> None:
    # The repeated identical action is rejected before a third validation can
    # reach the tool surface.
    adapter = _Adapter(
        [
            '{"action":"run_validation","check_id":"authoritative"}',
            '{"action":"list"}',
            '{"action":"run_validation","check_id":"authoritative"}',
        ]
    )
    tools = _Tools()

    with pytest.raises(RemediationAgentProtocolError, match="repeated"):
        _repair(adapter, tools, max_iterations=3)

    assert [name for name, _ in tools.calls] == ["validate", "list"]


def test_oversized_response_and_content_are_rejected_without_tool() -> None:
    budgets = RemediationAgentBudgets(
        max_response_bytes=64,
        max_observation_bytes=128,
        max_context_bytes=4096,
        max_action_bytes=128,
        max_content_bytes=8,
        max_summary_bytes=16,
    )
    huge_response = "{" + (" " * 100) + "}"
    response_adapter = _Adapter([huge_response])
    response_tools = _Tools()
    with pytest.raises(RemediationAgentBudgetError):
        _repair(response_adapter, response_tools, max_iterations=1, budgets=budgets)
    assert response_tools.calls == []

    content_adapter = _Adapter(
        ['{"action":"write","path":"fix.py","content":"123456789"}']
    )
    content_tools = _Tools()
    with pytest.raises(RemediationAgentBudgetError):
        _repair(content_adapter, content_tools, max_iterations=1, budgets=budgets)
    assert content_tools.calls == []


def test_observation_and_next_prompt_are_clipped_and_redacted() -> None:
    budgets = RemediationAgentBudgets(
        max_response_bytes=4096,
        max_observation_bytes=96,
        max_context_bytes=4096,
        max_action_bytes=512,
        max_content_bytes=128,
        max_summary_bytes=32,
    )
    adapter = _Adapter(
        [
            '{"action":"read","path":"fix.py"}',
            '{"action":"finalize","summary":"done"}',
        ]
    )
    tools = _Tools()
    tools.read_result = (
        "Authorization: Bearer super-secret-token "
        "/Users/junjieli/private/file "
        + "x" * 1000
    )

    result = _repair(adapter, tools, max_iterations=2, budgets=budgets)

    assert result.iterations == 2
    next_prompt = adapter.calls[1][0]
    serialized = json.dumps(next_prompt, ensure_ascii=False)
    assert "super-secret-token" not in serialized
    assert "/Users/junjieli/private/file" not in serialized
    assert sum(len(item["content"].encode()) for item in next_prompt) < 4096
    assert any("TRUNCATED" in item["content"] for item in next_prompt)


def test_oversized_initial_context_fails_before_model_call() -> None:
    budgets = RemediationAgentBudgets(
        max_response_bytes=1024,
        max_observation_bytes=128,
        max_context_bytes=256,
        max_action_bytes=512,
        max_content_bytes=128,
        max_summary_bytes=32,
    )
    adapter = _Adapter(['{"action":"finalize","summary":"done"}'])
    tools = _Tools()
    paths = tuple(f"file-{index}-" + "x" * 100 for index in range(8))

    with pytest.raises(RemediationAgentBudgetError, match="context"):
        _repair(
            adapter,
            tools,
            workspace=_workspace(paths),
            max_iterations=1,
            budgets=budgets,
        )
    assert adapter.calls == []


def test_repeated_action_and_max_iterations_do_not_loop_forever() -> None:
    repeated_adapter = _Adapter(
        [
            '{"action":"write","path":"fix.py","content":"new"}',
            '{"action":"write","path":"fix.py","content":"new"}',
            '{"action":"write","path":"fix.py","content":"new"}',
        ]
    )
    repeated_tools = _Tools()
    with pytest.raises(RemediationAgentProtocolError, match="repeated"):
        _repair(repeated_adapter, repeated_tools, max_iterations=3)
    assert len(repeated_adapter.calls) == 2
    assert [name for name, _ in repeated_tools.calls] == ["write"]

    limited_adapter = _Adapter(
        [
            '{"action":"list"}',
            '{"action":"read","path":"fix.py"}',
            '{"action":"list"}',
        ]
    )
    limited_tools = _Tools()
    with pytest.raises(RemediationAgentBudgetError, match="iteration"):
        _repair(limited_adapter, limited_tools, max_iterations=2)
    assert len(limited_adapter.calls) == 2
    assert [name for name, _ in limited_tools.calls] == ["list", "read"]


@pytest.mark.parametrize(
    ("action", "tool_name"),
    (
        ('{"action":"list"}', "list"),
        ('{"action":"read","path":"fix.py"}', "read"),
    ),
)
def test_repeated_observation_actions_can_finalize_within_iteration_cap(
    action: str,
    tool_name: str,
) -> None:
    adapter = _Adapter(
        [action, action, '{"action":"finalize","summary":"done"}']
    )
    tools = _Tools()

    result = _repair(adapter, tools, max_iterations=3)

    assert result.iterations == 3
    assert [name for name, _ in tools.calls] == [tool_name, tool_name]


def test_finalize_without_mutation_returns_safe_truncated_result() -> None:
    adapter = _Adapter(
        ['{"action":"finalize","summary":"Authorization: Bearer hidden"}']
    )
    tools = _Tools()
    result = _repair(
        adapter,
        tools,
        max_iterations=1,
        budgets=RemediationAgentBudgets(max_summary_bytes=12),
    )

    assert result.iterations == 1
    assert "hidden" not in result.summary
    assert tools.calls == []


def test_adapter_exception_is_not_wrapped_or_prompted() -> None:
    error = RuntimeError("Authorization: Bearer super-secret")
    adapter = _Adapter([error])
    tools = _Tools()

    with pytest.raises(RuntimeError) as raised:
        _repair(adapter, tools, max_iterations=1)

    assert raised.value is error
    assert tools.calls == []


def test_dangerous_duck_tool_is_rejected_at_repair_entry() -> None:
    class DangerousDuck:
        def list_files(self) -> tuple[str, ...]:
            raise AssertionError("duck tool must not be called")

        def read_file(self, path: str) -> str:
            raise AssertionError("duck tool must not be called")

        def edit_file(self, path: str, old: str, new: str) -> str:
            raise AssertionError("duck tool must not be called")

        def write_file(self, path: str, content: str) -> str:
            raise AssertionError("duck tool must not be called")

        async def run_validation(self, check_id: str) -> str:
            raise AssertionError("duck tool must not be called")

    adapter = _Adapter(['{"action":"finalize","summary":"done"}'])
    with pytest.raises(TypeError, match="ScopedValidationTools"):
        asyncio.run(
            RemediationAgent(adapter).repair(
                request=_request(max_iterations=1),
                finding_id="finding-1",
                attempt=1,
                workspace=_workspace(),
                tools=DangerousDuck(),
                validation_feedback="failed",
                max_iterations=1,
            )
        )
    assert adapter.calls == []


def test_replace_empty_old_text_is_rejected_before_tool() -> None:
    adapter = _Adapter(
        ['{"action":"replace","path":"fix.py","old_text":"","new_text":"new"}']
    )
    tools = _Tools()
    with pytest.raises(RemediationAgentProtocolError, match="old_text"):
        _repair(adapter, tools, max_iterations=1)
    assert tools.calls == []


@pytest.mark.parametrize("policy_limit", [True, 0, -1, "3"])
def test_iteration_limits_are_strict_and_reject_model_copy_tampering(
    policy_limit: object,
) -> None:
    adapter = _Adapter(['{"action":"finalize","summary":"done"}'])
    tools = _Tools()
    request = _request(max_iterations=3)
    request.policy.max_agent_iterations = policy_limit

    with pytest.raises(RemediationAgentBudgetError):
        _repair(adapter, tools, request=request, max_iterations=3)
    assert adapter.calls == []
    assert tools.calls == []


def test_iteration_limit_uses_strict_minimum_of_controller_and_policy() -> None:
    adapter = _Adapter(
        [
            '{"action":"list"}',
            '{"action":"read","path":"fix.py"}',
            '{"action":"write","path":"fix.py","content":"new"}',
            '{"action":"finalize","summary":"too late"}',
        ]
    )
    tools = _Tools()
    request = _request(max_iterations=3)

    with pytest.raises(RemediationAgentBudgetError, match="iteration"):
        _repair(adapter, tools, request=request, max_iterations=4)
    assert len(adapter.calls) == 3
    assert [name for name, _ in tools.calls] == ["list", "read", "write"]


@pytest.mark.parametrize("path", ["%2e%2e/fix.py", "src/%2fsecret", "src/%5Csecret"])
def test_percent_encoded_path_escape_is_rejected_before_tool(path: str) -> None:
    adapter = _Adapter([json.dumps({"action": "read", "path": path})])
    tools = _Tools()

    with pytest.raises(RemediationAgentProtocolError):
        _repair(adapter, tools, max_iterations=1)
    assert tools.calls == []


def test_nonfinite_structured_observation_is_stably_redacted() -> None:
    adapter = _Adapter(
        [
            '{"action":"read","path":"fix.py"}',
            '{"action":"finalize","summary":"done"}',
        ]
    )
    tools = _Tools()
    tools.read_result = {
        "score": float("nan"),
        "upper": float("inf"),
        "lower": float("-inf"),
    }

    _repair(adapter, tools, max_iterations=2)
    next_prompt = json.dumps(adapter.calls[1][0], ensure_ascii=False)
    assert "NaN" not in next_prompt
    assert "Infinity" not in next_prompt
    assert "-Infinity" not in next_prompt
