"""Focused tests for the live local Assurance Run client."""

import json

import httpx
import pytest

from assurance.entry import AssuranceReadbackError, AssuranceHttpClient


SUBJECT = "sha256:" + "1" * 64


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
