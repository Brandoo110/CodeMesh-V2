"""Focused tests for the live local Assurance Run client."""

import json
import hashlib

import httpx
import pytest

from assurance.entry import (
    AssuranceArtifactReadback,
    AssuranceReadbackError,
    AssuranceResponseError,
    AssuranceTransportError,
    AssuranceHttpClient,
)


SUBJECT = "sha256:" + "1" * 64


def _receipt_payload() -> dict[str, object]:
    return {
        "schema_version": "v1",
        "receipt_id": "receipt-001",
        "run_id": "run-001",
        "subject_digest": SUBJECT,
        "steps": [
            {
                "sequence": 0,
                "planned_role": "operability",
                "actual_role": "operability",
                "model_ref": "fixture-model",
                "provider": "fixture-provider",
                "tool_grants": [],
                "routing_rule": "fixture",
                "fallback_reason": None,
                "token_budget": 0,
                "timeout_seconds": 1,
                "result": "success",
                "schema_status": "valid",
            }
        ],
        "overall_result": "success",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "started_at": "2026-08-30T10:00:00Z",
        "completed_at": "2026-08-30T10:00:01Z",
    }


def _case_view(*, checked_at: str) -> dict[str, object]:
    return {
        "schema_version": "v1",
        "case_id": "case-001",
        "subject_digest": SUBJECT,
        "revision": 1,
        "digest_freshness": True,
        "policy_gate": "PENDING",
        "acceptance_state": "DRAFT",
        "release_state": {"status": "UNAVAILABLE"},
        "allowed_actions": [],
        "freshness": {
            "status": "FRESH",
            "reason_code": "SOURCE_MATCH",
            "checked_at": checked_at,
        },
    }


def test_run_posts_with_idempotency_and_reads_authoritative_case():
    post_view = _case_view(checked_at="2026-08-30T10:00:00Z")
    get_view = _case_view(checked_at="2026-08-30T10:00:01Z")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/api/assurance/runs"
            assert request.headers["Idempotency-Key"] == "replay-key"
            assert json.loads(request.content)["command_ids"] == ["diff-check"]
            return httpx.Response(
                201,
                json={
                    "schema_version": "v1",
                    "run_id": "run-001",
                    "request_digest": "sha256:" + "2" * 64,
                    "cached": False,
                    "case_id": "case-001",
                    "case_view": post_view,
                },
            )
        assert request.method == "GET"
        assert request.url.path == "/api/assurance/changes/case-001"
        return httpx.Response(200, json=get_view)

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )
    result = client.run_and_readback(
        {"command_ids": ["diff-check"]}, idempotency_key="replay-key"
    )

    assert result.run_id == "run-001"
    assert result.case_id == "case-001"
    assert result.cached is False
    assert result.case_view == get_view
    assert [request.method for request in requests] == ["POST", "GET"]


def test_run_post_uses_dedicated_default_timeout():
    timeout_extensions: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        timeout_extensions.append(request.extensions["timeout"])
        return httpx.Response(
            201,
            json={
                "schema_version": "v1",
                "run_id": "run-001",
                "request_digest": "sha256:" + "2" * 64,
                "cached": False,
                "case_id": "case-001",
                "case_view": {},
            },
        )

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )

    client.run({"command_ids": ["diff-check"]}, idempotency_key="timeout-key")

    assert timeout_extensions == [
        {"connect": 240.0, "read": 240.0, "write": 240.0, "pool": 240.0}
    ]


def test_run_create_timeout_override_is_used_for_post_only():
    timeout_extensions: list[tuple[str, dict[str, float]]] = []
    post_view = _case_view(checked_at="2026-08-30T10:00:00Z")
    get_view = _case_view(checked_at="2026-08-30T10:00:01Z")

    def handler(request: httpx.Request) -> httpx.Response:
        timeout_extensions.append((request.method, request.extensions["timeout"]))
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "schema_version": "v1",
                    "run_id": "run-001",
                    "request_digest": "sha256:" + "2" * 64,
                    "cached": False,
                    "case_id": "case-001",
                    "case_view": post_view,
                },
            )
        return httpx.Response(200, json=get_view)

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        run_create_timeout=12.5,
        transport=httpx.MockTransport(handler),
    )

    client.run_and_readback({"command_ids": ["diff-check"]}, idempotency_key="timeout-key")

    assert timeout_extensions == [
        (
            "POST",
            {"connect": 12.5, "read": 12.5, "write": 12.5, "pool": 12.5},
        ),
        (
            "GET",
            {"connect": 20.0, "read": 20.0, "write": 20.0, "pool": 20.0},
        ),
    ]


@pytest.mark.parametrize(
    "invalid_timeout",
    [True, False, 0, -1, float("nan"), float("inf"), 600.0001, 601],
)
def test_run_create_timeout_rejects_nonfinite_or_out_of_bounds_values(invalid_timeout):
    with pytest.raises(ValueError, match="run_create_timeout"):
        AssuranceHttpClient(
            "http://127.0.0.1:8010",
            run_create_timeout=invalid_timeout,
            transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
        )


