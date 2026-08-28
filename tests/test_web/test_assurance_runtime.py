import asyncio
import json
from pathlib import Path
import re

import httpx
import pytest
from fastapi.testclient import TestClient

import web.assurance_runtime as runtime_module
from assurance.artifacts import ArtifactStore
from assurance.fixed_reviewer_invoker import FixedOpenAICompatibleReviewerInvoker
from assurance.reviewer_context import SafeReviewerContextBuilder
from assurance.run_service import AssuranceRunService
from web.assurance_store import AssuranceWebRepository
from web.assurance_runtime import (
    AssuranceRuntime,
    AssuranceRuntimeStartupError,
    load_assurance_runtime_from_environment,
)
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


def _environment(config_path: Path, key: str = "test-secret"):
    return {
        "CODEMESH_ASSURANCE_CONFIG": str(config_path),
        "CODEMESH_ASSURANCE_REVIEWER_API_KEY": key,
    }


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

    assert loader_calls == [True]
    assert len(requests) == 1
    assert close_calls == [True]
    runtime = runtime_holder["runtime"]
    assert runtime.service._committer is runtime.repository
