import asyncio
import json
from pathlib import Path
import re
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import web.assurance_runtime as runtime_module
import orchestration.adapters.deepseek as deepseek_module
from assurance.artifacts import ArtifactStore
from assurance.codex_cli_reviewer_invoker import CodexCliReviewerInvoker
from assurance.digests import (
    AcceptanceScopeDigestInput,
    SubjectDigestInput,
    compute_acceptance_scope_digest,
    compute_subject_digest,
)
from assurance.fixed_reviewer_invoker import FixedOpenAICompatibleReviewerInvoker
from assurance.reviewer_context import SafeReviewerContextBuilder
from assurance.run_service import AssuranceRunService
from assurance.snapshot import GitSnapshotCollector
from web.assurance_store import AssuranceWebRepository
from web.assurance_runtime import (
    AssuranceRuntime,
    AssuranceRuntimeStartupError,
    load_assurance_runtime_from_environment,
)
from web.assurance_remediation import AssuranceRemediationPreparationError
from orchestration.adapters import DeepSeekAdapter
from web.routes.assurance_runs import get_assurance_run_client
from web.server import create_app
from tests.test_assurance_run_service import _repository


def _config(tmp_path: Path, **updates):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    database_parent = tmp_path / "database"
    database_parent.mkdir(exist_ok=True)
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(exist_ok=True)
    config = {
        "schema_version": "v1",
        "workspace_root": str(workspace),
        "database_path": str(database_parent / "assurance.sqlite"),
        "artifact_store_root": str(artifact_root),
        "allowed_commands": [
            {
                "schema_version": "v1",
                "command_id": "check",
                "kind": "test",
                "argv": ["python", "-c", "print('ok')"],
                "cwd": ".",
                "timeout_seconds": 5.0,
                "max_output_bytes": 4096,
            }
        ],
        "orchestration_version": "golden.v1",
        "redaction_policy_version": "redaction.v0",
        "policy_version": "gate.v0",
        "rubric_version": "single_general.v0",
        "freshness_ttl_seconds": 300,
        "reviewer": {
            "provider": "deepseek",
            "model_ref": "deepseek-chat",
            "timeout_seconds": 5,
            "token_budget": 64,
            "routing_rule": "single_general.v0:fixed",
        },
    }
    config.update(updates)
    return config


def _write_config(tmp_path: Path, payload: str | dict) -> Path:
    path = tmp_path / "assurance-config.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, allow_nan=False), encoding="utf-8")
    return path


def _environment(
    config_path: Path,
    key: str = "test-secret",
    remediation_key: str | None = None,
):
    environment = {
        "CODEMESH_ASSURANCE_CONFIG": str(config_path),
        "CODEMESH_ASSURANCE_REVIEWER_API_KEY": key,
    }
    if remediation_key is not None:
        environment["CODEMESH_ASSURANCE_REMEDIATION_API_KEY"] = remediation_key
    return environment


def _remediation_config(provider: str = "qwen"):
    return {
        "provider": provider,
        "model_ref": "repair-model",
        "workspace_grant": {
            "allowed_paths": ["fix.py"],
            "max_files": 4,
            "max_bytes": 4096,
        },
        "policy": {
            "max_attempts": 1,
            "max_agent_iterations": 2,
            "max_validation_calls_per_attempt": 1,
            "total_wall_time_s": 10.0,
            "authoritative_check_id": "check",
        },
    }


class _FakeRemediationAdapter:
    name = "fake-remediation"

    def __init__(self, close_calls):
        self.last_usage = object()
        self._close_calls = close_calls

    async def complete(self, messages, system=""):
        return '{"action":"finalize","summary":"done"}'

    async def complete_stream(self, messages, system=""):
        if False:
            yield ""

    async def aclose(self):
        self._close_calls.append(True)


