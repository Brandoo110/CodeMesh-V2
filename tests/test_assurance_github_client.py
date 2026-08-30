"""Focused tests for the live GitHub Check publisher and readback."""

import base64
import io
import json
import zipfile
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
import yaml

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


def test_dual_transport_ref_endpoint_rejects_legacy_single_segment_ref():
    ref = (
        "refs/heads/codex/evidence-v2/"
        + PRODUCER_HEAD
        + "/"
        + TRANSPORT_HEAD
    )
    assert github_actions._ref_endpoint(ref) == (
        "/git/ref/heads/codex/evidence-v2/" + PRODUCER_HEAD + "/" + TRANSPORT_HEAD
    )
    with pytest.raises(github_actions.GitHubActionsError, match="temporary ref"):
        github_actions._ref_endpoint("refs/heads/codex/evidence/" + PRODUCER_HEAD)


def test_transport_ref_selection_ignores_legacy_and_other_transport_heads():
    current_ref = (
        "refs/heads/codex/evidence-v2/"
        + PRODUCER_HEAD
        + "/"
        + TRANSPORT_HEAD
    )
    other_ref = "refs/heads/codex/evidence-v2/" + PRODUCER_HEAD + "/" + "c" * 40
    refs = [
        ("d" * 40, "refs/heads/codex/evidence/" + PRODUCER_HEAD),
        ("e" * 40, other_ref),
        ("f" * 40, current_ref),
    ]

    assert github_actions._select_transport_ref(
        refs,
        transport_head=TRANSPORT_HEAD,
    ) == ("f" * 40, current_ref)


def test_transport_ref_selection_rejects_multiple_current_refs():
    refs = [
        ("d" * 40, "refs/heads/codex/evidence-v2/" + PRODUCER_HEAD + "/" + TRANSPORT_HEAD),
        ("e" * 40, "refs/heads/codex/evidence-v2/" + "c" * 40 + "/" + TRANSPORT_HEAD),
    ]

    with pytest.raises(github_actions.GitHubActionsError, match="exactly one"):
        github_actions._select_transport_ref(refs, transport_head=TRANSPORT_HEAD)


def test_same_dual_transport_ref_with_different_bytes_is_idempotency_conflict(tmp_path):
    _, bundle = _build_fixture_bundle(tmp_path)
    expected_ref_path = (
        "/git/ref/heads/codex/evidence-v2/" + PRODUCER_HEAD + "/" + TRANSPORT_HEAD
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/codemesh" + expected_ref_path:
            return httpx.Response(200, json={"object": {"sha": "c" * 40}})
        if request.url.path == "/repos/acme/codemesh/git/commits/" + "c" * 40:
            return httpx.Response(200, json={"tree": {"sha": "d" * 40}})
        if request.url.path == "/repos/acme/codemesh/git/trees/" + "d" * 40:
            return httpx.Response(
                200,
                json={"tree": [{"path": "bundle.json", "type": "blob", "sha": "e" * 40}]},
            )
        if request.url.path == "/repos/acme/codemesh/git/blobs/" + "e" * 40:
            return httpx.Response(
                200,
                json={"content": base64.b64encode(b"different").decode("ascii")},
            )
        raise AssertionError(f"unexpected API request: {request.method} {request.url}")

    api = github_actions._GitHubApi(
        token="ghs-test-token",
        owner="acme",
        repo="codemesh",
        api_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(github_actions.IdempotencyConflict, match="different bundle"):
            github_actions._ref_matches(api, ref=bundle.transport_ref, bundle=bundle)
    finally:
        api.close()


def test_cleanup_cannot_target_preserved_legacy_ref_when_new_ref_is_active():
    transport = github_actions.GitHubActionsTransport(
        token="ghs-test-token",
        repository="acme/codemesh",
        api_url="https://api.github.test",
        api_transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(
                AssertionError(f"unexpected cleanup API request: {request}")
            )
        ),
    )
    new_ref = "refs/heads/codex/evidence-v2/" + PRODUCER_HEAD + "/" + TRANSPORT_HEAD
    legacy_ref = "refs/heads/codex/evidence/" + PRODUCER_HEAD
    transport._active_ref = (new_ref, "c" * 40)
    try:
        with pytest.raises(github_actions.GitHubActionsError, match="cleanup target"):
            transport.cleanup(ref=legacy_ref, commit_sha="c" * 40)
    finally:
        transport.close()


def test_import_event_head_mismatch_happens_before_github_mutation(tmp_path, monkeypatch):
    _, bundle = _build_fixture_bundle(tmp_path)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "repository": {"full_name": "acme/codemesh"},
                "pull_request": {"head": {"sha": "c" * 40}},
            }
        ),
        encoding="utf-8",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError(f"unexpected API mutation: {request.method} {request.url}")

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    with pytest.raises(github_actions.GitHubActionsError, match="transport head"):
        github_actions.import_authoritative_bundle(
            bundle.bundle_bytes,
            output_dir=tmp_path / "output",
            repository_root=tmp_path / "repository",
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
    assert requests == []


def test_workflow_queries_only_current_evidence_v2_transport_ref():
    workflow = Path(".github/workflows/codemesh-assurance.yml").read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)

    assert "refs/heads/codex/evidence-v2/*/" in workflow
    assert "refs/heads/codex/evidence/*/" not in workflow
    assert document["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "checks": "write",
        "actions": "read",
    }


