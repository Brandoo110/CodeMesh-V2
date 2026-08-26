"""Offline adapters for Sonar, CodeQL SARIF, Harness, and Cortex reports.

Only caller-supplied bytes are accepted. Provider claims remain declarations:
the imported Evidence and receipt always use effective trust ``declared``.
Native Sonar/CodeQL artifacts use an explicit ``caller_declared`` subject
binding because those wire shapes do not contain a CodeMesh subject digest.
The adapters produce common Evidence plus the existing policy Finding model;
they never produce an acceptance or release decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Mapping

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..artifacts import ArtifactStore
from ..contracts import Evidence, Finding


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
_MAX_TEXT_BYTES = 4096
_MAX_FINDINGS = 512
_RUBRIC_HASH = "sha256:" + hashlib.sha256(
    b"codemesh.external-tool-adapter.v1"
).hexdigest()

_Provider = Literal["sonar", "codeql", "harness", "cortex"]
_Trust = Literal[
    "declared", "observed", "deterministic", "inferred", "human_attested"
]
_EvidenceStatus = Literal["success", "failure", "error", "cancelled"]
_Severity = Literal["info", "low", "medium", "high", "critical"]
_FindingStatus = Literal[
    "open", "acknowledged", "resolved", "dismissed", "stale"
]
_ProviderFormat = Literal[
    "sonar_minimum_v1", "sarif_2.1.0", "codemesh_envelope_v1"
]
_SubjectBindingBasis = Literal["caller_declared", "embedded_declared"]


class ExternalToolImportError(Exception):
    """Base error for strict external-tool import."""


class ExternalToolPayloadError(ExternalToolImportError):
    """Raw bytes or provider report format is invalid."""


class ExternalToolSubjectMismatch(ExternalToolImportError):
    """The report is not bound to the expected change subject."""


class ExternalToolArtifactError(ExternalToolImportError):
    """The raw payload could not be persisted and verified."""


def _reject_json_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase sha256:<64 hex> digest"
        )
    return value


def _text(value: object, field_name: str, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be exactly a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field_name} is too long")
    return value


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _text(value, field_name)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class ToolFindingSummary(BaseModel):
    """The provider-neutral minimum finding extracted from a report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    provider: _Provider
    provider_finding_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    severity: _Severity
    message: str = Field(min_length=1)
    provider_status: _FindingStatus
    file_path: str | None = None
    start_line: int | None = Field(default=None, strict=True, ge=1)

    @field_validator(
        "provider_finding_id", "rule_id", "message", mode="before"
    )
    @classmethod
    def _validate_text_fields(cls, value: object, info) -> str:
        return _text(value, info.field_name)

    @field_validator("file_path", mode="before")
    @classmethod
    def _validate_file_path(cls, value: object) -> str | None:
        return _optional_text(value, "file_path")

    @field_validator("start_line", mode="before")
    @classmethod
    def _validate_start_line(cls, value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 1:
            raise ValueError("start_line must be an integer >= 1 or null")
        return value


class ExternalToolReport(BaseModel):
    """Canonical summary with an explicit, non-elevating subject binding basis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    provider: _Provider
    run_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    provider_format: _ProviderFormat
    tool_name: str = Field(min_length=1)
    tool_version: str | None = None
    project_ref: str | None = None
    subject_binding_basis: _SubjectBindingBasis
    subject_digest: str
    status: _EvidenceStatus
    collected_at: AwareDatetime
    claimed_trust_level: _Trust = "declared"
    findings: tuple[ToolFindingSummary, ...] = ()

    @field_validator("run_id", "source_ref", "tool_name", mode="before")
    @classmethod
    def _validate_identity_text(cls, value: object, info) -> str:
        return _text(value, info.field_name)

    @field_validator("subject_digest", mode="before")
    @classmethod
    def _validate_subject(cls, value: object) -> str:
        return _validate_digest(value, "subject_digest")

    @field_validator("tool_version", "project_ref", mode="before")
    @classmethod
    def _validate_optional_metadata(cls, value: object, info) -> str | None:
        return _optional_text(value, info.field_name)

    @field_validator("findings", mode="before")
    @classmethod
    def _validate_findings_tuple(cls, value: object) -> object:
        if type(value) not in (tuple, list):
            raise ValueError("findings must be an array")
        if len(value) > _MAX_FINDINGS:
            raise ValueError("too many findings")
        return value

    @model_validator(mode="after")
    def _validate_bindings(self) -> "ExternalToolReport":
        expected_source = (
            f"sonar:analysis:{self.run_id}"
            if self.provider == "sonar"
            else f"{self.provider}:run:{self.run_id}"
        )
        if self.source_ref != expected_source:
            raise ValueError("source_ref must bind provider and run_id")
        if self.provider == "sonar":
            if (
                self.provider_format != "sonar_minimum_v1"
                or self.tool_name != "Sonar"
                or self.project_ref is None
                or self.subject_binding_basis != "caller_declared"
            ):
                raise ValueError("Sonar metadata or binding basis is invalid")
        elif self.provider == "codeql":
            if (
                self.provider_format != "sarif_2.1.0"
                or self.tool_name != "CodeQL"
                or self.tool_version is None
                or self.subject_binding_basis != "caller_declared"
            ):
                raise ValueError("CodeQL metadata or binding basis is invalid")
        elif (
            self.provider_format != "codemesh_envelope_v1"
            or self.tool_name != self.provider
            or self.subject_binding_basis != "embedded_declared"
        ):
            raise ValueError("CodeMesh envelope metadata is invalid")
        provider_ids = [item.provider_finding_id for item in self.findings]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("provider finding IDs must be unique")
        if any(item.provider != self.provider for item in self.findings):
            raise ValueError("finding provider must match report provider")
        return self


class ExternalToolImportReceipt(BaseModel):
    """Verified local import metadata; publication is intentionally absent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    provider: _Provider
    run_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    provider_format: _ProviderFormat
    tool_name: str = Field(min_length=1)
    tool_version: str | None = None
    project_ref: str | None = None
    subject_binding_basis: _SubjectBindingBasis
    raw_payload_artifact_digest: str
    canonical_report_digest: str
    claimed_trust_level: _Trust
    effective_trust_level: Literal["declared"] = "declared"
    evidence_status: _EvidenceStatus
    status_semantics: Literal["analysis_execution_only"] = (
        "analysis_execution_only"
    )
    finding_count: int = Field(strict=True, ge=0, le=_MAX_FINDINGS)

    @field_validator(
        "run_id", "source_ref", "tool_name", mode="before"
    )
    @classmethod
    def _validate_text_fields(cls, value: object, info) -> str:
        return _text(value, info.field_name)

    @field_validator("tool_version", "project_ref", mode="before")
    @classmethod
    def _validate_optional_metadata(cls, value: object, info) -> str | None:
        return _optional_text(value, info.field_name)

    @field_validator(
        "raw_payload_artifact_digest", "canonical_report_digest", mode="before"
    )
    @classmethod
    def _validate_digests(cls, value: object, info) -> str:
        return _validate_digest(value, info.field_name)


def _finding_id(
    summary: ToolFindingSummary, *, evidence_id: str
) -> str:
    body = {
        "evidence_id": evidence_id,
        "summary": summary.model_dump(mode="json"),
    }
    return "fnd_ext_" + hashlib.sha256(_canonical_json_bytes(body)).hexdigest()[:32]


def _reviewer_role(provider: _Provider) -> Literal["intent", "architecture", "operability"]:
    return "architecture" if provider in {"sonar", "codeql"} else "operability"


def _finding_from_summary(
    summary: ToolFindingSummary, *, evidence_id: str, subject_digest: str
) -> Finding:
    _validate_digest(subject_digest, "subject_digest")
    return Finding(
        finding_id=_finding_id(summary, evidence_id=evidence_id),
        subject_digest=subject_digest,
        reviewer_role=_reviewer_role(summary.provider),
        claim=summary.message,
        evidence_refs=(evidence_id,),
        basis="inferred",
        severity=summary.severity,
        confidence=0.5,
        rubric_hash=_RUBRIC_HASH,
        model_ref=f"external:{summary.provider}",
        # Provider lifecycle claims are observations, not CodeMesh lifecycle
        # commands. A local action must resolve or dismiss this Finding.
        status="open",
    )


class ExternalToolResult(BaseModel):
    """Canonical external report, local receipt, Evidence, and Findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    report: ExternalToolReport
    receipt: ExternalToolImportReceipt
    evidence: Evidence
    findings: tuple[Finding, ...] = ()

    @model_validator(mode="after")
    def _validate_cross_field_bindings(self) -> "ExternalToolResult":
        report = self.report
        receipt = self.receipt
        evidence = self.evidence
        if receipt.provider != report.provider:
            raise ValueError("receipt provider must equal report provider")
        if receipt.run_id != report.run_id:
            raise ValueError("receipt run_id must equal report run_id")
        if receipt.source_ref != report.source_ref:
            raise ValueError("receipt source_ref must equal report source_ref")
        if receipt.provider_format != report.provider_format:
            raise ValueError("receipt provider_format must equal report format")
        if receipt.tool_name != report.tool_name:
            raise ValueError("receipt tool_name must equal report tool")
        if receipt.tool_version != report.tool_version:
            raise ValueError("receipt tool_version must equal report version")
        if receipt.project_ref != report.project_ref:
            raise ValueError("receipt project_ref must equal report project")
        if receipt.subject_binding_basis != report.subject_binding_basis:
            raise ValueError("receipt subject binding must equal report binding")
        if receipt.evidence_status != report.status:
            raise ValueError("receipt status must equal report status")
        if receipt.claimed_trust_level != report.claimed_trust_level:
            raise ValueError("receipt claimed trust must equal report claim")
        if receipt.effective_trust_level != "declared":
            raise ValueError("effective trust must remain declared")
        if evidence.subject_digest != report.subject_digest:
            raise ValueError("evidence subject must equal report subject")
        if evidence.kind != "external_tool_report":
            raise ValueError("evidence kind must be external_tool_report")
        if evidence.producer != f"adapter.external.{report.provider}":
            raise ValueError("evidence producer must bind provider")
        if evidence.status != report.status:
            raise ValueError("evidence status must equal report status")
        if evidence.trust_level != "declared":
            raise ValueError("external evidence trust must remain declared")
        if evidence.collected_at != report.collected_at:
            raise ValueError("evidence collected_at must equal report time")
        if evidence.trace_id is not None:
            raise ValueError("external evidence trace_id must be null")
        if evidence.artifact_digest != receipt.raw_payload_artifact_digest:
            raise ValueError("evidence artifact must equal raw payload digest")
        expected_source = (
            f"external_tool:{report.provider}:{report.subject_binding_basis}:"
            f"{report.run_id}:"
            f"{receipt.raw_payload_artifact_digest}"
        )
        if evidence.source_ref != expected_source:
            raise ValueError("evidence source_ref must bind provider and run")
        expected_canonical = _sha256_digest(_canonical_report_bytes(report))
        if receipt.canonical_report_digest != expected_canonical:
            raise ValueError("canonical_report_digest must be recomputed")
        if receipt.finding_count != len(report.findings):
            raise ValueError("finding_count must equal report findings")
        expected_evidence_id = "ev_ext_" + hashlib.sha256(
            (
                receipt.raw_payload_artifact_digest
                + receipt.canonical_report_digest
                + report.provider
                + report.run_id
            ).encode("utf-8")
        ).hexdigest()[:32]
        if evidence.evidence_id != expected_evidence_id:
            raise ValueError("evidence_id must be derived from import digests")
        if len(self.findings) != len(report.findings):
            raise ValueError("findings must equal report findings")
        expected_findings = tuple(
            _finding_from_summary(
                summary,
                evidence_id=evidence.evidence_id,
                subject_digest=report.subject_digest,
            )
            for summary in report.findings
        )
        if self.findings != expected_findings:
            raise ValueError("findings must be recomputed from report summaries")
        return self

    def verify_against_store(self, artifact_store: ArtifactStore) -> None:
        """Re-establish raw artifact existence before persistence or ingestion."""

        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        digest = self.receipt.raw_payload_artifact_digest
        try:
            if not artifact_store.verify(digest):
                raise ExternalToolArtifactError(
                    "external tool raw artifact is missing or invalid"
                )
            raw = artifact_store.get_bytes(digest)
            if _sha256_digest(raw) != digest:
                raise ExternalToolArtifactError(
                    "external tool raw artifact is missing or invalid"
                )
            replay = _parse_report(
                raw,
                provider=self.report.provider,
                expected_subject_digest=self.report.subject_digest,
            )
            if replay != self.report:
                raise ExternalToolArtifactError(
                    "external tool raw artifact does not replay to report"
                )
        except ExternalToolImportError:
            raise
        except Exception as exc:
            raise ExternalToolArtifactError(
                "external tool raw artifact is missing or invalid"
            ) from exc


def _canonical_report_bytes(report: ExternalToolReport) -> bytes:
    return _canonical_json_bytes(report.model_dump(mode="json"))


def _parse_json(payload: bytes) -> dict[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > _MAX_PAYLOAD_BYTES:
        raise ExternalToolPayloadError("invalid external tool payload")
    try:
        text = payload.decode("utf-8")
        if text.startswith("\ufeff") or "\x00" in text:
            raise ValueError("invalid text")
        value = json.loads(
            text,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, RecursionError, TypeError, ValueError):
        raise ExternalToolPayloadError("invalid external tool payload") from None
    if type(value) is not dict:
        raise ExternalToolPayloadError("external tool payload must be an object")
    return value


def _status(value: object, field_name: str) -> _EvidenceStatus:
    text = _text(value, field_name).casefold()
    mapping = {
        "success": "success",
        "succeeded": "success",
        "ok": "success",
        "failure": "failure",
        "failed": "failure",
        "error": "error",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    if text not in mapping:
        raise ExternalToolPayloadError("unknown external tool status")
    return mapping[text]  # type: ignore[return-value]


def _sonar_severity(value: object) -> _Severity:
    mapping = {
        "BLOCKER": "critical",
        "CRITICAL": "critical",
        "MAJOR": "high",
        "MINOR": "medium",
        "INFO": "info",
    }
    if type(value) is not str or value.upper() not in mapping:
        raise ExternalToolPayloadError("unknown Sonar severity")
    return mapping[value.upper()]  # type: ignore[return-value]


def _sonar_finding_status(value: object) -> _FindingStatus:
    mapping = {
        "OPEN": "open",
        "REOPENED": "open",
        "CONFIRMED": "open",
        "RESOLVED": "resolved",
        "CLOSED": "resolved",
        "FALSE-POSITIVE": "dismissed",
        "WONTFIX": "dismissed",
    }
    if type(value) is not str or value.upper() not in mapping:
        raise ExternalToolPayloadError("unknown Sonar finding status")
    return mapping[value.upper()]  # type: ignore[return-value]


def _provider_summary(
    provider: _Provider,
    item: Mapping[str, object],
    *,
    provider_finding_id: object,
    rule_id: object,
    severity: _Severity,
    message: object,
    provider_status: _FindingStatus,
    file_path: object,
    start_line: object,
) -> ToolFindingSummary:
    try:
        return ToolFindingSummary(
            provider=provider,
            provider_finding_id=provider_finding_id,
            rule_id=rule_id,
            severity=severity,
            message=message,
            provider_status=provider_status,
            file_path=file_path,
            start_line=start_line,
        )
    except ValidationError as exc:
        raise ExternalToolPayloadError("invalid external finding") from exc


def _parse_sonar(raw: dict[str, object], subject_digest: str) -> ExternalToolReport:
    allowed = {"analysisDate", "analysisId", "issues", "project", "status"}
    if set(raw) != allowed:
        raise ExternalToolPayloadError("invalid Sonar report fields")
    run_id = _text(raw["analysisId"], "analysisId")
    project_ref = _text(raw["project"], "project")
    status = _status(raw["status"], "status")
    issues = raw["issues"]
    if type(issues) is not list or len(issues) > _MAX_FINDINGS:
        raise ExternalToolPayloadError("invalid Sonar issues")
    summaries: list[ToolFindingSummary] = []
    for item in issues:
        if type(item) is not dict:
            raise ExternalToolPayloadError("invalid Sonar issue")
        allowed_issue = {"component", "key", "line", "message", "rule", "severity", "status"}
        if set(item) != allowed_issue:
            raise ExternalToolPayloadError("invalid Sonar issue fields")
        component = _text(item["component"], "component")
        file_path = component.split(":", 1)[1] if ":" in component else component
        line = item["line"]
        if type(line) is not int or line < 1:
            raise ExternalToolPayloadError("invalid Sonar issue line")
        summaries.append(
            _provider_summary(
                "sonar",
                item,
                provider_finding_id=item["key"],
                rule_id=item["rule"],
                severity=_sonar_severity(item["severity"]),
                message=item["message"],
                provider_status=_sonar_finding_status(item["status"]),
                file_path=file_path,
                start_line=line,
            )
        )
    try:
        return ExternalToolReport(
            provider="sonar",
            run_id=run_id,
            source_ref=f"sonar:analysis:{run_id}",
            provider_format="sonar_minimum_v1",
            tool_name="Sonar",
            tool_version=None,
            project_ref=project_ref,
            subject_binding_basis="caller_declared",
            subject_digest=subject_digest,
            status=status,
            collected_at=raw["analysisDate"],
            findings=tuple(sorted(summaries, key=lambda item: item.provider_finding_id)),
        )
    except ValidationError as exc:
        raise ExternalToolPayloadError("invalid Sonar report") from exc


def _codeql_severity(value: object) -> _Severity:
    mapping = {"error": "critical", "warning": "high", "note": "medium", "none": "info"}
    if type(value) is not str or value.casefold() not in mapping:
        raise ExternalToolPayloadError("unknown CodeQL level")
    return mapping[value.casefold()]  # type: ignore[return-value]


def _codeql_finding_status(value: object) -> _FindingStatus:
    if value is None:
        return "open"
    if type(value) is not str:
        raise ExternalToolPayloadError("invalid CodeQL baselineState")
    mapping = {
        "new": "open",
        "absent": "resolved",
        "updated": "open",
        "unchanged": "acknowledged",
        "existing": "acknowledged",
    }
    if value.casefold() not in mapping:
        raise ExternalToolPayloadError("unknown CodeQL baselineState")
    return mapping[value.casefold()]  # type: ignore[return-value]


def _codeql_finding_id(
    item: Mapping[str, object],
    *,
    rule_id: object,
    message: object,
    file_path: object,
    start_line: object,
) -> str:
    guid = item.get("guid")
    if guid is not None:
        return "guid:" + _text(guid, "result.guid")
    fingerprints = item.get("partialFingerprints")
    if fingerprints is not None:
        if type(fingerprints) is not dict or not fingerprints:
            raise ExternalToolPayloadError("invalid CodeQL partialFingerprints")
        for key, value in fingerprints.items():
            _text(key, "partialFingerprint key")
            _text(value, "partialFingerprint value")
        return "fingerprint:" + hashlib.sha256(
            _canonical_json_bytes(fingerprints)
        ).hexdigest()
    fallback = {
        "rule_id": _text(rule_id, "ruleId"),
        "message": _text(message, "message.text"),
        "file_path": _optional_text(file_path, "artifactLocation.uri"),
        "start_line": start_line,
    }
    return "fallback:" + hashlib.sha256(
        _canonical_json_bytes(fallback)
    ).hexdigest()


def _parse_codeql(raw: dict[str, object], subject_digest: str) -> ExternalToolReport:
    if set(raw) - {"$schema", "runs", "version"} or raw.get("version") != "2.1.0":
        raise ExternalToolPayloadError("CodeQL SARIF must be version 2.1.0")
    runs = raw.get("runs")
    if type(runs) is not list or len(runs) != 1:
        raise ExternalToolPayloadError("CodeQL SARIF must contain one run")
    run = runs[0]
    if type(run) is not dict:
        raise ExternalToolPayloadError("invalid CodeQL run")
    automation = run.get("automationDetails")
    invocations = run.get("invocations")
    results = run.get("results")
    tool = run.get("tool")
    if type(automation) is not dict or type(invocations) is not list or len(invocations) != 1:
        raise ExternalToolPayloadError("CodeQL run identity is missing")
    if type(results) is not list or len(results) > _MAX_FINDINGS:
        raise ExternalToolPayloadError("invalid CodeQL results")
    invocation = invocations[0]
    if type(invocation) is not dict or type(invocation.get("executionSuccessful")) is not bool:
        raise ExternalToolPayloadError("CodeQL invocation status is missing")
    if type(tool) is not dict or type(tool.get("driver")) is not dict:
        raise ExternalToolPayloadError("CodeQL tool identity is missing")
    driver_name = _text(tool["driver"].get("name"), "tool.driver.name")
    if driver_name.casefold() != "codeql":
        raise ExternalToolPayloadError("SARIF tool driver must be CodeQL")
    driver_version = _text(
        tool["driver"].get("semanticVersion", tool["driver"].get("version")),
        "tool.driver.version",
    )
    run_id = _text(automation.get("id"), "automationDetails.id")
    summaries: list[ToolFindingSummary] = []
    for item in results:
        if type(item) is not dict:
            raise ExternalToolPayloadError("invalid CodeQL result")
        rule_id = item.get("ruleId")
        message = item.get("message")
        if type(message) is not dict:
            raise ExternalToolPayloadError("CodeQL result message is missing")
        locations = item.get("locations", [])
        if type(locations) is not list:
            raise ExternalToolPayloadError("invalid CodeQL result locations")
        file_path = None
        start_line = None
        if locations:
            location = locations[0]
            if type(location) is not dict:
                raise ExternalToolPayloadError("invalid CodeQL location")
            physical = location.get("physicalLocation")
            if type(physical) is not dict:
                raise ExternalToolPayloadError("invalid CodeQL physical location")
            artifact = physical.get("artifactLocation")
            region = physical.get("region")
            if type(artifact) is not dict or type(region) is not dict:
                raise ExternalToolPayloadError("invalid CodeQL physical location")
            file_path = artifact.get("uri")
            start_line = region.get("startLine")
            if type(start_line) is not int or start_line < 1:
                raise ExternalToolPayloadError("invalid CodeQL startLine")
        summaries.append(
            _provider_summary(
                "codeql",
                item,
                provider_finding_id=_codeql_finding_id(
                    item,
                    rule_id=rule_id,
                    message=message.get("text"),
                    file_path=file_path,
                    start_line=start_line,
                ),
                rule_id=rule_id,
                severity=_codeql_severity(item.get("level")),
                message=message.get("text"),
                provider_status=_codeql_finding_status(
                    item.get("baselineState")
                ),
                file_path=file_path,
                start_line=start_line,
            )
        )
    try:
        return ExternalToolReport(
            provider="codeql",
            run_id=run_id,
            source_ref=f"codeql:run:{run_id}",
            provider_format="sarif_2.1.0",
            tool_name="CodeQL",
            tool_version=driver_version,
            project_ref=None,
            subject_binding_basis="caller_declared",
            subject_digest=subject_digest,
            status=("success" if invocation["executionSuccessful"] else "failure"),
            collected_at=invocation.get("endTimeUtc"),
            findings=tuple(sorted(summaries, key=lambda item: item.provider_finding_id)),
        )
    except ValidationError as exc:
        raise ExternalToolPayloadError("invalid CodeQL report") from exc


class _EnvelopeFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_id: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    severity: _Severity
    status: _FindingStatus
    file_path: str | None = None
    start_line: int | None = Field(default=None, strict=True, ge=1)

    @field_validator("finding_id", "rule_id", "claim", mode="before")
    @classmethod
    def _validate_text_fields(cls, value: object, info) -> str:
        return _text(value, info.field_name)

    @field_validator("file_path", mode="before")
    @classmethod
    def _validate_file_path(cls, value: object) -> str | None:
        return _optional_text(value, "file_path")

    @field_validator("start_line", mode="before")
    @classmethod
    def _validate_start_line(cls, value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 1:
            raise ValueError("start_line must be an integer >= 1 or null")
        return value


class _CodeMeshEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    provider: Literal["harness", "cortex"]
    run_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    subject_digest: str
    status: _EvidenceStatus
    collected_at: AwareDatetime
    claimed_trust_level: _Trust = "declared"
    findings: tuple[_EnvelopeFinding, ...] = ()

    @field_validator("run_id", "source_ref", mode="before")
    @classmethod
    def _validate_text_fields(cls, value: object, info) -> str:
        return _text(value, info.field_name)

    @field_validator("subject_digest", mode="before")
    @classmethod
    def _validate_subject(cls, value: object) -> str:
        return _validate_digest(value, "subject_digest")

    @field_validator("findings", mode="before")
    @classmethod
    def _validate_findings(cls, value: object) -> object:
        if type(value) not in (tuple, list) or len(value) > _MAX_FINDINGS:
            raise ValueError("invalid envelope findings")
        return value

    @model_validator(mode="after")
    def _validate_source(self) -> "_CodeMeshEnvelope":
        expected = f"{self.provider}:run:{self.run_id}"
        if self.source_ref != expected:
            raise ValueError("source_ref must bind provider and run_id")
        ids = [item.finding_id for item in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("envelope finding IDs must be unique")
        return self


def _parse_envelope(
    raw: dict[str, object],
    *,
    provider: Literal["harness", "cortex"],
) -> ExternalToolReport:
    if raw.get("provider") != provider:
        raise ExternalToolPayloadError("envelope provider does not match adapter")
    try:
        envelope = _CodeMeshEnvelope.model_validate(raw)
    except ValidationError as exc:
        raise ExternalToolPayloadError("invalid CodeMesh external envelope") from exc
    summaries = tuple(
        ToolFindingSummary(
            provider=provider,
            provider_finding_id=item.finding_id,
            rule_id=item.rule_id,
            severity=item.severity,
            message=item.claim,
            provider_status=item.status,
            file_path=item.file_path,
            start_line=item.start_line,
        )
        for item in envelope.findings
    )
    try:
        return ExternalToolReport(
            provider=provider,
            run_id=envelope.run_id,
            source_ref=envelope.source_ref,
            provider_format="codemesh_envelope_v1",
            tool_name=provider,
            tool_version=None,
            project_ref=None,
            subject_binding_basis="embedded_declared",
            subject_digest=envelope.subject_digest,
            status=envelope.status,
            collected_at=envelope.collected_at,
            claimed_trust_level=envelope.claimed_trust_level,
            findings=summaries,
        )
    except ValidationError as exc:
        raise ExternalToolPayloadError("invalid CodeMesh external envelope") from exc


def _parse_report(
    payload: bytes,
    *,
    provider: _Provider,
    expected_subject_digest: str,
) -> ExternalToolReport:
    raw = _parse_json(payload)
    if provider == "sonar":
        report = _parse_sonar(raw, expected_subject_digest)
    elif provider == "codeql":
        report = _parse_codeql(raw, expected_subject_digest)
    else:
        report = _parse_envelope(raw, provider=provider)  # type: ignore[arg-type]
    if report.subject_digest != expected_subject_digest:
        raise ExternalToolSubjectMismatch("external tool subject digest mismatch")
    return report


class ExternalToolAdapter:
    """Shared local importer used by the four provider-specific façades."""

    @staticmethod
    def import_bytes(
        payload: bytes,
        *,
        provider: _Provider,
        expected_subject_digest: str,
        artifact_store: ArtifactStore,
    ) -> ExternalToolResult:
        if (
            type(expected_subject_digest) is not str
            or _SHA256_DIGEST_RE.fullmatch(expected_subject_digest) is None
        ):
            raise ExternalToolPayloadError("invalid expected subject digest")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be an exact ArtifactStore")
        report = _parse_report(
            payload,
            provider=provider,
            expected_subject_digest=expected_subject_digest,
        )
        raw_digest = _sha256_digest(payload)
        try:
            stored_digest = artifact_store.put_bytes(payload)
            if stored_digest != raw_digest or not artifact_store.verify(raw_digest):
                raise ExternalToolArtifactError("external tool raw persistence failed")
            if artifact_store.get_bytes(raw_digest) != payload:
                raise ExternalToolArtifactError("external tool raw persistence failed")
        except ExternalToolImportError:
            raise
        except Exception as exc:
            raise ExternalToolArtifactError(
                "external tool raw persistence failed"
            ) from exc
        canonical_digest = _sha256_digest(_canonical_report_bytes(report))
        receipt = ExternalToolImportReceipt(
            provider=report.provider,
            run_id=report.run_id,
            source_ref=report.source_ref,
            provider_format=report.provider_format,
            tool_name=report.tool_name,
            tool_version=report.tool_version,
            project_ref=report.project_ref,
            subject_binding_basis=report.subject_binding_basis,
            raw_payload_artifact_digest=raw_digest,
            canonical_report_digest=canonical_digest,
            claimed_trust_level=report.claimed_trust_level,
            effective_trust_level="declared",
            evidence_status=report.status,
            finding_count=len(report.findings),
        )
        evidence_id = "ev_ext_" + hashlib.sha256(
            (raw_digest + canonical_digest + report.provider + report.run_id).encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        evidence = Evidence(
            evidence_id=evidence_id,
            subject_digest=report.subject_digest,
            kind="external_tool_report",
            producer=f"adapter.external.{report.provider}",
            artifact_digest=raw_digest,
            source_ref=(
                f"external_tool:{report.provider}:"
                f"{report.subject_binding_basis}:{report.run_id}:{raw_digest}"
            ),
            trace_id=None,
            status=report.status,
            trust_level="declared",
            collected_at=report.collected_at,
        )
        findings = tuple(
            _finding_from_summary(
                summary,
                evidence_id=evidence_id,
                subject_digest=report.subject_digest,
            )
            for summary in report.findings
        )
        result = ExternalToolResult(
            report=report,
            receipt=receipt,
            evidence=evidence,
            findings=findings,
        )
        result.verify_against_store(artifact_store)
        return result


class SonarAdapter:
    """Offline adapter for the minimum Sonar issue report shape."""

    @staticmethod
    def import_bytes(payload: bytes, *, expected_subject_digest: str, artifact_store: ArtifactStore) -> ExternalToolResult:
        return ExternalToolAdapter.import_bytes(
            payload,
            provider="sonar",
            expected_subject_digest=expected_subject_digest,
            artifact_store=artifact_store,
        )


class CodeQLAdapter:
    """Offline adapter for SARIF 2.1.0 CodeQL run results."""

    @staticmethod
    def import_bytes(payload: bytes, *, expected_subject_digest: str, artifact_store: ArtifactStore) -> ExternalToolResult:
        return ExternalToolAdapter.import_bytes(
            payload,
            provider="codeql",
            expected_subject_digest=expected_subject_digest,
            artifact_store=artifact_store,
        )


class HarnessAdapter:
    """Offline adapter for the strict CodeMesh Harness envelope."""

    @staticmethod
    def import_bytes(payload: bytes, *, expected_subject_digest: str, artifact_store: ArtifactStore) -> ExternalToolResult:
        return ExternalToolAdapter.import_bytes(
            payload,
            provider="harness",
            expected_subject_digest=expected_subject_digest,
            artifact_store=artifact_store,
        )


class CortexAdapter:
    """Offline adapter for the strict CodeMesh Cortex envelope."""

    @staticmethod
    def import_bytes(payload: bytes, *, expected_subject_digest: str, artifact_store: ArtifactStore) -> ExternalToolResult:
        return ExternalToolAdapter.import_bytes(
            payload,
            provider="cortex",
            expected_subject_digest=expected_subject_digest,
            artifact_store=artifact_store,
        )


SonarEvidenceAdapter = SonarAdapter
CodeQLEvidenceAdapter = CodeQLAdapter
HarnessEvidenceAdapter = HarnessAdapter
CortexEvidenceAdapter = CortexAdapter


__all__ = [
    "CodeQLAdapter",
    "CodeQLEvidenceAdapter",
    "CortexAdapter",
    "CortexEvidenceAdapter",
    "ExternalToolAdapter",
    "ExternalToolArtifactError",
    "ExternalToolImportError",
    "ExternalToolImportReceipt",
    "ExternalToolPayloadError",
    "ExternalToolReport",
    "ExternalToolResult",
    "ExternalToolSubjectMismatch",
    "HarnessAdapter",
    "HarnessEvidenceAdapter",
    "SonarAdapter",
    "SonarEvidenceAdapter",
    "ToolFindingSummary",
]