def test_missing_runtime_environment_is_disabled_without_initialization(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("CODEMESH_ASSURANCE_CONFIG", raising=False)
    monkeypatch.delenv("CODEMESH_ASSURANCE_REVIEWER_API_KEY", raising=False)
    calls = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("runtime must stay uninitialized")

    monkeypatch.setattr(runtime_module, "AssuranceWebRepository", _Forbidden)
    monkeypatch.setattr(
        runtime_module, "FixedOpenAICompatibleReviewerInvoker", _Forbidden
    )

    assert load_assurance_runtime_from_environment() is None
    assert calls == []
    assert not list(tmp_path.iterdir())


def test_strict_invalid_json_is_disabled_before_runtime_construction(tmp_path, monkeypatch):
    config = _config(tmp_path)
    encoded = json.dumps(config, separators=(",", ":"), allow_nan=False)
    cases = (
        encoded[:-1] + ',"schema_version":"v1"}',
        encoded.replace('"freshness_ttl_seconds":300', '"freshness_ttl_seconds":NaN'),
        json.dumps(config | {"api_key": "must-not-be-configured"}),
    )
    calls = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("invalid config must stay uninitialized")

    monkeypatch.setattr(runtime_module, "AssuranceWebRepository", _Forbidden)
    monkeypatch.setattr(
        runtime_module, "FixedOpenAICompatibleReviewerInvoker", _Forbidden
    )

    for index, raw in enumerate(cases):
        config_path = tmp_path / f"invalid-{index}.json"
        config_path.write_text(raw, encoding="utf-8")
        environment = _environment(config_path)
        assert load_assurance_runtime_from_environment(environment) is None

    assert calls == []


def test_path_boundary_violations_are_disabled_before_runtime_construction(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    workspace = Path(config["workspace_root"])
    variants = [
        config | {"database_path": str(workspace / "assurance.sqlite")},
        config | {"artifact_store_root": str(workspace / "artifacts")},
        config | {"database_path": str(tmp_path / "database-dir")},
        config | {"workspace_root": str(tmp_path / "missing" / "$ROOT")},
    ]
    (tmp_path / "database-dir").mkdir()
    calls = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("invalid path must stay uninitialized")

    monkeypatch.setattr(runtime_module, "AssuranceWebRepository", _Forbidden)
    monkeypatch.setattr(
        runtime_module, "FixedOpenAICompatibleReviewerInvoker", _Forbidden
    )

    for index, payload in enumerate(variants):
        config_path = _write_config(tmp_path, payload)
        assert (
            load_assurance_runtime_from_environment(_environment(config_path))
            is None
        ), index

    assert calls == []


def test_valid_config_builds_one_safe_runtime_and_closes_transport_once(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, _config(tmp_path))
    close_calls = []

    async def handler(request):
        raise AssertionError("C1 composition must not make a network request")

    class _CountingMockTransport(httpx.MockTransport):
        async def aclose(self):
            close_calls.append(True)
            await super().aclose()

    transport = _CountingMockTransport(handler)
    initialize_calls = []
    original_initialize = runtime_module.AssuranceWebRepository.initialize

    def initialize_once(repository):
        initialize_calls.append(repository)
        return original_initialize(repository)

    monkeypatch.setattr(
        runtime_module.AssuranceWebRepository, "initialize", initialize_once
    )

    runtime = load_assurance_runtime_from_environment(
        _environment(config_path), reviewer_transport=transport
    )
    assert isinstance(runtime, AssuranceRuntime)
    assert isinstance(runtime.repository, AssuranceWebRepository)
    assert isinstance(runtime.artifact_store, ArtifactStore)
    assert isinstance(runtime.context_builder, SafeReviewerContextBuilder)
    assert isinstance(runtime.reviewer_invoker, FixedOpenAICompatibleReviewerInvoker)
    assert isinstance(runtime.service, AssuranceRunService)
    assert initialize_calls == [runtime.repository]
    assert runtime.service._committer is runtime.repository
    assert runtime.service._artifact_store is runtime.artifact_store
    assert runtime.service._context_builder is runtime.context_builder
    assert runtime.service._reviewer_invoker is runtime.reviewer_invoker

    endpoint = runtime.reviewer_invoker._endpoint
    assert endpoint.base_url == "https://api.deepseek.com/v1"
    assert endpoint.route.provider == "deepseek"
    assert endpoint.route.model_ref == "deepseek-chat"
    assert endpoint.route.tool_grants == ()
    assert endpoint.api_key.get_secret_value() == "test-secret"
    assert close_calls == []

    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())
    assert close_calls == [True]


def test_v2_without_remediation_secret_starts_core_without_remediation_service(
    tmp_path,
):
    config = _config(tmp_path, schema_version="v2")
    config["remediation"] = _remediation_config()
    config_path = _write_config(tmp_path, config)
    factory_calls = []

    def factory(provider, model_ref, api_key):
        factory_calls.append((provider, model_ref, api_key))
        return _FakeRemediationAdapter([])

    runtime = load_assurance_runtime_from_environment(
        _environment(config_path), remediation_adapter_factory=factory
    )

    assert isinstance(runtime, AssuranceRuntime)
    assert runtime.remediation_service is None
    assert factory_calls == []


def test_codex_reviewer_composes_without_reviewer_api_key_and_does_not_receive_one(
    tmp_path, monkeypatch
):
    config = _config(tmp_path)
    config["reviewer"] = {
        "provider": "openai-codex-desktop",
        "model_ref": "gpt-5.6-luna",
        "timeout_seconds": 5,
        "token_budget": 4096,
        "routing_rule": "single_general.v0:fixed",
    }
    config_path = _write_config(tmp_path, config)
    calls = []

    class _FakeCodexInvoker:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        async def aclose(self):
            return None

    monkeypatch.setattr(runtime_module, "CodexCliReviewerInvoker", _FakeCodexInvoker)
    environment = {
        "CODEMESH_ASSURANCE_CONFIG": str(config_path),
        "CODEMESH_ASSURANCE_REVIEWER_API_KEY": "must-not-be-used",
    }

    runtime = load_assurance_runtime_from_environment(environment)
    assert isinstance(runtime, AssuranceRuntime)
    assert isinstance(runtime.reviewer_invoker, _FakeCodexInvoker)
    assert calls == [((), {})]
    assert not isinstance(runtime.reviewer_invoker, CodexCliReviewerInvoker)
    asyncio.run(runtime.aclose())


def test_deepseek_reviewer_without_api_key_stays_disabled(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, _config(tmp_path))
    environment = {"CODEMESH_ASSURANCE_CONFIG": str(config_path)}
    calls = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("missing DeepSeek key must disable before construction")

    monkeypatch.setattr(runtime_module, "AssuranceWebRepository", _Forbidden)
    monkeypatch.setattr(runtime_module, "FixedOpenAICompatibleReviewerInvoker", _Forbidden)

    assert load_assurance_runtime_from_environment(environment) is None
    assert calls == []


def test_codex_reviewer_without_remediation_key_keeps_remediation_disabled(
    tmp_path, monkeypatch
):
    config = _config(tmp_path, schema_version="v2")
    config["reviewer"] = {
        "provider": "openai-codex-desktop",
        "model_ref": "gpt-5.6-luna",
        "timeout_seconds": 5,
        "token_budget": 4096,
        "routing_rule": "single_general.v0:fixed",
    }
    config["remediation"] = _remediation_config()
    config_path = _write_config(tmp_path, config)
    calls = []

    class _FakeCodexInvoker:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

        async def aclose(self):
            return None

    monkeypatch.setattr(runtime_module, "CodexCliReviewerInvoker", _FakeCodexInvoker)
    runtime = load_assurance_runtime_from_environment(
        {"CODEMESH_ASSURANCE_CONFIG": str(config_path)}
    )

    assert isinstance(runtime, AssuranceRuntime)
    assert runtime.remediation_service is None
    assert calls == [((), {})]
    asyncio.run(runtime.aclose())


@pytest.mark.parametrize("provider", ["qwen", "deepseek"])
def test_v2_explicit_remediation_provider_uses_dedicated_secret_and_closes_once(
    tmp_path, provider
):
    config = _config(tmp_path, schema_version="v2")
    config["remediation"] = _remediation_config(provider)
    config_path = _write_config(tmp_path, config)
    factory_calls = []
    close_calls = []

    def factory(provider, model_ref, api_key):
        factory_calls.append((provider, model_ref, api_key))
        return _FakeRemediationAdapter(close_calls)

    runtime = load_assurance_runtime_from_environment(
        _environment(config_path, remediation_key="dedicated-secret"),
        remediation_adapter_factory=factory,
    )

    assert isinstance(runtime, AssuranceRuntime)
    assert runtime.remediation_service is not None
    assert factory_calls == [(provider, "repair-model", "dedicated-secret")]
    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())
    assert close_calls == [True]


