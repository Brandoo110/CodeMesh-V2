"""Focused tests for the live GitHub Check publisher and readback."""

import json

import httpx
import pytest

from assurance.integrations.github_client import (
    GitHubCheckPublisher,
    GitHubPublishError,
)
from assurance.integrations.github import canonical_passport_digest


SUBJECT = "sha256:" + "1" * 64


def _passport() -> dict[str, object]:
    return {
        "schema": "codemesh.assurance.passport.v1",
        "case_id": "case-017",
        "subject_digest": SUBJECT,
        "state": "ACCEPTED",
        "gate": "ACCEPTED",
        "revision": 7,
        "evidence": [],
        "findings": [],
        "policy_decisions": [],
        "human_decisions": [],
    }


def test_publish_check_posts_and_authoritatively_reads_exact_sha():
    head_sha = "a" * 40
    requests: list[httpx.Request] = []
    passport_digest = canonical_passport_digest(_passport())
    check = {
        "id": 123,
        "name": "CodeMesh Change Assurance",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/acme/widget/runs/123",
        "output": {
            "summary": (
                "codemesh-case:case-017;subject:"
                + SUBJECT
                + ";passport:"
                + passport_digest
                + "\nPublished by CodeMesh Change Assurance.\n"
                + "State: ACCEPTED\nGate: ACCEPTED"
            )
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if request.method == "POST":
            assert request.url.path == "/repos/acme/widget/check-runs"
            body = json.loads(request.content)
            assert body["head_sha"] == head_sha
            assert "case-017" in body["output"]["summary"]
            assert "offline payload, not published" not in body["output"]["summary"]
            return httpx.Response(201, json=check)
        assert request.method == "GET"
        assert request.url.path == "/repos/acme/widget/check-runs/123"
        return httpx.Response(200, json=check)

    publisher = GitHubCheckPublisher(
        token="ghs-secret-token",
        transport=httpx.MockTransport(handler),
    )
    result = publisher.publish(
        _passport(), owner="acme", repo="widget", head_sha=head_sha
    )

    assert result.check_id == 123
    assert result.reused is False
    assert result.head_sha == head_sha
    assert result.conclusion == "success"
    assert result.check_url == check["html_url"]
    assert [request.method for request in requests] == ["GET", "POST", "GET"]


def test_publish_reuses_matching_existing_check_without_posting_again():
    head_sha = "b" * 40
    requests: list[httpx.Request] = []
    passport_digest = canonical_passport_digest(_passport())
    check = {
        "id": 456,
        "name": "CodeMesh Change Assurance",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success",
        "output": {
            "summary": (
                "codemesh-case:case-017;subject:"
                + SUBJECT
                + ";passport:"
                + passport_digest
                + "\nPublished by CodeMesh Change Assurance.\n"
                + "State: ACCEPTED\nGate: ACCEPTED"
            )
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": [check]})
        assert request.url.path.endswith("/check-runs/456")
        return httpx.Response(200, json=check)

    publisher = GitHubCheckPublisher(
        token="ghs-secret-token",
        transport=httpx.MockTransport(handler),
    )
    result = publisher.publish(
        _passport(), owner="acme", repo="widget", head_sha=head_sha
    )

    assert result.check_id == 456
    assert result.reused is True
    assert [request.method for request in requests] == ["GET", "GET"]


def test_live_freshness_timestamp_does_not_break_replay_binding():
    first = _passport()
    second = dict(first)
    first["freshness"] = {"status": "FRESH", "checked_at": "2026-08-30T10:00:00Z"}
    second["freshness"] = {"status": "FRESH", "checked_at": "2026-08-30T10:00:01Z"}
    assert canonical_passport_digest(first) != canonical_passport_digest(second)

    # The publisher's replay marker is intentionally based on stable facts;
    # the wall-clock freshness probe is not a new Passport revision.
    from assurance.integrations.github_client import _stable_passport_digest

    assert _stable_passport_digest(first) == _stable_passport_digest(second)


def test_publish_rejects_mismatched_github_readback_without_echoing_token():
    head_sha = "c" * 40
    check = {
        "id": 789,
        "name": "CodeMesh Change Assurance",
        "head_sha": "d" * 40,
        "status": "completed",
        "conclusion": "success",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/check-runs"):
            return httpx.Response(200, json={"check_runs": []})
        if request.method == "POST":
            return httpx.Response(201, json={"id": 789})
        return httpx.Response(200, json=check)

    publisher = GitHubCheckPublisher(
        token="ghs-secret-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(GitHubPublishError) as caught:
        publisher.publish(
            _passport(), owner="acme", repo="widget", head_sha=head_sha
        )
    assert "ghs-secret-token" not in str(caught.value)
