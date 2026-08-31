"""GP-02 focused tests for the in-memory AssuranceRunService orchestration."""

import asyncio
import hashlib
import io
import json
import shutil
import subprocess
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import assurance.run_service as run_service_module
from pydantic import SecretStr

from assurance.artifacts import ArtifactStore
from assurance.commands import CommandSpec
from assurance.contracts import Evidence
from assurance.fixed_reviewer_invoker import (
    FixedOpenAICompatibleReviewerInvoker,
    FixedReviewerEndpoint,
)
from assurance.manifest import EvidenceManifest
from assurance.run_service import (
    AssuranceRunBundle,
    AssuranceRunConfig,
    AssuranceRunError,
    AssuranceRunIntent,
    AssuranceRunOfficialEvidenceError,
    AssuranceRunRedactionError,
    AssuranceRunResult,
    AssuranceRunService,
    AssuranceRunStaleError,
    AssuranceRunPreconditionError,
    AssuranceRunValidationError,
    IdempotencyConflictError,
    RedactionDisposition,
    ReviewerContextPlan,
    ReviewerContextPlanEntry,
    ReviewerInvocationResponse,
    ReviewerRoute,
    ReviewerRunRecord,
)
from assurance.official_evidence import (
    OfficialEvidenceError,
    OfficialEvidenceImport,
    OfficialEvidenceReceipt,
    OfficialEvidenceReport,
    OfficialEvidenceSource,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "CodeMesh Test")
    (root / "TASK.md").write_text(
        "---\n"
        "title: Golden path\n"
        "owner: test\n"
        "version: v1\n"
        "status: active\n"
        "---\n"
        "# Golden path\n\n## Acceptance\n\n- [ ] command passes\n",
        encoding="utf-8",
    )
    (root / "POLICY.md").write_text(
        "# Policy\n\nVersion: v1\n\nStatus: active\n",
        encoding="utf-8",
    )
    (root / "changed.txt").write_text("changed\n", encoding="utf-8")
    workflow = root / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "p-c-handover.yml").write_text(
        "name: P-C Handover Experience\n", encoding="utf-8"
    )
    _git(root, "add", "TASK.md", "POLICY.md", ".github/workflows/p-c-handover.yml")
    _git(root, "commit", "-qm", "base")
    (root / "changed.txt").write_text("changed again\n", encoding="utf-8")
    return root


def _add_api_contract(root: Path) -> None:
    contract = root / "contracts" / "openapi.json"
    contract.parent.mkdir()
    contract.write_text(
        '{"openapi":"3.0.0","paths":{}}\n', encoding="utf-8"
    )
    _git(root, "add", "contracts/openapi.json")
    _git(root, "commit", "-qm", "add api contract")
    api = root / "api"
    api.mkdir()
    (api / "routes.py").write_text("def route(): pass\n", encoding="utf-8")


class _ContextBuilder:
    def __init__(self, disposition=RedactionDisposition.NOT_APPLICABLE):
        self.disposition = disposition
        self.calls = 0

    def prepare(
        self, evidences, *, artifact_store, subject_digest, git_snapshot=None
    ):
        self.calls += 1
        return ReviewerContextPlan(
            entries=tuple(
                ReviewerContextPlanEntry(
                    evidence_id=evidence.evidence_id,
                    kind=evidence.kind,
                    artifact_digest=evidence.artifact_digest,
                    disposition=self.disposition,
                    content=(
                        "safe evidence"
                        if self.disposition
                        in (
                            RedactionDisposition.DECLARED_REDACTED,
                            RedactionDisposition.NOT_APPLICABLE,
                        )
                        else None
                    ),
                    truncated=False,
                )
                for evidence in evidences
            )
        )


class _Reviewer:
    def __init__(self, status="success", questions=False):
        self.status = status
        self.questions = questions
        self.calls = 0

    async def invoke(self, prompt, *, run_id, route):
        self.calls += 1
        if self.status != "success":
            return ReviewerInvocationResponse(
                status=self.status,
                provider=route.provider,
                model_ref=route.model_ref,
                error_code={
                    "failure": "REVIEWER_PROVIDER_FAILURE",
                    "timeout": "REVIEWER_TIMEOUT",
                    "cancelled": "REVIEWER_CANCELLED",
                    "budget_exceeded": "REVIEWER_BUDGET_EXCEEDED",
                }[self.status],
            )
        questions = ()
        if self.questions:
            questions = (
                {
                    "reviewer_role": "intent",
                    "question": "Please confirm the acceptance evidence.",
                    "reason": "model_question",
                    "evidence_refs": [prompt.input.contexts[0].evidence_id],
                },
            )
        payload = {
            "schema_version": "v1",
            "subject_digest": prompt.input.subject.subject_digest,
            "rubric_hash": prompt.rubric_hash,
            "findings": [],
            "questions": list(questions),
        }
        return ReviewerInvocationResponse(
            status="success",
            provider=route.provider,
            model_ref=route.model_ref,
            raw_response=json.dumps(payload, separators=(",", ":")).encode(),
            started_at=prompt.input.evaluated_at,
            completed_at=prompt.input.evaluated_at,
            schema_status="unverified",
            usage_status="unavailable",
        )


class _Committer:
    def __init__(self):
        self.calls = []
        self.cached = None

    def lookup(self, idempotency_key, request_digest):
        self.calls.append(("lookup", idempotency_key, request_digest))
        return self.cached

    def commit(
        self,
        bundle,
        *,
        idempotency_key,
        request_digest,
        official_proofs=(),
    ):
        self.calls.append(("commit", idempotency_key, request_digest))
        self.cached = AssuranceRunResult(
            run_id=bundle.run_id,
            request_digest=request_digest,
            cached=False,
            bundle=bundle,
        )
        return self.cached


class _OfficialProofCapturingCommitter:
    def __init__(self):
        self.proofs = ()

    def lookup(self, _idempotency_key, _request_digest):
        return None

    def commit(
        self,
        bundle,
        *,
        idempotency_key,
        request_digest,
        official_proofs=(),
    ):
        self.proofs = official_proofs
        return AssuranceRunResult(
            run_id=bundle.run_id,
            request_digest=request_digest,
            cached=False,
            bundle=bundle,
        )


