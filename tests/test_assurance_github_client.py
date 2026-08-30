"""Focused tests for the live GitHub Check publisher and readback."""

import io
import json
import zipfile
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from assurance.integrations.github_client import (
    GitHubCheckPublisher,
    GitHubPublishError,
)
from assurance.integrations.github import canonical_passport_digest
import assurance.integrations.github_actions as github_actions
from assurance.evidence_bundle import build_evidence_bundle

from .test_assurance_evidence_bundle import (
    CASE_ID,
    PRODUCER_HEAD,
    SUBJECT,
    TRANSPORT_HEAD,
    _fixture,
)


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


def _build_fixture_bundle(tmp_path: Path):
    root, _, _ = _fixture(tmp_path)
    return root, build_evidence_bundle(
        root,
        case_id=CASE_ID,
        repository="acme/codemesh",
        pr_number=2,
        producer_head=PRODUCER_HEAD,
        transport_head=TRANSPORT_HEAD,
    )


def _import_fixture(tmp_path: Path, monkeypatch):
    root, bundle = _build_fixture_bundle(tmp_path)
    output_dir = tmp_path / "workbench"
    repository_root = tmp_path / "repo"
    repository_root.mkdir()
    calls: list[str] = []
    check = {
        "id": 321,
        "name": github_actions._IMPORTED_CHECK_NAME,
        "head_sha": PRODUCER_HEAD,
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/acme/codemesh/runs/321",
    }
    check["output"] = {
        "summary": github_actions._imported_summary(
            bundle=github_actions.verify_evidence_bundle(bundle.bundle_bytes).document,
            ci_run_id="9001",
            check_marker=github_actions._check_marker(
                github_actions.verify_evidence_bundle(bundle.bundle_bytes).document
            ),
        )
    }

    class FakePublisher:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def publish(self, passport, *, owner, repo, head_sha):
            calls.append("check_publish")
            assert passport["case_id"] == CASE_ID
            assert owner == "acme"
            assert repo == "codemesh"
            assert head_sha == PRODUCER_HEAD
            return SimpleNamespace(
                passport_digest=bundle.passport_digest,
                check_id=321,
                conclusion="success",
            )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/git/ref/heads/codex/evidence/" in request.url.path:
            return httpx.Response(200, json={"object": {"sha": "c" * 40}})
        if request.method == "GET" and request.url.path.endswith("/pulls/2"):
            return httpx.Response(
                200,
                json={
                    "number": 2,
                    "state": "open",
                    "head": {
                        "sha": PRODUCER_HEAD,
                        "repo": {"full_name": "acme/codemesh"},
                    },
                },
            )
        if request.url.path.endswith("/check-runs/321"):
            return httpx.Response(200, json=check)
        if request.method == "PATCH" and request.url.path.endswith("/check-runs/321"):
            return httpx.Response(200, json=check)
        raise AssertionError(f"unexpected API request: {request.method} {request.url}")

    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setattr(github_actions, "GitHubCheckPublisher", FakePublisher)
    monkeypatch.setattr(github_actions, "_is_ancestor", lambda *_args, **_kwargs: None)
    result = github_actions.import_authoritative_bundle(
        bundle.bundle_bytes,
        output_dir=output_dir,
        repository_root=repository_root,
        token="ghs-test-token",
        repository="acme/codemesh",
        transport_head=TRANSPORT_HEAD,
        transport_ref_commit="c" * 40,
        ci_run_id="9001",
        ci_job_id="assurance",
        run_attempt=1,
        api_url="https://api.github.test",
        api_transport=httpx.MockTransport(handler),
    )
    assert calls == ["check_publish"]
    return bundle, result, output_dir