def test_runtime_prepare_callback_classifies_source_composition_failures(
    tmp_path,
):
    config = _config(tmp_path, schema_version="v2")
    config["remediation"] = _remediation_config()
    config_path = _write_config(tmp_path, config)

    runtime = load_assurance_runtime_from_environment(
        _environment(config_path, remediation_key="dedicated-secret"),
        remediation_adapter_factory=lambda *_: _FakeRemediationAdapter([]),
    )
    assert runtime is not None
    assert runtime.remediation_service is not None

    try:
        with pytest.raises(AssuranceRemediationPreparationError) as raised:
            asyncio.run(
                runtime.remediation_service.prepare_callback(
                    object(),
                    context=object(),
                )
            )
    finally:
        asyncio.run(runtime.aclose())

    assert raised.value.stage == "SOURCE_RUNTIME"
    assert raised.value.reason_code == "TYPE"
    assert str(raised.value) == "assurance remediation preparation failed"


def test_deepseek_complete_defaults_and_json_mode_use_exact_request_kwargs(
    monkeypatch,
):
    calls = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                usage=None,
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"ok":true}')
                    )
                ],
            )

    class _FakeClient:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=_FakeCompletions())

    monkeypatch.setattr(deepseek_module, "AsyncOpenAI", _FakeClient)
    messages = [{"role": "user", "content": "return json"}]

    default = DeepSeekAdapter(api_key="secret", model="deepseek-chat")
    structured = DeepSeekAdapter(
        api_key="secret", model="deepseek-chat", json_mode=True
    )
    assert asyncio.run(default.complete(messages, system="system")) == '{"ok":true}'
    assert asyncio.run(structured.complete(messages, system="system")) == '{"ok":true}'

    expected_messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "return json"},
    ]
    assert calls == [
        {
            "model": "deepseek-chat",
            "messages": expected_messages,
            "temperature": 0.3,
        },
        {
            "model": "deepseek-chat",
            "messages": expected_messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "extra_body": {"thinking": {"type": "disabled"}},
        },
    ]