def test_run_create_timeout_accepts_upper_bound():
    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        run_create_timeout=600,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                201,
                json={
                    "schema_version": "v1",
                    "run_id": "run-001",
                    "request_digest": "sha256:" + "2" * 64,
                    "cached": False,
                    "case_id": "case-001",
                    "case_view": {},
                },
            )
        ),
    )

    assert client.run({}, idempotency_key="timeout-key").run_id == "run-001"


def test_run_timeout_is_fail_closed_without_readback_or_retry():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("run create timed out", request=request)

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AssuranceTransportError):
        client.run_and_readback({}, idempotency_key="timeout-key")

    assert [request.method for request in requests] == ["POST"]


def test_run_fails_closed_when_post_projection_differs_from_readback():
    post_view = _case_view(checked_at="2026-08-30T10:00:00Z")
    get_view = _case_view(checked_at="2026-08-30T10:00:01Z")
    get_view["subject_digest"] = "sha256:" + "3" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "schema_version": "v1",
                    "run_id": "run-001",
                    "request_digest": "sha256:" + "2" * 64,
                    "cached": False,
                    "case_id": "case-001",
                    "case_view": post_view,
                },
            )
        return httpx.Response(200, json=get_view)

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AssuranceReadbackError):
        client.run_and_readback({"command_ids": ["diff-check"]}, idempotency_key="k")


def test_http_error_does_not_echo_server_detail_or_request_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "code": "ASSURANCE_RUN_INVALID",
                "message": "secret-token=do-not-print /private/repo",
            },
        )

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception) as caught:
        client.run_and_readback(
            {"task_path": "private prompt", "command_ids": ["diff-check"]},
            idempotency_key="k",
        )
    assert "secret-token" not in str(caught.value)
    assert "/private/repo" not in str(caught.value)


def test_passport_readback_must_bind_to_requested_case():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "schema": "codemesh.assurance.passport.v1",
                "case_id": "another-case",
                "subject_digest": SUBJECT,
                "state": "ACCEPTED",
                "gate": "ACCEPTED",
                "revision": 1,
            },
        )

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(Exception):
        client.get_passport("case-001")


def test_readonly_export_getters_validate_paths_and_artifact_headers():
    artifact = b"authoritative artifact\n"
    digest = "sha256:" + hashlib.sha256(artifact).hexdigest()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/receipt"):
            return httpx.Response(
                200,
                json=_receipt_payload(),
            )
        if request.url.path.endswith("/passport"):
            if request.url.query == b"format=markdown":
                return httpx.Response(
                    200,
                    content=b"# Passport\n",
                    headers={"Content-Type": "text/markdown"},
                )
            return httpx.Response(
                200,
                json={
                    "schema": "codemesh.assurance.passport.v1",
                    "case_id": "case-001",
                    "subject_digest": SUBJECT,
                    "evidence": [],
                },
            )
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(
                200,
                json={
                    "schema_version": "v1",
                    "case_id": "case-001",
                    "evidence_id": "ev_001",
                    "evidence_kind": "command_batch",
                    "artifacts": [
                        {
                            "schema_version": "v1",
                            "digest": digest,
                            "kind": "stdout",
                            "label": "fixture:stdout",
                            "byte_size": len(artifact),
                            "media_type": "text/plain",
                            "integrity_status": "SHA-256 integrity verified",
                            "role": "stdout",
                            "path": None,
                            "command_id": "fixture",
                            "stream": "stdout",
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            content=artifact,
            headers={
                "Content-Type": "text/plain",
                "X-Artifact-Digest": digest,
                "X-Artifact-Size": str(len(artifact)),
            },
        )

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )

    receipt = client.get_receipt("case-001")
    passport = client.get_passport("case-001")
    markdown = client.get_passport_markdown("case-001")
    index = client.list_artifacts("case-001", "ev_001")
    readback = client.read_artifact("case-001", "ev_001", digest)

    assert receipt["run_id"] == "run-001"
    assert passport["case_id"] == "case-001"
    assert markdown == "# Passport\n"
    assert index["evidence_id"] == "ev_001"
    assert isinstance(readback, AssuranceArtifactReadback)
    assert readback.data == artifact
    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "GET",
        "GET",
        "GET",
    ]


def test_read_artifact_rejects_mismatched_digest_header():
    digest = "sha256:" + "4" * 64

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"artifact",
            headers={
                "Content-Type": "text/plain",
                "X-Artifact-Digest": "sha256:" + "5" * 64,
                "X-Artifact-Size": "8",
            },
        )

    client = AssuranceHttpClient(
        "http://127.0.0.1:8010",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AssuranceResponseError, match="digest"):
        client.read_artifact("case-001", "ev-001", digest)