def _zip_directory(root: Path, *, extra: tuple[str, bytes] | None = None, mutate: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            name = path.relative_to(root).as_posix()
            archive.writestr(name, b"tampered" if name == mutate else path.read_bytes())
        if extra is not None:
            archive.writestr(*extra)
    return buffer.getvalue()


def test_actions_import_materializes_ci_only_workbench_and_zero_counters(tmp_path, monkeypatch):
    bundle, result, output_dir = _import_fixture(tmp_path, monkeypatch)

    receipt = json.loads((output_dir / "bundle-receipt.json").read_text(encoding="utf-8"))
    publication = json.loads((output_dir / "publication.json").read_text(encoding="utf-8"))
    workbench = json.loads((output_dir / "workbench.json").read_text(encoding="utf-8"))
    assert result.check_id == 321
    assert workbench["origin"] == "local_authoritative_bundle"
    assert workbench["case_id"] == bundle.case_id
    assert workbench["run_id"] == bundle.run_id
    assert workbench["subject_digest"] == bundle.subject_digest
    assert workbench["producer_head"] == bundle.producer_head
    assert workbench["transport_head"] == bundle.transport_head
    assert workbench["passport_digest"] == bundle.passport_digest
    for value in (receipt, publication):
        assert value["case_writes"] == 0
        assert value["provider_calls"] == 0
        assert value["assurance_run_invocations"] == 0


def test_actions_artifact_readback_rejects_tampered_or_extra_files(tmp_path, monkeypatch):
    bundle, result, output_dir = _import_fixture(tmp_path, monkeypatch)
    transport = github_actions.GitHubActionsTransport(
        token="ghs-test-token",
        repository="acme/codemesh",
        api_url="https://api.github.test",
        api_transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    workflow = github_actions._WorkflowFacts(
        run_id=result.ci_run_id,
        job_id=result.ci_job_id,
        run_attempt=result.run_attempt,
        artifact_id=result.artifact_id,
        artifact_zip=b"",
    )
    try:
        workflow = SimpleNamespace(**workflow.__dict__)
        workflow.artifact_zip = _zip_directory(
            output_dir,
            mutate="evidence/artifacts/" + next(
                item["digest"].removeprefix("sha256:")
                for item in github_actions.verify_evidence_bundle(bundle.bundle_bytes).document["objects"]
                if item["role"] == "evidence_artifact"
            ),
        )
        with pytest.raises(github_actions.GitHubActionsError, match="did not match|closure"):
            transport._verify_artifact(
                bundle=bundle,
                workflow=workflow,
                transport_ref_commit="c" * 40,
            )

        workflow.artifact_zip = _zip_directory(output_dir, extra=("evidence/artifacts/extra", b"x"))
        with pytest.raises(github_actions.GitHubActionsError, match="closure|extra"):
            transport._verify_artifact(
                bundle=bundle,
                workflow=workflow,
                transport_ref_commit="c" * 40,
            )
    finally:
        transport.close()


def test_actions_rerun_waits_for_same_run_id_with_new_attempt(monkeypatch):
    transport = github_actions.GitHubActionsTransport(
        token="ghs-test-token",
        repository="acme/codemesh",
        poll_interval_seconds=0.001,
        poll_timeout_seconds=0.2,
    )
    transport_branch = transport.transport_branch
    states = iter(
        (
            {
                "id": 77,
                "run_attempt": 1,
                "head_sha": TRANSPORT_HEAD,
                "head_branch": transport_branch,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "failure",
            },
            {
                "id": 77,
                "run_attempt": 2,
                "head_sha": TRANSPORT_HEAD,
                "head_branch": transport_branch,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "id": 77,
                "run_attempt": 2,
                "head_sha": TRANSPORT_HEAD,
                "head_branch": transport_branch,
                "event": "pull_request",
                "status": "completed",
                "conclusion": "success",
            },
        )
    )
    monkeypatch.setattr(transport, "_run", lambda _run_id: next(states))
    monkeypatch.setattr(transport, "_workflow_runs", lambda **_kwargs: [])
    try:
        result = transport._wait_for_run(
            initial_id=77,
            initial_attempt=1,
            transport_head=TRANSPORT_HEAD,
        )
    finally:
        transport.close()
    assert result["id"] == 77
    assert result["run_attempt"] == 2
    assert result["conclusion"] == "success"