def test_import_checkout_mismatch_happens_before_github_mutation(tmp_path, monkeypatch):
    _, bundle = _build_fixture_bundle(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError(f"unexpected API mutation: {request.method} {request.url}")

    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setattr(
        github_actions,
        "_is_ancestor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            github_actions.GitHubActionsError("producer head is not an ancestor")
        ),
    )
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    with pytest.raises(github_actions.GitHubActionsError, match="not an ancestor"):
        github_actions.import_authoritative_bundle(
            bundle.bundle_bytes,
            output_dir=tmp_path / "output",
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
    assert requests == []


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
        if request.method == "GET" and "/git/ref/heads/codex/evidence-v2/" in request.url.path:
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


def test_actions_api_failure_exposes_redacted_structured_diagnostic():
    token = "ghs-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "message": f"Bad credentials Authorization: Bearer {token}",
                "errors": [{"code": "custom", "message": f"token={token}"}],
                "sensitive_full_response": "must-not-be-copied",
            },
        )

    api = github_actions._GitHubApi(
        token=token,
        owner="acme",
        repo="codemesh",
        api_url="https://api.github.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(github_actions.GitHubActionsError) as caught:
            api.request(
                "GET",
                "/repos/acme/codemesh/pulls/2",
                expected={200},
            )
    finally:
        api.close()

    diagnostic = caught.value.diagnostic()
    serialized = json.dumps(diagnostic, sort_keys=True)
    assert diagnostic["stage"] == "target_pr_readback"
    assert diagnostic["exception_class"] == "GitHubActionsError"
    assert diagnostic["http_status"] == 403
    assert diagnostic["github_error_code"] == "custom"
    assert token not in serialized
    assert "sensitive_full_response" not in serialized
    assert len(diagnostic["message"]) <= 240


def test_actions_cli_failure_prints_structured_redacted_diagnostic(monkeypatch, capsys):
    token = "ghs-secret-token"
    monkeypatch.setenv("GITHUB_TOKEN", token)

    def fail(**_kwargs):
        raise github_actions.GitHubActionsError(
            "Actions Check publisher failed",
            stage="check_publish",
            status_code=403,
            github_error_code="custom",
            safe_message=f"provider echoed token={token}",
        )

    monkeypatch.setattr(github_actions, "import_from_environment", fail)
    result = github_actions._cli_main(
        [
            "--bundle",
            "/tmp/bundle.json",
            "--output-dir",
            "/tmp/output",
            "--repository-root",
            "/tmp/repository",
            "--transport-head",
            "a" * 40,
            "--transport-ref-commit",
            "b" * 40,
            "--ci-run-id",
            "9001",
            "--ci-job-id",
            "assurance",
            "--run-attempt",
            "1",
        ]
    )

    assert result == 1
    diagnostic = json.loads(capsys.readouterr().err)
    assert diagnostic["stage"] == "check_publish"
    assert diagnostic["exception_class"] == "GitHubActionsError"
    assert diagnostic["http_status"] == 403
    assert diagnostic["github_error_code"] == "custom"
    assert token not in json.dumps(diagnostic, sort_keys=True)


def test_actions_publisher_failure_preserves_http_status_without_secret_echo():
    error = github_actions._stage_error(
        "check_publish",
        GitHubPublishError("GitHub API request failed (HTTP 403)"),
    )

    diagnostic = error.diagnostic()
    assert diagnostic["stage"] == "check_publish"
    assert diagnostic["exception_class"] == "GitHubPublishError"
    assert diagnostic["http_status"] == 403
    assert diagnostic["github_error_code"] is None
    assert diagnostic["message"] == "GitHub API request failed (HTTP 403)"