def _service(
    tmp_path,
    *,
    reviewer=None,
    context=None,
    committer=None,
    command_specs=None,
    freshness_ttl_seconds=300,
    clock=None,
    reviewer_route=None,
):
    root = _repository(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    if command_specs is None:
        command_specs = (
            CommandSpec(
                command_id="check",
                kind="test",
                argv=("python", "-c", "print('ok')"),
                cwd=".",
                timeout_seconds=5.0,
                max_output_bytes=4096,
            ),
        )
    service = AssuranceRunService(
        artifact_store=store,
        reviewer_invoker=reviewer or _Reviewer(),
        context_builder=context or _ContextBuilder(),
        committer=committer or _Committer(),
        config=AssuranceRunConfig(
            workspace_root=tmp_path,
            redaction_policy_version="redaction.v0",
            policy_version="gate.v0",
            rubric_version="single_general.v0",
            allowed_commands=command_specs,
            freshness_ttl_seconds=freshness_ttl_seconds,
            reviewer_route=reviewer_route
            or ReviewerRoute(
                provider="fake",
                model_ref="fake-model",
                timeout_seconds=30,
            ),
        ),
        clock=clock,
    )
    intent = AssuranceRunIntent(
        repository_path=root,
        repository_identity="example/service",
        author="author-agent",
        base_ref="HEAD",
        task_path="TASK.md",
        policy_paths=("POLICY.md",),
        command_ids=("check",),
        changed_lines_total=1,
        external_side_effects="none_declared",
        provider_boundary="within_declared_boundary",
    )
    return service, intent


def test_intent_rejects_duplicate_or_empty_command_ids_before_io(tmp_path):
    with pytest.raises(ValueError):
        AssuranceRunIntent(
            repository_path=tmp_path / "does-not-exist",
            repository_identity="example/service",
            author="author-agent",
            base_ref="HEAD",
            task_path="TASK.md",
            command_ids=("check", "check"),
        )


def _fake_official_import(
    artifact_store: ArtifactStore,
    *,
    repository_path: Path,
    kind: str,
    subject_digest: str,
    head_revision: str,
    collected_at: datetime,
) -> OfficialEvidenceImport:
    def build_report(report_kind: str) -> tuple[OfficialEvidenceReport, bytes, bytes]:
        result_path = (
            "dependency-audit-result.json"
            if report_kind == "dependency_audit"
            else "ci-iac-result.json"
        )
        result_bytes = b'{"status":"success"}'
        result_digest = "sha256:" + hashlib.sha256(result_bytes).hexdigest()
        source_path = (
            "TASK.md"
            if report_kind == "dependency_audit"
            else ".github/workflows/p-c-handover.yml"
        )
        source_bytes = (repository_path / source_path).read_bytes()
        source = OfficialEvidenceSource(
            path=source_path,
            digest="sha256:" + hashlib.sha256(source_bytes).hexdigest(),
            byte_size=len(source_bytes),
        )
        checks = ()
        audit_command = "pnpm audit --prod --audit-level=high --json"
        if report_kind == "ci_iac_validation":
            checks = tuple(
                {
                    "name": name,
                    "status": "success",
                    "conclusion": "success",
                }
                for name in (
                    "checkout",
                    "install",
                    "focused_checks",
                    "build",
                    "browser_walkthrough",
                )
            )
            audit_command = None
        report = OfficialEvidenceReport(
            kind=report_kind,
            repository_identity="example/service",
            head_revision=head_revision,
            subject_digest=subject_digest,
            producer="collector." + report_kind,
            source_paths=(source,),
            workflow_name="P-C Handover Experience",
            workflow_path=".github/workflows/p-c-handover.yml",
            event="workflow_dispatch",
            pull_request_number=7,
            workflow_run_id="123",
            workflow_run_attempt=1,
            job_id="handover",
            job_name="handover",
            status="success",
            conclusion="success",
            result_path=result_path,
            result_digest=result_digest,
            result_byte_size=len(result_bytes),
            checks=checks,
            audit_command=audit_command,
        )
        report_bytes = json.dumps(
            report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return report, report_bytes, result_bytes

    reports = {
        report_kind: build_report(report_kind)
        for report_kind in ("dependency_audit", "ci_iac_validation")
    }
    remote_buffer = io.BytesIO()
    with zipfile.ZipFile(remote_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for report_kind, (report, report_bytes, result_bytes) in reports.items():
            archive.writestr(
                "dependency_audit.json"
                if report_kind == "dependency_audit"
                else "ci_iac_validation.json",
                report_bytes,
            )
            archive.writestr(report.result_path, result_bytes)
    remote_zip = remote_buffer.getvalue()
    report, report_bytes, result_bytes = reports[kind]
    source = report.source_paths[0]
    result_digest = report.result_digest
    remote_zip_digest = "sha256:" + hashlib.sha256(remote_zip).hexdigest()
    receipt = OfficialEvidenceReceipt(
        kind=kind,
        subject_digest=subject_digest,
        repository_identity="example/service",
        head_revision=head_revision,
        producer=report.producer,
        source_paths=(source,),
        workflow_name=report.workflow_name,
        workflow_path=report.workflow_path,
        event=report.event,
        pull_request_number=report.pull_request_number,
        workflow_run_id=report.workflow_run_id,
        workflow_run_attempt=report.workflow_run_attempt,
        job_id="456",
        job_name="handover",
        artifact_id="789",
        artifact_name="p-c-official-validation-123",
        artifact_digest=remote_zip_digest,
        artifact_byte_size=len(remote_zip),
        report_digest="sha256:" + hashlib.sha256(report_bytes).hexdigest(),
        report_byte_size=len(report_bytes),
        result_path=report.result_path,
        result_digest=result_digest,
        result_byte_size=len(result_bytes),
        report=report,
        result={"status": "success"},
    )
    receipt_bytes = json.dumps(
        receipt.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    receipt_digest = artifact_store.put_bytes(receipt_bytes)
    artifact_store.put_bytes(remote_zip)
    evidence = Evidence(
        evidence_id="ev_official_" + kind,
        subject_digest=subject_digest,
        kind=kind,
        producer=report.producer,
        artifact_digest=receipt_digest,
        source_ref="github:official:" + kind + ":run:123:artifact:789:success",
        trace_id="github:123:1:456",
        status="success",
        trust_level="observed",
        collected_at=collected_at,
    )
    return OfficialEvidenceImport(
        receipt=receipt,
        evidence=evidence,
        receipt_bytes=receipt_bytes,
        receipt_digest=receipt_digest,
        receipt_byte_size=len(receipt_bytes),
        remote_zip_bytes=remote_zip,
        remote_zip_digest=remote_zip_digest,
        remote_zip_byte_size=len(remote_zip),
        source_bindings=(source,),
    )


class _FakeOfficialImporter:
    calls = 0
    verified = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def import_run(self, run_id: str):
        type(self).calls += 1
        assert run_id == "123"
        return tuple(
            _fake_official_import(
                self.kwargs["artifact_store"],
                repository_path=self.kwargs["repository_path"],
                kind=kind,
                subject_digest=self.kwargs["subject_digest"],
                head_revision=self.kwargs["head_revision"],
                collected_at=self.kwargs["collected_at"],
            )
            for kind in ("dependency_audit", "ci_iac_validation")
        )

    def verify_import(self, _imported):
        type(self).verified += 1


def test_official_dependency_evidence_flows_through_manifest_risk_policy_and_bundle(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter)
    service, intent = _service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    intent = intent.model_copy(update={"official_evidence_run_id": "123"})

    result = asyncio.run(service.run(intent, idempotency_key="official-dependency"))

    assert [item.kind for item in result.bundle.evidence] == [
        "git_snapshot",
        "intake_documents",
        "command_batch",
        "evidence_manifest",
        "dependency_audit",
        "ci_iac_validation",
    ]
    assert any(
        item.kind == "dependency_audit"
        for item in result.bundle.manifest.manifest.entries
    )
    assert "dependency_audit" in result.bundle.risk.classification.required_collectors
    assert result.bundle.policy.input.risk_result == result.bundle.risk
    assert result.bundle.case.state == "EVIDENCE_COLLECTED"
    assert _FakeOfficialImporter.calls == 1
    assert _FakeOfficialImporter.verified == 2


def test_service_passes_verified_official_commit_proofs_to_committer(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    committer = _OfficialProofCapturingCommitter()
    service, intent = _service(tmp_path, committer=committer)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    intent = intent.model_copy(update={"official_evidence_run_id": "123"})

    result = asyncio.run(service.run(intent, idempotency_key="official-proof"))

    assert result.bundle.case.state == "EVIDENCE_COLLECTED"
    assert len(committer.proofs) == 2
    assert {proof.kind for proof in committer.proofs} == {
        "dependency_audit",
        "ci_iac_validation",
    }
    assert all(proof.evidence_id in {item.evidence_id for item in result.bundle.evidence} for proof in committer.proofs)


def test_missing_official_dependency_evidence_keeps_policy_blocked(tmp_path):
    service, intent = _service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )

    result = asyncio.run(service.run(intent, idempotency_key="missing-official"))

    assert "dependency_audit" in result.bundle.risk.classification.required_collectors
    assert result.bundle.policy.decision.outcome == "BLOCKED"
    assert result.bundle.policy.decision.reason_codes == (
        "REQUIRED_COLLECTOR_MISSING",
        "REQUIRED_REVIEWER_MISSING",
    )
    assert result.bundle.reviewer.status == "blocked_evidence"
    assert result.bundle.reviewer.error_code == "OFFICIAL_EVIDENCE_MISSING"
    assert service._reviewer_invoker.calls == 0
    assert all(
        item.kind not in {"dependency_audit", "ci_iac_validation"}
        for item in result.bundle.evidence
    )


def test_provider_disabled_preflight_canonicalizes_mixed_blocked_reasons(tmp_path):
    service, intent = _service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )

    result = asyncio.run(service.run(intent, idempotency_key="mixed-blocked-reasons"))
    receipt = result.bundle.execution_receipt
    mixed_receipt = receipt.model_copy(
        update={
            "steps": (
                receipt.steps[0].model_copy(
                    update={
                        "actual_role": "intent",
                        "result": "failure",
                        "schema_status": "invalid",
                    }
                ),
                *receipt.steps[1:],
            )
        }
    )
    mixed_input = result.bundle.policy.input.model_copy(
        update={"execution_receipts": (mixed_receipt,)}
    )
    mixed_policy = run_service_module.PolicyGate.evaluate(mixed_input)

    assert mixed_policy.decision.outcome == "BLOCKED"
    assert mixed_policy.decision.reason_codes == (
        "REQUIRED_COLLECTOR_MISSING",
        "REQUIRED_REVIEWER_MISSING",
        "REQUIRED_REVIEWER_NOT_SUCCESS",
    )
    assert result.bundle.reviewer.status == "blocked_evidence"
    assert result.bundle.reviewer.error_code == "OFFICIAL_EVIDENCE_MISSING"
    assert service._reviewer_invoker.calls == 0


def test_malformed_official_run_fails_before_reviewer_and_commit(tmp_path, monkeypatch):
    class _MalformedImporter(_FakeOfficialImporter):
        def import_run(self, _run_id):
            raise OfficialEvidenceError()

    monkeypatch.setattr(run_service_module, "OfficialEvidenceImporter", _MalformedImporter)
    service, intent = _service(tmp_path)
    intent = intent.model_copy(update={"official_evidence_run_id": "123"})

    with pytest.raises(AssuranceRunOfficialEvidenceError):
        asyncio.run(service.run(intent, idempotency_key="malformed-official"))

    assert not any(call[0] == "commit" for call in service._committer.calls)


@pytest.mark.parametrize(
    "repository_identity",
    (
        "/tmp/repository",
        "//server/share/repository",
        "C:/repository",
        "C:\\repository",
        "~",
        "~/repository",
        "file:///tmp/repository",
    ),
)
def test_intent_rejects_path_like_repository_identity(tmp_path, repository_identity):
    service, intent = _service(tmp_path)
    payload = intent.model_dump()
    payload["repository_identity"] = repository_identity

    with pytest.raises(ValueError):
        AssuranceRunIntent.model_validate(payload)

    payload["repository_identity"] = "github.com/org/repository"
    assert AssuranceRunIntent.model_validate(payload).repository_identity == (
        "github.com/org/repository"
    )


def test_reviewer_record_rejects_forged_state_combinations_and_error_text(tmp_path):
    service, intent = _service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="reviewer-state"))
    record = result.bundle.reviewer

    def invalid(**updates):
        data = record.model_dump()
        data.update(updates)
        with pytest.raises(ValueError):
            ReviewerRunRecord.model_validate(data)

    invalid(actual_provider=None)
    invalid(raw_response_artifact_digest=None)
    invalid(error_code="provider returned secret text")
    invalid(status="failure", result_id=None, result_digest=None)
    invalid(status="blocked_redaction", actual_provider=None, actual_model_ref=None)

    invalid_json = record.model_dump()
    invalid_json.update(
        {
            "status": "invalid_json",
            "schema_status": "invalid",
            "canonical_response_digest": None,
            "result_id": None,
            "result_digest": None,
            "error_code": "REVIEWER_INVALID_JSON",
        }
    )
    assert ReviewerRunRecord.model_validate(invalid_json).status == "invalid_json"
    with pytest.raises(ValueError):
        AssuranceRunIntent(
            repository_path=tmp_path / "does-not-exist",
            repository_identity="example/service",
            author="author-agent",
            base_ref="HEAD",
            task_path="TASK.md",
            command_ids=(),
        )


def test_intent_rejects_command_outside_frozen_allowlist_before_lookup(tmp_path):
    service, intent = _service(tmp_path)
    committer = service._committer
    invalid = intent.model_copy(update={"command_ids": ("unknown",)})

    with pytest.raises(AssuranceRunValidationError):
        asyncio.run(service.run(invalid, idempotency_key="run-unknown-command"))

    assert committer.calls == []


@pytest.mark.parametrize(
    "provider_boundary", ["unknown", "crosses_declared_boundary"]
)
def test_provider_boundary_precondition_fails_before_any_work(
    tmp_path, provider_boundary
):
    service, intent = _service(tmp_path)
    invalid = intent.model_copy(update={"provider_boundary": provider_boundary})

    with pytest.raises(AssuranceRunPreconditionError):
        asyncio.run(service.run(invalid, idempotency_key="run-boundary"))

    assert service._committer.calls == []
    assert service._reviewer_invoker.calls == 0


def test_unsafe_redaction_disposition_cannot_carry_content():
    with pytest.raises(ValueError, match="must not expose content"):
        ReviewerContextPlanEntry(
            evidence_id="ev_1",
            kind="git_diff",
            artifact_digest="sha256:" + "0" * 64,
            disposition=RedactionDisposition.CONTAINS_UNREDACTED_CONTENT,
            content="unredacted",
        )


def test_config_versions_are_required_and_request_digest_bound(tmp_path):
    service, intent = _service(tmp_path)
    config = service._config
    assert config.orchestration_version == "golden.v1"
    assert config.redaction_policy_version == "redaction.v0"

    with pytest.raises(ValueError, match="redaction_policy_version"):
        AssuranceRunConfig(
            workspace_root=config.workspace_root,
            allowed_commands=config.allowed_commands,
            redaction_policy_version="",
        )

    baseline = service._request_digest(intent)
    service._config = AssuranceRunConfig(
        workspace_root=config.workspace_root,
        allowed_commands=config.allowed_commands,
        redaction_policy_version="redaction.v1",
        orchestration_version=config.orchestration_version,
        policy_version=config.policy_version,
        rubric_version=config.rubric_version,
        freshness_ttl_seconds=config.freshness_ttl_seconds,
        reviewer_route=config.reviewer_route,
    )
    assert service._request_digest(intent) != baseline


@pytest.mark.parametrize(
    "field_name",
    [
        "repository",
        "base_revision",
        "head_revision",
        "task_digest",
        "policy_version",
        "created_at",
    ],
)
def test_bundle_validator_binds_subject_facts_to_collected_results(
    tmp_path, field_name
):
    service, intent = _service(tmp_path)
    bundle = asyncio.run(service.run(intent, idempotency_key="run-subject-bind")).bundle
    if field_name in {"repository", "base_revision", "head_revision"}:
        replacement = bundle.git.snapshot.model_copy(
            update={field_name: "other/service" if field_name == "repository" else "0" * 40}
        )
        bad_git = bundle.git.model_copy(update={"snapshot": replacement})
        bad_bundle = bundle.model_copy(update={"git": bad_git})
    elif field_name == "task_digest":
        replacement = bundle.intake.snapshot.model_copy(
            update={"task_digest": "sha256:" + "0" * 64}
        )
        bad_intake = bundle.intake.model_copy(update={"snapshot": replacement})
        bad_bundle = bundle.model_copy(update={"intake": bad_intake})
    elif field_name == "policy_version":
        bad_binding = bundle.binding.model_copy(update={"policy_version": "gate.tampered"})
        bad_bundle = bundle.model_copy(update={"binding": bad_binding})
    else:
        bad_bundle = bundle.model_copy(
            update={"started_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}
        )

    with pytest.raises(ValueError):
        bad_bundle._bind_all_results()


def test_bundle_rejects_api_evidence_trace_id_bypass(tmp_path):
    service, intent = _service(tmp_path)
    _add_api_contract(intent.repository_path)

    result = asyncio.run(service.run(intent, idempotency_key="api-trace-bypass"))
    api = next(item for item in result.bundle.evidence if item.kind == "api_contract")
    forged_api = api.model_copy(update={"trace_id": "forged:trace"})
    forged_evidence = tuple(
        forged_api if item.evidence_id == api.evidence_id else item
        for item in result.bundle.evidence
    )
    forged_bundle = result.bundle.model_copy(update={"evidence": forged_evidence})

    with pytest.raises(ValueError, match="api_contract Evidence is invalid"):
        forged_bundle._bind_all_results()


def test_bundle_rejects_cross_kind_duplicate_evidence_id_before_manifest_set_compare(
    tmp_path, monkeypatch
):
    _FakeOfficialImporter.calls = 0
    _FakeOfficialImporter.verified = 0
    monkeypatch.setattr(
        run_service_module, "OfficialEvidenceImporter", _FakeOfficialImporter
    )
    service, intent = _service(tmp_path)
    (intent.repository_path / "package-lock.json").write_text(
        '{"lockfileVersion":3}\n', encoding="utf-8"
    )
    intent = intent.model_copy(update={"official_evidence_run_id": "123"})
    bundle = asyncio.run(
        service.run(intent, idempotency_key="cross-kind-duplicate")
    ).bundle

    duplicate_id = bundle.git.evidence.evidence_id
    official = next(
        item for item in bundle.evidence if item.kind == "dependency_audit"
    )
    forged_official = official.model_copy(update={"evidence_id": duplicate_id})
    forged_evidence = tuple(
        forged_official if item.evidence_id == official.evidence_id else item
        for item in bundle.evidence
    )

    manifest_entries = tuple(
        entry.model_copy(update={"evidence_id": duplicate_id})
        if entry.evidence_id == official.evidence_id
        else entry
        for entry in bundle.manifest.manifest.entries
    )
    manifest_data = bundle.manifest.manifest.model_dump(mode="python")
    manifest_data["entries"] = manifest_entries
    forged_manifest = EvidenceManifest.model_construct(**manifest_data)
    forged_manifest_result = bundle.manifest.model_copy(
        update={"manifest": forged_manifest}
    )
    forged_risk_input = bundle.risk.input.model_copy(
        update={"manifest": forged_manifest}
    )
    forged_risk = bundle.risk.model_copy(update={"input": forged_risk_input})
    forged_policy_input = bundle.policy.input.model_copy(
        update={"risk_result": forged_risk}
    )
    forged_policy = bundle.policy.model_copy(update={"input": forged_policy_input})
    forged_bundle = bundle.model_copy(
        update={
            "evidence": forged_evidence,
            "manifest": forged_manifest_result,
            "risk": forged_risk,
            "policy": forged_policy,
        }
    )

    with pytest.raises(ValueError, match="evidence_id"):
        forged_bundle._bind_all_results()


@pytest.mark.parametrize("field_name", ["snapshot", "intake", "manifest"])
def test_bundle_validator_binds_risk_input_to_exact_collected_results(
    tmp_path, field_name
):
    service, intent = _service(tmp_path)
    bundle = asyncio.run(service.run(intent, idempotency_key="run-risk-bind")).bundle
    if field_name == "snapshot":
        replacement = bundle.risk.input.snapshot.model_copy(
            update={"head_revision": "0" * 40}
        )
    elif field_name == "intake":
        replacement = bundle.risk.input.intake.model_copy(
            update={"task_digest": "sha256:" + "0" * 64}
        )
    else:
        replacement = bundle.risk.input.manifest.model_copy(
            update={"evaluated_at": datetime(2030, 1, 1, tzinfo=timezone.utc)}
        )
    bad_input = bundle.risk.input.model_copy(update={field_name: replacement})
    bad_risk = bundle.risk.model_copy(update={"input": bad_input})
    bad_bundle = bundle.model_copy(update={"risk": bad_risk})

    with pytest.raises(ValueError, match="risk input .* must match bundle"):
        bad_bundle._bind_all_results()


@pytest.mark.parametrize("field_name", ["run_id", "request_digest"])
def test_result_validator_binds_envelope_to_bundle(tmp_path, field_name):
    service, intent = _service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="run-result-bind"))
    replacement = (
        "run_tampered"
        if field_name == "run_id"
        else "sha256:" + "0" * 64
    )

    with pytest.raises(ValueError, match="result .* must match bundle"):
        AssuranceRunResult.model_validate(
            {**result.__dict__, field_name: replacement}
        )