def test_default_remediation_factory_enables_json_mode_only_for_deepseek(
    monkeypatch,
):
    calls = []

    class _FakeAdapter:
        name = "fake"

        def __init__(self, **kwargs):
            self.last_usage = object()
            self.kwargs = kwargs
            calls.append(kwargs)

        async def complete(self, messages, system=""):
            return ""

        async def complete_stream(self, messages, system=""):
            if False:
                yield ""

    monkeypatch.setattr(runtime_module, "DeepSeekAdapter", _FakeAdapter)
    monkeypatch.setattr(runtime_module, "DashScopeAdapter", _FakeAdapter)

    deepseek = runtime_module._default_remediation_adapter_factory(
        "deepseek", "repair-model", "dedicated-secret"
    )
    qwen = runtime_module._default_remediation_adapter_factory(
        "qwen", "repair-model", "dedicated-secret"
    )

    assert deepseek.kwargs == {
        "api_key": "dedicated-secret",
        "model": "repair-model",
        "json_mode": True,
    }
    assert qwen.kwargs == {
        "api_key": "dedicated-secret",
        "model": "repair-model",
    }
    assert calls == [deepseek.kwargs, qwen.kwargs]


def test_remediation_source_root_accepts_posix_path_and_rejects_prefix_and_symlink(
    tmp_path,
):
    configured_root = tmp_path / "workspace"
    configured_root.mkdir()
    source_root = configured_root / "repository"
    source_root.mkdir()
    sibling_root = tmp_path / "workspace-sibling"
    sibling_root.mkdir()
    symlink_target = tmp_path / "real-repository"
    symlink_target.mkdir()
    symlink_root = configured_root / "linked-repository"
    symlink_root.symlink_to(symlink_target, target_is_directory=True)

    assert runtime_module._revalidate_remediation_root(
        source_root, configured_root
    ) == source_root.resolve()
    with pytest.raises(ValueError):
        runtime_module._revalidate_remediation_root(sibling_root, configured_root)
    with pytest.raises(ValueError):
        runtime_module._revalidate_remediation_root(symlink_root, configured_root)


