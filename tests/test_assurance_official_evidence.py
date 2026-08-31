"""Focused contracts for the read-only official P-C artifact adapter."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import assurance.official_evidence as official_evidence_module

from assurance.artifacts import ArtifactStore
from assurance.official_evidence import (
    OfficialEvidenceError,
    OfficialEvidenceImporter,
)


SUBJECT = "sha256:" + "1" * 64
REPOSITORY = "example/handover"
RUN_ID = "123"
JOB_ID = 456
ARTIFACT_ID = 789
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "CodeMesh Test")
    (root / ".github/workflows").mkdir(parents=True)
    (root / ".github/workflows/p-c-handover.yml").write_text(
        "name: P-C Handover Experience\n", encoding="utf-8"
    )
    (root / "frontend").mkdir()
    (root / "frontend/package.json").write_text(
        '{"name":"handover"}\n', encoding="utf-8"
    )
    (root / "frontend/pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n", encoding="utf-8"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "official-fixture")
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return root, head


def _git_blob(root: Path, head: str, path: str) -> bytes:
    return subprocess.run(
        ("git", "show", f"{head}:{path}"),
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def _source(root: Path, head: str, path: str) -> dict[str, object]:
    data = _git_blob(root, head, path)
    return {"path": path, "digest": _digest(data), "byte_size": len(data)}


def _report(
    root: Path,
    head: str,
    *,
    kind: str,
    result_name: str,
    result_bytes: bytes,
    subject_digest: str | None = SUBJECT,
    status: str = "success",
    conclusion: str = "success",
) -> tuple[bytes, bytes]:
    if kind == "dependency_audit":
        source_paths = [
            _source(root, head, "frontend/package.json"),
            _source(root, head, "frontend/pnpm-lock.yaml"),
        ]
        checks: list[dict[str, str]] = []
        audit_command: str | None = "pnpm audit --prod --audit-level=high --json"
    else:
        source_paths = [
            _source(root, head, ".github/workflows/p-c-handover.yml"),
            _source(root, head, "frontend/package.json"),
            _source(root, head, "frontend/pnpm-lock.yaml"),
        ]
        checks = [
            {"name": name, "status": "success", "conclusion": "success"}
            for name in (
                "checkout",
                "install",
                "focused_checks",
                "build",
                "browser_walkthrough",
            )
        ]
        audit_command = None
    report = {
        "schema_version": "v1",
        "kind": kind,
        "repository_identity": REPOSITORY,
        "head_revision": head,
        "subject_digest": subject_digest,
        "producer": "collector." + kind,
        "source_paths": source_paths,
        "workflow_name": "P-C Handover Experience",
        "workflow_path": ".github/workflows/p-c-handover.yml",
        "event": "workflow_dispatch",
        "pull_request_number": 7,
        "workflow_run_id": RUN_ID,
        "workflow_run_attempt": 1,
        "job_id": "handover",
        "job_name": "handover",
        "status": status,
        "conclusion": conclusion,
        "result_path": result_name,
        "result_digest": _digest(result_bytes),
        "result_byte_size": len(result_bytes),
        "checks": checks,
        "evidence_mode": "official",
    }
    if audit_command is not None:
        report["audit_command"] = audit_command
    report_bytes = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return report_bytes, result_bytes


def _zip_payload(root: Path, head: str) -> bytes:
    dependency_report, dependency_result = _report(
        root,
        head,
        kind="dependency_audit",
        result_name="dependency-audit-result.json",
        result_bytes=b'{"advisories":[]}\n',
    )
    ci_report, ci_result = _report(
        root,
        head,
        kind="ci_iac_validation",
        result_name="ci-iac-result.json",
        result_bytes=b'{"validation":"success"}\n',
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (
            ("dependency_audit.json", dependency_report),
            ("ci_iac_validation.json", ci_report),
            ("dependency-audit-result.json", dependency_result),
            ("ci-iac-result.json", ci_result),
        ):
            archive.writestr(name, data)
    return output.getvalue()


def _transport(
    head: str,
    zip_bytes: bytes,
    *,
    mutate_run=None,
    mutate_job=None,
    mutate_artifact=None,
    mutate_pr=None,
    mutate_zip=None,
):
    requests: list[httpx.Request] = []
    artifact_digest = _digest(zip_bytes)
    run = {
        "id": int(RUN_ID),
        "name": "P-C Handover Experience",
        "path": ".github/workflows/p-c-handover.yml",
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "candidate",
        "head_sha": head,
        "run_attempt": 1,
        "repository": {"full_name": REPOSITORY},
        "head_repository": {"full_name": REPOSITORY},
        "pull_requests": [],
    }
    job = {
        "id": JOB_ID,
        "name": "handover",
        "run_id": int(RUN_ID),
        "status": "completed",
        "conclusion": "success",
    }
    artifact = {
        "id": ARTIFACT_ID,
        "name": "p-c-official-validation-123",
        "expired": False,
        "size_in_bytes": len(zip_bytes),
        "digest": artifact_digest,
        "workflow_run": {"id": int(RUN_ID)},
    }
    pull_request = {
        "number": 7,
        "state": "open",
        "base": {
            "ref": "main",
            "sha": "c" * 40,
            "repo": {"full_name": REPOSITORY},
        },
        "head": {
            "ref": "candidate",
            "sha": head,
            "repo": {"full_name": REPOSITORY},
        },
    }
    if mutate_run is not None:
        mutate_run(run)
    if mutate_job is not None:
        mutate_job(job)
    if mutate_artifact is not None:
        mutate_artifact(artifact)
    if mutate_pr is not None:
        mutate_pr(pull_request)
    payload_zip = zip_bytes if mutate_zip is None else mutate_zip(zip_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith(f"/actions/runs/{RUN_ID}"):
            return httpx.Response(200, json=run, request=request)
        if path.endswith(f"/actions/runs/{RUN_ID}/jobs"):
            return httpx.Response(200, json={"jobs": [job]}, request=request)
        if path.endswith(f"/actions/runs/{RUN_ID}/artifacts"):
            return httpx.Response(200, json={"artifacts": [artifact]}, request=request)
        if path.endswith(f"/actions/artifacts/{ARTIFACT_ID}/zip"):
            return httpx.Response(
                200,
                content=payload_zip,
                headers={"content-length": str(len(payload_zip))},
                request=request,
            )
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json=pull_request, request=request)
        return httpx.Response(404, request=request)

    return httpx.MockTransport(handler), requests


def _importer(
    tmp_path: Path,
    root: Path,
    head: str,
    transport: httpx.BaseTransport,
) -> OfficialEvidenceImporter:
    return OfficialEvidenceImporter(
        workspace_root=tmp_path,
        repository_path=root,
        repository_identity=REPOSITORY,
        head_revision=head,
        subject_digest=SUBJECT,
        artifact_store=ArtifactStore(tmp_path / "artifact-store"),
        collected_at=NOW,
        github_token="test-token",
        github_api_url="https://api.github.test",
        transport=transport,
    )


def test_import_reads_one_exact_run_and_binds_two_observed_receipts(tmp_path):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, requests = _transport(head, zip_bytes)
    importer = _importer(tmp_path, root, head, transport)

    imports = importer.import_run(RUN_ID)

    assert [item.receipt.kind for item in imports] == [
        "dependency_audit",
        "ci_iac_validation",
    ]
    assert len(requests) == 5
    assert [request.method for request in requests] == ["GET"] * 5
    assert requests[0].url.path.endswith("/actions/runs/123")
    assert requests[1].url.path.endswith("/actions/runs/123/jobs")
    assert requests[2].url.path.endswith("/actions/runs/123/artifacts")
    assert requests[3].url.path.endswith("/actions/artifacts/789/zip")
    assert requests[4].url.path.endswith("/pulls/7")
    for imported in imports:
        assert imported.evidence.subject_digest == SUBJECT
        assert imported.evidence.trust_level == "observed"
        assert imported.receipt.subject_digest == SUBJECT
        assert imported.receipt.workflow_run_id == RUN_ID
        assert imported.receipt.job_id == str(JOB_ID)
        assert imported.receipt.artifact_id == str(ARTIFACT_ID)
        assert imported.receipt.artifact_digest == _digest(zip_bytes)
        assert importer._artifact_store.get_bytes(imported.receipt_digest) == imported.receipt_bytes
        assert importer._artifact_store.get_bytes(imported.remote_zip_digest) == zip_bytes
        importer.verify_import(imported)


@pytest.mark.parametrize(
    ("mutator", "expected_requests", "expected_reason"),
    (
        (lambda run: run.update({"head_sha": "b" * 40}), 1, "lineage_mismatch"),
        (lambda run: run.update({"head_branch": "other"}), 5, "lineage_mismatch"),
        (lambda run: run.update({"event": "pull_request"}), 1, "lineage_mismatch"),
        (lambda run: run.update({"conclusion": "failure"}), 1, "lineage_mismatch"),
        (
            lambda run: run.update({"repository": {"full_name": "other/repo"}}),
            1,
            "lineage_mismatch",
        ),
    ),
)
def test_run_provenance_mismatch_fails_closed_before_artifact_read(
    tmp_path, mutator, expected_requests, expected_reason
):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, requests = _transport(head, zip_bytes, mutate_run=mutator)
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)
    assert len(requests) == expected_requests
    assert caught.value.reason_code == expected_reason


def test_run_provenance_mismatch_reports_lineage_reason(tmp_path):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, _requests = _transport(
        head,
        zip_bytes,
        mutate_run=lambda run: run.update({"head_sha": "b" * 40}),
    )
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "lineage_mismatch"
    assert str(caught.value) == "official evidence import failed"


@pytest.mark.parametrize("status_code", (401, 403))
def test_credential_response_reports_credential_reason(tmp_path, status_code):
    root, head = _repository(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    importer = _importer(tmp_path, root, head, httpx.MockTransport(handler))

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "credential_missing_or_invalid"
    assert "secret" not in str(caught.value).lower()


@pytest.mark.parametrize("status_code", (429, 500, 503))
def test_github_http_failure_reports_transport_reason(tmp_path, status_code):
    root, head = _repository(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    importer = _importer(tmp_path, root, head, httpx.MockTransport(handler))

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "github_transport"


@pytest.mark.parametrize("transport_error", ("timeout", "eof"))
def test_github_transport_failure_reports_transport_reason(
    tmp_path, transport_error
):
    root, head = _repository(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if transport_error == "timeout":
            raise httpx.ReadTimeout("secret timeout", request=request)
        raise httpx.RemoteProtocolError("secret EOF", request=request)

    importer = _importer(tmp_path, root, head, httpx.MockTransport(handler))

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "github_transport"
    assert str(caught.value) == "official evidence import failed"


def test_missing_token_reports_credential_reason_without_network(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    root, head = _repository(tmp_path)
    transport, requests = _transport(head, b"unused")
    importer = _importer(tmp_path, root, head, transport)
    importer._github_token = None

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "credential_missing_or_invalid"
    assert requests == []


def test_artifact_structure_failure_reports_structure_reason(tmp_path):
    root, head = _repository(tmp_path)
    valid = _zip_payload(root, head)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(valid)) as source, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            data = b"not-json" if item.filename == "dependency-audit-result.json" else source.read(item)
            target.writestr(item.filename, data)
        target.writestr("unexpected.txt", b"extra")
    malformed = output.getvalue()
    transport, _requests = _transport(
        head, malformed, mutate_zip=lambda _zip: malformed
    )
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "artifact_structure_invalid"


def test_artifact_digest_or_size_failure_reports_digest_reason(tmp_path):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, _requests = _transport(
        head,
        zip_bytes,
        mutate_artifact=lambda artifact: artifact.update({"digest": _digest(b"tampered")}),
    )
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "digest_or_size_mismatch"


def test_unexpected_import_failure_is_unknown_and_sanitized(tmp_path, monkeypatch):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, _requests = _transport(head, zip_bytes)
    importer = _importer(tmp_path, root, head, transport)

    def fail(_data):
        raise RuntimeError("secret /private/report.zip")

    monkeypatch.setattr(official_evidence_module, "_zip_files", fail)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "unknown"
    assert "secret" not in str(caught.value)
    assert "/private/report.zip" not in str(caught.value)


def test_first_failure_reason_wins_for_mixed_run_and_artifact_errors(tmp_path):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, _requests = _transport(
        head,
        zip_bytes,
        mutate_run=lambda run: run.update({"head_sha": "b" * 40}),
        mutate_artifact=lambda artifact: artifact.update({"digest": _digest(b"tampered")}),
    )
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)

    assert caught.value.reason_code == "lineage_mismatch"


def test_expired_or_drifted_artifact_metadata_fails_closed(tmp_path):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    for mutate, expected_requests, expected_reason in (
        (lambda artifact: artifact.update({"expired": True}), 3, "lineage_mismatch"),
        (
            lambda artifact: artifact.update({"digest": _digest(b"different")}),
            4,
            "digest_or_size_mismatch",
        ),
        (
            lambda artifact: artifact.update({"workflow_run": {"id": 999}}),
            3,
            "lineage_mismatch",
        ),
    ):
        transport, requests = _transport(head, zip_bytes, mutate_artifact=mutate)
        importer = _importer(tmp_path, root, head, transport)
        with pytest.raises(OfficialEvidenceError) as caught:
            importer.import_run(RUN_ID)
        assert len(requests) == expected_requests
        assert caught.value.reason_code == expected_reason


def test_zip_closure_and_malformed_result_fail_closed(tmp_path):
    root, head = _repository(tmp_path)
    valid = _zip_payload(root, head)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(valid)) as source, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            data = b"not-json" if item.filename == "dependency-audit-result.json" else source.read(item)
            target.writestr(item.filename, data)
        target.writestr("unexpected.txt", b"extra")
    malformed = output.getvalue()
    # Make metadata match the replacement bytes so closure, not a claimed
    # digest, is the first failing boundary.
    transport, requests = _transport(
        head,
        malformed,
        mutate_zip=lambda _zip: malformed,
    )
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)
    assert len(requests) == 4
    assert caught.value.reason_code == "artifact_structure_invalid"


def test_subject_claim_is_not_allowed_to_override_runtime_subject(tmp_path):
    root, head = _repository(tmp_path)
    valid = _zip_payload(root, head)
    wrong_subject = "sha256:" + "2" * 64
    dependency_report, dependency_result = _report(
        root,
        head,
        kind="dependency_audit",
        result_name="dependency-audit-result.json",
        result_bytes=b'{"advisories":[]}\n',
        subject_digest=wrong_subject,
    )
    _ci_report, ci_result = _report(
        root,
        head,
        kind="ci_iac_validation",
        result_name="ci-iac-result.json",
        result_bytes=b'{"validation":"success"}\n',
    )
    ci_report, _ = _report(
        root,
        head,
        kind="ci_iac_validation",
        result_name="ci-iac-result.json",
        result_bytes=ci_result,
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (
            ("dependency_audit.json", dependency_report),
            ("ci_iac_validation.json", ci_report),
            ("dependency-audit-result.json", dependency_result),
            ("ci-iac-result.json", ci_result),
        ):
            archive.writestr(name, data)
    replacement = output.getvalue()
    transport, requests = _transport(
        head,
        replacement,
        mutate_zip=lambda _zip: replacement,
    )
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)
    assert len(requests) == 5
    assert caught.value.reason_code == "lineage_mismatch"


@pytest.mark.parametrize("missing_subject", (False, True))
def test_report_subject_is_required_and_bound_to_runtime_subject(
    tmp_path, missing_subject
):
    root, head = _repository(tmp_path)
    dependency_report, dependency_result = _report(
        root,
        head,
        kind="dependency_audit",
        result_name="dependency-audit-result.json",
        result_bytes=b'{"advisories":[]}\n',
        subject_digest=None,
    )
    if missing_subject:
        report_value = json.loads(dependency_report)
        report_value.pop("subject_digest")
        dependency_report = json.dumps(
            report_value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ci_report, ci_result = _report(
        root,
        head,
        kind="ci_iac_validation",
        result_name="ci-iac-result.json",
        result_bytes=b'{"validation":"success"}\n',
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in (
            ("dependency_audit.json", dependency_report),
            ("ci_iac_validation.json", ci_report),
            ("dependency-audit-result.json", dependency_result),
            ("ci-iac-result.json", ci_result),
        ):
            archive.writestr(name, data)
    replacement = output.getvalue()
    transport, requests = _transport(head, replacement, mutate_zip=lambda _zip: replacement)
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError):
        importer.import_run(RUN_ID)
    assert len(requests) == 4


@pytest.mark.parametrize(
    "mutator",
    (
        lambda pr: pr.update({"state": "closed"}),
        lambda pr: pr["head"].update({"sha": "b" * 40}),
        lambda pr: pr["head"]["repo"].update({"full_name": "other/repo"}),
        lambda pr: pr["base"]["repo"].update({"full_name": "other/repo"}),
    ),
)
def test_dispatch_pr_readback_mismatch_fails_closed(tmp_path, mutator):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, requests = _transport(head, zip_bytes, mutate_pr=mutator)
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises(OfficialEvidenceError) as caught:
        importer.import_run(RUN_ID)
    assert len(requests) == 5
    assert caught.value.reason_code == "lineage_mismatch"


def test_workflow_dispatch_inputs_are_the_only_official_report_source():
    workflow = Path(".github/workflows/p-c-handover.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "target_sha:" in workflow
    assert "pull_request_number:" in workflow
    assert "subject_digest:" in workflow
    assert '"subject_digest": None' not in workflow
    assert "if: success() && github.event_name == 'workflow_dispatch'" in workflow


def test_production_importer_uses_fixed_github_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_API_URL", "https://attacker.invalid")
    root, head = _repository(tmp_path)
    importer = OfficialEvidenceImporter(
        workspace_root=tmp_path,
        repository_path=root,
        repository_identity=REPOSITORY,
        head_revision=head,
        subject_digest=SUBJECT,
        artifact_store=ArtifactStore(tmp_path / "artifact-store"),
        collected_at=NOW,
        github_token="test-token",
    )
    assert importer._github_api_url == "https://api.github.com"
    with pytest.raises(ValueError):
        OfficialEvidenceImporter(
            workspace_root=tmp_path,
            repository_path=root,
            repository_identity=REPOSITORY,
            head_revision=head,
            subject_digest=SUBJECT,
            artifact_store=ArtifactStore(tmp_path / "artifact-store-2"),
            collected_at=NOW,
            github_token="test-token",
            github_api_url="https://attacker.invalid",
        )


def test_final_fence_rejects_a_new_exact_head(tmp_path):
    root, head = _repository(tmp_path)
    zip_bytes = _zip_payload(root, head)
    transport, _requests = _transport(head, zip_bytes)
    importer = _importer(tmp_path, root, head, transport)
    imported = importer.import_run(RUN_ID)[0]

    (root / "frontend/package.json").write_text(
        '{"name":"changed"}\n', encoding="utf-8"
    )
    _git(root, "add", "frontend/package.json")
    _git(root, "commit", "-qm", "drift")

    with pytest.raises(OfficialEvidenceError):
        importer.verify_import(imported)


@pytest.mark.parametrize(
    "run_id", ("0", "0123", "123.0", "../123", 123, "9" * 20)
)
def test_import_accepts_only_a_positive_numeric_run_id(tmp_path, run_id):
    root, head = _repository(tmp_path)
    transport, requests = _transport(head, b"unused")
    importer = _importer(tmp_path, root, head, transport)

    with pytest.raises((OfficialEvidenceError, ValueError)):
        importer.import_run(run_id)
    assert requests == []


def test_local_report_path_importer_is_not_available(tmp_path):
    root, head = _repository(tmp_path)
    transport, _requests = _transport(head, b"unused")
    importer = _importer(tmp_path, root, head, transport)

    assert not hasattr(importer, "import_file")