def test_real_git_happy_path_commits_and_never_accepts(tmp_path):
    service, intent = _service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="run-1"))

    assert result.bundle.subject.subject_digest == result.bundle.git.snapshot.subject_digest
    assert result.bundle.case.state == "EVIDENCE_COLLECTED"
    assert result.bundle.policy.decision.outcome == "PASS"
    assert result.bundle.events[0].kind == "COLLECT_EVIDENCE"
    assert result.bundle.execution_receipt.overall_result == "success"
    assert result.bundle.reviewer.prompt_id.startswith("srp_")
    assert result.bundle.reviewer.prompt_digest.startswith("sha256:")


def test_success_receipt_binds_non_default_reviewer_route(tmp_path):
    service, intent = _service(tmp_path)
    service._config = AssuranceRunConfig(
        workspace_root=service._config.workspace_root,
        allowed_commands=service._config.allowed_commands,
        redaction_policy_version=service._config.redaction_policy_version,
        orchestration_version=service._config.orchestration_version,
        policy_version=service._config.policy_version,
        rubric_version=service._config.rubric_version,
        freshness_ttl_seconds=service._config.freshness_ttl_seconds,
        reviewer_route=ReviewerRoute(
            provider="custom-provider",
            model_ref="custom-model",
            timeout_seconds=17,
            token_budget=321,
            routing_rule="custom.route:v1",
        ),
    )

    result = asyncio.run(service.run(intent, idempotency_key="run-custom-route"))
    route = service._config.reviewer_route
    assert all(
        (
            step.routing_rule == route.routing_rule
            and step.timeout_seconds == route.timeout_seconds
            and step.token_budget == route.token_budget
            and step.tool_grants == route.tool_grants
        )
        for step in result.bundle.execution_receipt.steps
    )