def test_remediation_git_collector_rejects_synthetic_subject_digest(tmp_path):
    repository_path = _repository(tmp_path)
    collector = GitSnapshotCollector()
    task_digest = "sha256:" + "2" * 64
    artifact_store = ArtifactStore(tmp_path / "artifacts")
    collected = collector.collect(
        repository_path,
        repository_identity="example/service",
        base_ref="HEAD",
        task_digest=task_digest,
        policy_version="gate.v0",
        rubric_version="single_general.v0",
        artifact_store=artifact_store,
    )
    synthetic = SubjectDigestInput(
        repository=collected.snapshot.repository,
        base_revision=collected.snapshot.base_revision,
        head_revision=collected.snapshot.head_revision,
        normalized_diff_digest="sha256:" + "f" * 64,
        task_digest=task_digest,
        policy_version="gate.v0",
        rubric_version="single_general.v0",
    )
    assert compute_subject_digest(synthetic) != collected.snapshot.subject_digest

    scoped = runtime_module._RemediationGitCollector(
        collector, lambda: synthetic
    )
    with pytest.raises(ValueError, match="subject digest"):
        scoped.collect(
            repository_path,
            repository_identity="example/service",
            base_ref="HEAD",
            task_digest=task_digest,
            policy_version="gate.v0",
            rubric_version="single_general.v0",
            artifact_store=ArtifactStore(tmp_path / "scoped-artifacts"),
        )


def test_remediation_git_collector_preserves_v2_scope_identity(tmp_path):
    repository_path = _repository(tmp_path)
    collector = GitSnapshotCollector()
    task_digest = "sha256:" + "2" * 64
    scope_digest = compute_acceptance_scope_digest(
        AcceptanceScopeDigestInput(
            task_path="TASK.md",
            policy_paths=("POLICY.md",),
            adr_paths=(),
            runbook_paths=(),
        )
    )
    collected = collector.collect(
        repository_path,
        repository_identity="example/service",
        base_ref="HEAD",
        task_digest=task_digest,
        policy_version="gate.v0",
        rubric_version="single_general.v0",
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        acceptance_scope_digest=scope_digest,
    )
    subject = collector.build_subject_input(
        collected.snapshot,
        task_digest=task_digest,
        policy_version="gate.v0",
        rubric_version="single_general.v0",
        acceptance_scope_digest=scope_digest,
    )
    scoped = runtime_module._RemediationGitCollector(
        collector, lambda: subject
    )
    result = scoped.collect(
        repository_path,
        repository_identity="example/service",
        base_ref="HEAD",
        task_digest=task_digest,
        policy_version="gate.v0",
        rubric_version="single_general.v0",
        artifact_store=ArtifactStore(tmp_path / "scoped-artifacts"),
        acceptance_scope_digest=scope_digest,
    )
    assert result.snapshot.subject_digest == compute_subject_digest(subject)


@pytest.mark.parametrize("version", ("", "v3", None))
def test_remediation_scope_identity_unknown_version_fails_closed(version):
    with pytest.raises(ValueError, match="subject identity version"):
        runtime_module._scope_kwargs_for_subject_identity_version(
            version,
            "sha256:" + "a" * 64,
        )


def test_valid_config_failure_raises_fixed_sanitized_startup_error(tmp_path, monkeypatch):
    config_path = _write_config(tmp_path, _config(tmp_path))

    def fail_initialize(repository):
        raise RuntimeError("secret-token /private/internal/database.sqlite")

    monkeypatch.setattr(
        runtime_module.AssuranceWebRepository, "initialize", fail_initialize
    )

    with pytest.raises(AssuranceRuntimeStartupError) as error:
        load_assurance_runtime_from_environment(
            _environment(config_path),
            reviewer_transport=httpx.MockTransport(
                lambda request: httpx.Response(500)
            ),
        )

    assert str(error.value) == "assurance runtime startup failed"
    assert "secret-token" not in str(error.value)
    assert "/private/internal/database.sqlite" not in str(error.value)