def test_questions_request_evidence_and_cached_replay_does_not_invoke_again(tmp_path):
    reviewer = _Reviewer(questions=True)
    committer = _Committer()
    service, intent = _service(tmp_path, reviewer=reviewer, committer=committer)
    first = asyncio.run(service.run(intent, idempotency_key="run-questions"))

    assert first.bundle.case.state == "NEEDS_EVIDENCE"
    assert first.bundle.case.missing_evidence == (
        "review_question:" + first.bundle.questions[0].question_id,
    )
    assert first.bundle.events[-1].kind == "REQUEST_EVIDENCE"
    assert reviewer.calls == 1

    replay = asyncio.run(service.run(intent, idempotency_key="run-questions"))
    assert replay.cached is True
    assert reviewer.calls == 1


def test_prepare_returns_complete_bundle_without_committer_io(tmp_path):
    committer = _Committer()
    service, intent = _service(tmp_path, committer=committer)

    bundle = asyncio.run(service.prepare(intent, idempotency_key="prepare-only"))

    assert type(bundle) is AssuranceRunBundle
    assert bundle.idempotency_key == "prepare-only"
    request_digest = service._request_digest(intent)
    assert bundle.request_digest == request_digest
    assert bundle.run_id == service._run_id(request_digest, "prepare-only")
    assert bundle.evidence == (
        bundle.git.evidence,
        bundle.intake.evidence,
        bundle.commands.evidence,
        bundle.manifest.evidence,
    )
    assert bundle.policy.input.subject == bundle.subject
    assert bundle.policy.input.risk_result == bundle.risk
    assert bundle.policy.input.findings == bundle.findings
    assert bundle.policy.input.execution_receipts == (bundle.execution_receipt,)
    assert bundle.freshness_source_binding.subject == bundle.subject
    assert committer.calls == []