def test_path_access_failure_is_startup_error_before_runtime_construction(
    tmp_path, monkeypatch
):
    config_path = _write_config(tmp_path, _config(tmp_path))
    calls = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("path access failure must stop construction")

    monkeypatch.setattr(runtime_module, "AssuranceWebRepository", _Forbidden)
    monkeypatch.setattr(
        runtime_module, "FixedOpenAICompatibleReviewerInvoker", _Forbidden
    )

    def inaccessible_paths(config):
        raise runtime_module._PathAccessError(
            "secret-token /private/internal/workspace"
        )

    monkeypatch.setattr(runtime_module, "_validate_paths", inaccessible_paths)

    with pytest.raises(AssuranceRuntimeStartupError) as error:
        load_assurance_runtime_from_environment(_environment(config_path))

    assert str(error.value) == "assurance runtime startup failed"
    assert "secret-token" not in str(error.value)
    assert "/private/internal/workspace" not in str(error.value)
    assert calls == []


def test_delayed_lifespan_loader_runs_fake_post_chain_and_closes_once(
    tmp_path,
):
    config = _config(tmp_path)
    workspace = Path(config["workspace_root"])
    repository_path = _repository(workspace)
    config_path = _write_config(tmp_path, config)
    requests = []
    close_calls = []

    async def handler(request):
        requests.append(request)
        payload = json.loads(request.content)
        prompt_text = payload["messages"][0]["content"]
        subject_digest = re.search(
            r"^Subject digest: (sha256:[0-9a-f]{64})$",
            prompt_text,
            re.MULTILINE,
        ).group(1)
        rubric_hash = re.search(
            r"^Rubric hash: (sha256:[0-9a-f]{64})$",
            prompt_text,
            re.MULTILINE,
        ).group(1)
        response_body = {
            "schema_version": "v1",
            "subject_digest": subject_digest,
            "rubric_hash": rubric_hash,
            "findings": [],
            "questions": [],
        }
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-chat",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                response_body, separators=(",", ":")
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    class _CountingMockTransport(httpx.MockTransport):
        async def aclose(self):
            close_calls.append(True)
            await super().aclose()

    transport = _CountingMockTransport(handler)
    loader_calls = []
    runtime_holder = {}

    def loader():
        loader_calls.append(True)
        runtime = load_assurance_runtime_from_environment(
            _environment(config_path), reviewer_transport=transport
        )
        runtime_holder["runtime"] = runtime
        return runtime

    app = create_app(assurance_runtime_loader=loader)
    app.dependency_overrides[get_assurance_run_client] = lambda: "127.0.0.1"
    body = {
        "repository_path": str(repository_path),
        "repository_identity": "example/service",
        "author": "author-agent",
        "base_ref": "HEAD",
        "task_path": "TASK.md",
        "policy_paths": ["POLICY.md"],
        "adr_paths": [],
        "runbook_paths": [],
        "command_ids": ["check"],
        "changed_lines_total": 1,
        "external_side_effects": "none_declared",
        "provider_boundary": "within_declared_boundary",
    }

    assert loader_calls == []
    with TestClient(app) as client:
        response = client.post(
            "/api/assurance/runs",
            headers={"Idempotency-Key": "run:lifespan"},
            json=body,
        )
        assert 200 <= response.status_code < 300
        assert response.json()["case_view"]["gate"] == "PASS"
        assert response.json()["case_view"]["policy_gate"]["status"] == "PASS"
        assert response.json()["case_view"]["freshness"]["status"] == "FRESH"
        assert response.json()["case_view"]["freshness"]["reason_code"] == "FRESHNESS_MATCH"

    assert loader_calls == [True]
    assert len(requests) == 1
    assert close_calls == [True]
    runtime = runtime_holder["runtime"]
    assert runtime.service._committer is runtime.repository