def test_default_run_service_git_collector_profile_is_one_mib(tmp_path):
    service, _ = _service(tmp_path)

    assert service._git_collector.max_diff_bytes == 1_048_576
    assert (
        run_service_module._DEFAULT_GIT_COLLECTOR_PROFILE["max_diff_bytes"]
        == 1_048_576
    )


def test_run_cache_hit_skips_prepare_and_miss_prepares_once_then_commits_once(
    tmp_path, monkeypatch
):
    committer = _Committer()
    service, intent = _service(tmp_path, committer=committer)
    prepare_calls = 0
    original_prepare_bundle = service._prepare_bundle

    async def counted_prepare_bundle(*args, **kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return await original_prepare_bundle(*args, **kwargs)

    monkeypatch.setattr(service, "_prepare_bundle", counted_prepare_bundle)

    first = asyncio.run(service.run(intent, idempotency_key="run-prepare-once"))
    replay = asyncio.run(service.run(intent, idempotency_key="run-prepare-once"))

    assert type(first.bundle) is AssuranceRunBundle
    assert replay.cached is True
    assert prepare_calls == 1
    assert [call[0] for call in committer.calls] == ["lookup", "commit", "lookup"]


def test_commit_race_winner_preserves_adapter_cached_value(tmp_path):
    class _RaceCommitter(_Committer):
        def commit(self, bundle, *, idempotency_key, request_digest):
            self.calls.append(("commit", idempotency_key, request_digest))
            self.cached = AssuranceRunResult(
                run_id=bundle.run_id,
                request_digest=request_digest,
                cached=True,
                bundle=bundle,
            )
            return self.cached

    committer = _RaceCommitter()
    service, intent = _service(tmp_path, committer=committer)

    result = asyncio.run(service.run(intent, idempotency_key="run-race-winner"))

    assert result.cached is True


def test_unsafe_redaction_blocks_reviewer_and_commit(tmp_path):
    reviewer = _Reviewer()
    committer = _Committer()
    context = _ContextBuilder(RedactionDisposition.CONTAINS_UNREDACTED_CONTENT)
    service, intent = _service(
        tmp_path, reviewer=reviewer, context=context, committer=committer
    )

    result = asyncio.run(service.run(intent, idempotency_key="run-unsafe"))

    assert reviewer.calls == 0
    assert any(call[0] == "commit" for call in committer.calls)
    assert result.bundle.policy.decision.outcome == "BLOCKED"
    assert result.bundle.reviewer.status == "blocked_redaction"
    assert result.bundle.reviewer.prompt_id is None
    assert result.bundle.reviewer.prompt_digest is None


def test_redaction_adapter_error_fails_without_commit(tmp_path):
    class _ErrorContext(_ContextBuilder):
        def prepare(
            self,
            evidences,
            *,
            artifact_store,
            subject_digest,
            git_snapshot=None,
        ):
            self.calls += 1
            raise RuntimeError("redaction adapter unavailable")

    committer = _Committer()
    service, intent = _service(
        tmp_path, context=_ErrorContext(), committer=committer
    )

    with pytest.raises(AssuranceRunRedactionError):
        asyncio.run(service.run(intent, idempotency_key="run-redaction-error"))

    assert not any(call[0] == "commit" for call in committer.calls)


@pytest.mark.parametrize("status", ["timeout", "failure", "cancelled", "budget_exceeded"])
def test_reviewer_failure_is_a_failed_receipt_not_a_success_invocation(tmp_path, status):
    reviewer = _Reviewer(status=status)
    service, intent = _service(tmp_path, reviewer=reviewer)

    result = asyncio.run(service.run(intent, idempotency_key="run-failed-" + status))

    assert result.bundle.reviewer.status == status
    assert result.bundle.reviewer.error_code == {
        "timeout": "REVIEWER_TIMEOUT",
        "failure": "REVIEWER_PROVIDER_FAILURE",
        "cancelled": "REVIEWER_CANCELLED",
        "budget_exceeded": "REVIEWER_BUDGET_EXCEEDED",
    }[status]
    assert "error_message" not in result.bundle.reviewer.model_dump()
    assert result.bundle.execution_receipt.overall_result in {"failure", "cancelled"}
    assert result.bundle.policy.decision.outcome == "BLOCKED"
    assert "REQUIRED_REVIEWER_NOT_SUCCESS" in result.bundle.policy.decision.reason_codes
    assert all(step.fallback_reason is None for step in result.bundle.execution_receipt.steps)


def test_new_safe_reviewer_failure_stage_is_durable_without_raw_transport_data(tmp_path):
    class _LaunchFailureReviewer:
        async def invoke(self, prompt, *, run_id, route):
            return ReviewerInvocationResponse(
                status="failure",
                provider=route.provider,
                model_ref=route.model_ref,
                error_code="REVIEWER_PROCESS_LAUNCH_FAILURE",
            )

    service, intent = _service(
        tmp_path,
        reviewer=_LaunchFailureReviewer(),
        command_specs=(
            CommandSpec(
                command_id="check",
                kind="test",
                argv=("/usr/bin/true",),
                cwd=".",
                timeout_seconds=5.0,
                max_output_bytes=4096,
            ),
        ),
    )
    result = asyncio.run(service.run(intent, idempotency_key="run-safe-stage"))

    assert result.bundle.reviewer.status == "failure"
    assert result.bundle.reviewer.error_code == "REVIEWER_PROVIDER_FAILURE"
    assert result.bundle.reviewer.schema_status == "not_produced"
    assert result.bundle.reviewer.raw_response_artifact_digest is None
    assert result.bundle.reviewer.canonical_response_digest is None
    assert result.bundle.reviewer.result_digest is None
    assert result.bundle.policy.decision.outcome == "BLOCKED"
    assert all(
        step.fallback_reason == "process_launch"
        for step in result.bundle.execution_receipt.steps
    )
    serialized = json.dumps(result.model_dump(mode="json"), sort_keys=True)
    for secret in ("launch secret", "/private/credential", "--token=do-not-leak"):
        assert secret not in serialized


@pytest.mark.parametrize("variant", ["timeout", "cancelled", "invalid_json", "blocked", "failure_valid"])
def test_reviewer_record_enforces_exact_status_error_and_fact_combinations(tmp_path, variant):
    service, intent = _service(tmp_path)
    result = asyncio.run(service.run(intent, idempotency_key="reviewer-combination-" + variant))
    data = result.bundle.reviewer.model_dump()
    if variant in {"timeout", "cancelled"}:
        data.update(
                {
                    "status": variant,
                    "error_code": "REVIEWER_CANCELLED" if variant == "timeout" else "REVIEWER_TIMEOUT",
                "schema_status": "not_produced",
                "raw_response_artifact_digest": None,
                "canonical_response_digest": None,
                "result_id": None,
                "result_digest": None,
                "usage_status": "unavailable",
                "input_tokens": None,
                "output_tokens": None,
                "cost_usd": None,
            }
        )
    elif variant == "invalid_json":
        data.update(
            {
                "status": "invalid_json",
                "schema_status": "invalid",
                "raw_response_artifact_digest": None,
                "canonical_response_digest": None,
                "result_id": None,
                "result_digest": None,
                "error_code": "REVIEWER_INVALID_JSON",
            }
        )
    elif variant == "blocked":
        data.update(
            {
                "status": "blocked_redaction",
                "prompt_id": None,
                "prompt_digest": None,
                "actual_provider": None,
                "actual_model_ref": None,
                "schema_status": "not_produced",
                "raw_response_artifact_digest": None,
                "canonical_response_digest": None,
                "result_id": None,
                "result_digest": None,
                "error_code": "REDACTION_UNSAFE",
                "usage_status": "measured",
                "input_tokens": 1,
                "output_tokens": 1,
                "cost_usd": 0.01,
            }
        )
    else:
        data.update(
            {
                "status": "failure",
                "error_code": "REVIEWER_PROVIDER_FAILURE",
                "schema_status": "valid",
                "raw_response_artifact_digest": None,
                "canonical_response_digest": None,
                "result_id": None,
                "result_digest": None,
            }
        )
    with pytest.raises(ValueError):
        ReviewerRunRecord.model_validate(data)


def test_missing_git_collector_profile_limit_fails_closed(tmp_path):
    service, intent = _service(tmp_path)

    class _CollectorWithoutProfile:
        def __init__(self, delegate):
            self._delegate = delegate

        def collect(self, *args, **kwargs):
            return self._delegate.collect(*args, **kwargs)

    service._git_collector = _CollectorWithoutProfile(service._git_collector)
    with pytest.raises(AssuranceRunStaleError, match="profile"):
        asyncio.run(service.run(intent, idempotency_key="missing-git-profile"))


def test_invalid_reviewer_json_is_fail_closed(tmp_path):
    class _InvalidJsonReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            self.calls += 1
            return ReviewerInvocationResponse(
                status="success",
                provider=route.provider,
                model_ref=route.model_ref,
                raw_response=b"not-json",
                started_at=prompt.input.evaluated_at,
                completed_at=prompt.input.evaluated_at,
                schema_status="unverified",
            )

    service, intent = _service(tmp_path, reviewer=_InvalidJsonReviewer())
    result = asyncio.run(service.run(intent, idempotency_key="run-invalid-json"))

    assert result.bundle.reviewer.status == "invalid_json"
    assert result.bundle.reviewer.raw_response_artifact_digest is not None
    assert service._artifact_store.verify(
        result.bundle.reviewer.raw_response_artifact_digest
    )
    assert result.bundle.reviewer.error_code == "REVIEWER_INVALID_JSON"
    assert "error_message" not in result.bundle.reviewer.model_dump()
    assert result.bundle.execution_receipt.overall_result == "failure"
    assert result.bundle.policy.decision.outcome == "BLOCKED"


def test_fixed_invoker_unverified_invalid_json_persists_raw_and_blocks(tmp_path):
    route = ReviewerRoute(
        provider="openai-compatible",
        model_ref="reviewer-model",
        timeout_seconds=5,
        token_budget=64,
        routing_rule="single_general.v0:fixed",
    )
    seen = []

    async def handler(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-invalid",
                "object": "chat.completion",
                "created": 1,
                "model": "reviewer-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "not-json"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    service, intent = _service(tmp_path, reviewer_route=route)
    invoker = FixedOpenAICompatibleReviewerInvoker(
        FixedReviewerEndpoint(
            route=route,
            base_url="https://reviewer.example/v1",
            api_key=SecretStr("test-secret"),
        ),
        transport=httpx.MockTransport(handler),
    )
    service._reviewer_invoker = invoker

    result = asyncio.run(
        service.run(intent, idempotency_key="run-fixed-invalid-json")
    )
    asyncio.run(invoker.aclose())

    assert len(seen) == 1
    assert result.bundle.reviewer.status == "invalid_json"
    assert result.bundle.reviewer.schema_status == "invalid"
    assert result.bundle.reviewer.raw_response_artifact_digest is not None
    assert service._artifact_store.verify(
        result.bundle.reviewer.raw_response_artifact_digest
    )
    assert result.bundle.execution_receipt.overall_result == "failure"
    assert result.bundle.policy.decision.outcome == "BLOCKED"


def test_unverified_transport_is_normalized_before_persisting_valid(tmp_path):
    class _UnverifiedReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            self.calls += 1
            payload = {
                "schema_version": "v1",
                "subject_digest": prompt.input.subject.subject_digest,
                "rubric_hash": prompt.rubric_hash,
                "findings": [],
                "questions": [],
            }
            return ReviewerInvocationResponse(
                status="success",
                provider=route.provider,
                model_ref=route.model_ref,
                raw_response=json.dumps(payload, separators=(",", ":")).encode(),
                started_at=prompt.input.evaluated_at,
                completed_at=prompt.input.evaluated_at,
                schema_status="unverified",
                usage_status="unavailable",
            )

    service, intent = _service(tmp_path, reviewer=_UnverifiedReviewer())
    result = asyncio.run(service.run(intent, idempotency_key="run-unverified"))

    assert result.bundle.reviewer.status == "success"
    assert result.bundle.reviewer.schema_status == "valid"
    assert all(step.schema_status == "valid" for step in result.bundle.execution_receipt.steps)
    assert result.bundle.reviewer.schema_status != "unverified"
    assert all(
        step.schema_status != "unverified"
        for step in result.bundle.execution_receipt.steps
    )


def test_reviewer_response_missing_error_code_is_not_overwritten(tmp_path):
    class _MissingReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            self.calls += 1
            return ReviewerInvocationResponse(
                status="failure",
                provider=route.provider,
                model_ref=route.model_ref,
                error_code="REVIEWER_RESPONSE_MISSING",
            )

    service, intent = _service(tmp_path, reviewer=_MissingReviewer())
    result = asyncio.run(service.run(intent, idempotency_key="run-missing-response"))

    assert result.bundle.reviewer.status == "failure"
    assert result.bundle.reviewer.error_code == "REVIEWER_RESPONSE_MISSING"


@pytest.mark.parametrize(
    "updates",
    (
        {"status": "success", "schema_status": "valid"},
        {"status": "success", "error_code": "REVIEWER_PROVIDER_FAILURE"},
        {"status": "success", "raw_response": b""},
        {
            "status": "failure",
            "error_code": "REVIEWER_PROVIDER_FAILURE",
            "raw_response": b"unexpected",
        },
        {
            "status": "failure",
            "error_code": "REVIEWER_PROVIDER_FAILURE",
            "schema_status": "invalid",
        },
        {"status": "timeout", "error_code": "REVIEWER_PROVIDER_FAILURE"},
        {
            "status": "failure",
            "error_code": "REVIEWER_PROVIDER_FAILURE",
            "usage_status": "unavailable",
            "input_tokens": 1,
        },
        {
            "status": "success",
            "usage_status": "measured",
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_usd": None,
        },
    ),
)
def test_contradictory_transport_facts_fail_before_pass_or_commit(tmp_path, updates):
    class _ContradictoryReviewer:
        async def invoke(self, prompt, *, run_id, route):
            values = {
                "status": "success",
                "provider": route.provider,
                "model_ref": route.model_ref,
                "raw_response": b"{}",
                "schema_status": "unverified",
                "usage_status": "unavailable",
            }
            values.update(updates)
            return ReviewerInvocationResponse(**values)

    service, intent = _service(tmp_path, reviewer=_ContradictoryReviewer())

    with pytest.raises(ValueError):
        asyncio.run(
            service.run(intent, idempotency_key="run-contradictory-transport")
        )

    assert not any(call[0] == "commit" for call in service._committer.calls)


@pytest.mark.parametrize("forge_kind", ("model_copy", "model_construct", "mapping"))
def test_coerce_revalidates_forged_instance_and_mapping_before_commit(
    tmp_path, forge_kind
):
    class _ForgedReviewer:
        async def invoke(self, prompt, *, run_id, route):
            values = {
                "status": "success",
                "provider": route.provider,
                "model_ref": route.model_ref,
                "raw_response": b"{}",
                "schema_status": "unverified",
                "usage_status": "unavailable",
            }
            valid = ReviewerInvocationResponse(**values)
            if forge_kind == "model_copy":
                return valid.model_copy(update={"schema_status": "valid"})
            if forge_kind == "model_construct":
                values["schema_status"] = "valid"
                return ReviewerInvocationResponse.model_construct(**values)
            values["schema_status"] = "valid"
            return values

    service, intent = _service(tmp_path, reviewer=_ForgedReviewer())

    with pytest.raises(ValueError):
        asyncio.run(service.run(intent, idempotency_key="run-forged-response"))

    assert not any(call[0] == "commit" for call in service._committer.calls)


def test_unknown_reviewer_exception_propagates_without_commit(tmp_path):
    class _UnknownErrorReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            self.calls += 1
            raise ValueError("provider exploded")

    committer = _Committer()
    service, intent = _service(
        tmp_path,
        reviewer=_UnknownErrorReviewer(),
        committer=committer,
    )

    with pytest.raises(ValueError, match="provider exploded"):
        asyncio.run(service.run(intent, idempotency_key="run-reviewer-error"))

    assert not any(call[0] == "commit" for call in committer.calls)


def test_reviewer_cancelled_error_propagates_without_commit(tmp_path):
    class _CancelledReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            self.calls += 1
            raise asyncio.CancelledError()

    committer = _Committer()
    service, intent = _service(
        tmp_path,
        reviewer=_CancelledReviewer(),
        committer=committer,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run(intent, idempotency_key="run-reviewer-cancelled"))

    assert not any(call[0] == "commit" for call in committer.calls)


def test_unknown_normalization_error_propagates_without_commit(tmp_path, monkeypatch):
    def _unexpected_normalization_error(*args, **kwargs):
        raise RuntimeError("normalizer unavailable")

    monkeypatch.setattr(
        run_service_module.SingleStrongReviewer,
        "normalize",
        staticmethod(_unexpected_normalization_error),
    )
    committer = _Committer()
    service, intent = _service(tmp_path, committer=committer)

    with pytest.raises(RuntimeError, match="normalizer unavailable"):
        asyncio.run(service.run(intent, idempotency_key="run-normalizer-error"))

    assert not any(call[0] == "commit" for call in committer.calls)


def test_probe_and_intake_task_drift_fails_before_commit(tmp_path):
    service, intent = _service(tmp_path)
    committer = service._committer
    original = service._intake_collector

    class _DriftingIntake:
        def probe_task_digest(self, *args, **kwargs):
            return original.probe_task_digest(*args, **kwargs)

        def collect(self, *args, **kwargs):
            task = intent.repository_path / "TASK.md"
            task.write_text(task.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
            return original.collect(*args, **kwargs)

    service._intake_collector = _DriftingIntake()
    with pytest.raises(AssuranceRunStaleError):
        asyncio.run(service.run(intent, idempotency_key="run-task-drift"))

    assert not any(call[0] == "commit" for call in committer.calls)


@pytest.mark.parametrize(
    ("argv", "outcome"),
    [
        (("python", "-c", "import sys; sys.exit(3)"), "failure"),
        (("python", "-c", "import time; time.sleep(0.2)"), "timeout"),
    ],
)
def test_command_failure_or_timeout_is_collected_and_policy_blocks(
    tmp_path, argv, outcome
):
    command_specs = (
        CommandSpec(
            command_id="check",
            kind="test",
            argv=argv,
            cwd=".",
            timeout_seconds=0.05 if outcome == "timeout" else 5.0,
            max_output_bytes=4096,
        ),
    )
    service, intent = _service(tmp_path, command_specs=command_specs)

    result = asyncio.run(service.run(intent, idempotency_key="run-command-" + outcome))

    assert result.bundle.commands.snapshot.commands[0].outcome == outcome
    assert result.bundle.policy.decision.outcome == "BLOCKED"


def test_missing_typed_collector_is_exact_policy_block(tmp_path):
    service, intent = _service(tmp_path)
    api_dir = intent.repository_path / "api"
    api_dir.mkdir()
    (api_dir / "routes.py").write_text("def route(): pass\n", encoding="utf-8")

    result = asyncio.run(service.run(intent, idempotency_key="run-missing-typed"))

    assert "api_contract" in result.bundle.risk.classification.required_collectors
    assert result.bundle.policy.decision.outcome == "BLOCKED"
    assert result.bundle.policy.decision.reason_codes == ("REQUIRED_COLLECTOR_MISSING",)
    assert all(
        entry.producer != "generic_importer"
        for entry in result.bundle.manifest.manifest.entries
    )


def test_same_key_with_different_request_digest_conflicts_before_external_work(tmp_path):
    committer = _Committer()
    service, intent = _service(tmp_path, committer=committer)
    asyncio.run(service.run(intent, idempotency_key="run-conflict"))
    changed_intent = intent.model_copy(update={"changed_lines_total": 2})

    with pytest.raises(IdempotencyConflictError):
        asyncio.run(service.run(changed_intent, idempotency_key="run-conflict"))

    assert [call[0] for call in committer.calls] == ["lookup", "commit", "lookup"]


def test_same_key_conflict_precedes_workspace_validation_when_repo_deleted(tmp_path):
    committer = _Committer()
    service, intent = _service(tmp_path, committer=committer)
    asyncio.run(service.run(intent, idempotency_key="run-deleted-conflict"))
    changed_intent = intent.model_copy(update={"changed_lines_total": 2})
    shutil.rmtree(intent.repository_path)

    with pytest.raises(IdempotencyConflictError):
        asyncio.run(service.run(changed_intent, idempotency_key="run-deleted-conflict"))

    assert [call[0] for call in committer.calls] == ["lookup", "commit", "lookup"]


def test_committer_error_is_not_converted_to_success(tmp_path):
    class _ErrorCommitter(_Committer):
        def commit(self, bundle, *, idempotency_key, request_digest):
            self.calls.append(("commit", idempotency_key, request_digest))
            raise RuntimeError("commit failed")

    committer = _ErrorCommitter()
    service, intent = _service(tmp_path, committer=committer)

    with pytest.raises(RuntimeError, match="commit failed"):
        asyncio.run(service.run(intent, idempotency_key="run-commit-error"))

    assert [call[0] for call in committer.calls] == ["lookup", "commit"]


def test_committer_none_is_not_converted_to_success(tmp_path):
    class _NoneCommitter(_Committer):
        def commit(self, bundle, *, idempotency_key, request_digest):
            self.calls.append(("commit", idempotency_key, request_digest))
            return None

    committer = _NoneCommitter()
    service, intent = _service(tmp_path, committer=committer)

    with pytest.raises(AssuranceRunError, match="did not return a persisted result"):
        asyncio.run(service.run(intent, idempotency_key="run-commit-none"))

    assert [call[0] for call in committer.calls] == ["lookup", "commit"]


def test_final_fence_ttl_uses_fresh_clock_after_artifact_verification(tmp_path):
    base = datetime.now(timezone.utc)
    clock_calls = []

    def clock():
        clock_calls.append(len(clock_calls) + 1)
        return base if len(clock_calls) < 6 else base + timedelta(seconds=2)

    service, intent = _service(
        tmp_path,
        freshness_ttl_seconds=1,
        clock=clock,
    )
    committer = service._committer

    with pytest.raises(AssuranceRunStaleError, match="TTL expired"):
        asyncio.run(service.run(intent, idempotency_key="run-ttl-expired"))

    assert len(clock_calls) >= 6
    assert not any(call[0] == "commit" for call in committer.calls)


def test_service_preserves_frozen_stage_order(tmp_path):
    service, intent = _service(tmp_path)
    order = []
    original_git = service._git_collector
    original_intake = service._intake_collector
    original_commands = service._command_collector
    original_context = service._context_builder
    original_reviewer = service._reviewer_invoker
    original_committer = service._committer

    class _Git:
        def collect(self, *args, **kwargs):
            order.append("git")
            return original_git.collect(*args, **kwargs)

    class _Intake:
        def probe_task_digest(self, *args, **kwargs):
            order.append("probe")
            return original_intake.probe_task_digest(*args, **kwargs)

        def collect(self, *args, **kwargs):
            order.append("intake")
            return original_intake.collect(*args, **kwargs)

    class _Commands:
        def collect(self, *args, **kwargs):
            order.append("commands")
            return original_commands.collect(*args, **kwargs)

    class _Context:
        def prepare(self, *args, **kwargs):
            order.append("redaction")
            return original_context.prepare(*args, **kwargs)

    class _Reviewer:
        async def invoke(self, *args, **kwargs):
            order.append("reviewer")
            return await original_reviewer.invoke(*args, **kwargs)

    class _Committer:
        def lookup(self, *args, **kwargs):
            order.append("lookup")
            return original_committer.lookup(*args, **kwargs)

        def commit(self, *args, **kwargs):
            order.append("commit")
            return original_committer.commit(*args, **kwargs)

    wrapped_git = _Git()
    for attribute in (
        "max_diff_bytes",
        "max_files",
        "max_file_bytes",
        "command_timeout_seconds",
    ):
        setattr(wrapped_git, attribute, getattr(original_git, attribute))
    service._git_collector = wrapped_git
    service._intake_collector = _Intake()
    service._command_collector = _Commands()
    service._context_builder = _Context()
    service._reviewer_invoker = _Reviewer()
    service._committer = _Committer()

    asyncio.run(service.run(intent, idempotency_key="run-order"))

    assert order == [
        "lookup",
        "probe",
        "git",
        "intake",
        "commands",
        "redaction",
        "reviewer",
        "git",
        "intake",
        "commit",
    ]


def test_sync_validation_collectors_context_normalize_artifact_and_commit_use_worker_threads(
    tmp_path, monkeypatch
):
    service, intent = _service(tmp_path)
    event_loop_thread = threading.get_ident()
    thread_ids = {}
    original_git = service._git_collector
    original_intake = service._intake_collector
    original_commands = service._command_collector
    original_context = service._context_builder
    original_committer = service._committer
    original_verify = service._artifact_store.verify
    original_normalize = run_service_module.SingleStrongReviewer.normalize

    def record(name):
        thread_ids.setdefault(name, threading.get_ident())

    def validate_workspace(value):
        record("workspace")
        return original_validate_workspace(value)

    original_validate_workspace = service._validate_workspace
    service._validate_workspace = validate_workspace

    class _Git:
        def collect(self, *args, **kwargs):
            record("git")
            return original_git.collect(*args, **kwargs)

    class _Intake:
        def probe_task_digest(self, *args, **kwargs):
            record("probe")
            return original_intake.probe_task_digest(*args, **kwargs)

        def collect(self, *args, **kwargs):
            record("intake")
            return original_intake.collect(*args, **kwargs)

    class _Commands:
        def collect(self, *args, **kwargs):
            record("commands")
            return original_commands.collect(*args, **kwargs)

    class _Context:
        def prepare(self, *args, **kwargs):
            record("context")
            return original_context.prepare(*args, **kwargs)

    class _Committer:
        def lookup(self, *args, **kwargs):
            record("lookup")
            return original_committer.lookup(*args, **kwargs)

        def commit(self, *args, **kwargs):
            record("commit")
            return original_committer.commit(*args, **kwargs)

    def verify(digest):
        record("artifact")
        return original_verify(digest)

    def normalize(*args, **kwargs):
        record("normalize")
        return original_normalize(*args, **kwargs)

    monkeypatch.setattr(
        run_service_module.SingleStrongReviewer,
        "normalize",
        staticmethod(normalize),
    )
    wrapped_git = _Git()
    for attribute in (
        "max_diff_bytes",
        "max_files",
        "max_file_bytes",
        "command_timeout_seconds",
    ):
        setattr(wrapped_git, attribute, getattr(original_git, attribute))
    service._git_collector = wrapped_git
    service._intake_collector = _Intake()
    service._command_collector = _Commands()
    service._context_builder = _Context()
    service._committer = _Committer()
    service._artifact_store.verify = verify

    asyncio.run(service.run(intent, idempotency_key="run-worker-threads"))

    assert {
        "workspace",
        "lookup",
        "probe",
        "git",
        "intake",
        "commands",
        "context",
        "normalize",
        "artifact",
        "commit",
    } <= thread_ids.keys()
    assert all(thread_id != event_loop_thread for thread_id in thread_ids.values())


def test_final_git_fence_rejects_reviewer_side_drift_without_commit(tmp_path):
    service, intent = _service(tmp_path)
    committer = service._committer

    class _DriftingReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            self.calls += 1
            (intent.repository_path / "changed.txt").write_text(
                "changed by reviewer\n", encoding="utf-8"
            )
            return await _Reviewer.invoke(self, prompt, run_id=run_id, route=route)

    service._reviewer_invoker = _DriftingReviewer()
    with pytest.raises(AssuranceRunStaleError):
        asyncio.run(service.run(intent, idempotency_key="run-drift"))

    assert not any(call[0] == "commit" for call in committer.calls)


def test_final_fence_rejects_tampered_evidence_artifact(tmp_path):
    service, intent = _service(tmp_path)
    committer = service._committer

    class _TamperingReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            digest = prompt.input.contexts[0].artifact_digest
            service._artifact_store._artifact_path(digest).write_bytes(b"tampered")
            return await super().invoke(prompt, run_id=run_id, route=route)

    service._reviewer_invoker = _TamperingReviewer()
    with pytest.raises(AssuranceRunStaleError):
        asyncio.run(service.run(intent, idempotency_key="run-evidence-tamper"))

    assert not any(call[0] == "commit" for call in committer.calls)


def test_final_fence_rejects_tampered_manifest_artifact(tmp_path):
    service, intent = _service(tmp_path)
    committer = service._committer
    manifest_digest = {}
    original_build_manifest = service._build_manifest

    def capture_manifest(*args):
        result = original_build_manifest(*args)
        manifest_digest["value"] = result.manifest.artifact_digest
        return result

    service._build_manifest = capture_manifest

    class _TamperingReviewer(_Reviewer):
        async def invoke(self, prompt, *, run_id, route):
            service._artifact_store._artifact_path(
                manifest_digest["value"]
            ).write_bytes(b"tampered manifest")
            return await super().invoke(prompt, run_id=run_id, route=route)

    service._reviewer_invoker = _TamperingReviewer()
    with pytest.raises(AssuranceRunStaleError):
        asyncio.run(service.run(intent, idempotency_key="run-manifest-tamper"))

    assert not any(call[0] == "commit" for call in committer.calls)
