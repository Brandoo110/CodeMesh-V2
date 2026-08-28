"""Synchronous web-facing assurance repository over SQLiteAssuranceStore."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from pydantic import ValidationError
from assurance.digests import SubjectDigestInput, compute_subject_digest
from assurance.run_service import (
    AssuranceRunBundle,
    AssuranceRunResult,
    AssuranceRunService,
)
from assurance.contracts import (
    AcceptanceCase, Evidence, ExecutionReceipt, Finding,
    HumanDecision, PolicyDecision,
)
from assurance.lifecycle_store import (
    LifecycleProjectionError,
    RemediationCommitReceipt,
    SQLiteAssuranceLifecycleStore,
)
from assurance.live_freshness import (
    FreshnessStatus,
    LiveFreshness,
    LiveFreshnessCheckerProtocol,
)
from assurance.remediation import (
    PreparedRemediationHandoff,
    RemediationController,
    RemediationRequest,
    RemediationStatus,
    ReviewerRerunReceipt,
)
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from assurance.store import (
    CaseNotFoundError,
    StoreConflictError,
    StorePersistenceError,
)
from web.assurance_case_view import apply_live_freshness, build_case_view
from web.assurance_run_committer import (
    AssuranceRunCommitter,
    AssuranceRunConflictError,
    AssuranceRunMigrationError,
    AssuranceRunPersistenceError,
    _assert_row_columns,
    _canonical_json,
    _validate_run_schema,
    _ensure_run_schema,
    _load_bundle_from_row,
    _load_pointer,
    _public_bundle_json,
    _result_pointer,
    _source_binding_json,
)


class AssuranceWebError(Exception):
    """Base error for the web-facing assurance repository."""


class AssuranceWebConflictError(AssuranceWebError):
    """Idempotency key reused with a different operation or payload."""


class AssuranceWebNotFoundError(AssuranceWebError):
    """Case or supplemental resource does not exist."""


_UNSET = object()
_LIVE_BASELINE_CORRUPT = object()
_REMEDIATION_OPERATION = "remediate"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssuranceWebRepository:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        freshness_checker: LiveFreshnessCheckerProtocol | None = None,
        live_required: bool = False,
    ) -> None:
        if type(live_required) is not bool:
            raise TypeError("live_required must be a bool")
        self._db_path = Path(db_path or Path.home() / ".codemesh" / "assurance.sqlite")
        self._freshness_checker = freshness_checker
        self._live_required = live_required
        self._store = SQLiteAssuranceLifecycleStore(self._db_path)
        self._run_committer = AssuranceRunCommitter(self)

    def initialize(self) -> None:
        self._store.initialize()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute("CREATE TABLE IF NOT EXISTS assurance_web_cases (case_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL, evidence_json TEXT NOT NULL, findings_json TEXT NOT NULL, receipt_json TEXT, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS assurance_web_idempotency (idempotency_key TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_digest TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            _ensure_run_schema(conn)
            conn.commit()
        except AssuranceRunMigrationError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise AssuranceWebError(f"failed to initialize assurance web tables: {exc}") from exc
        finally:
            conn.close()

    def lookup_run(
        self, idempotency_key: str, request_digest: str
    ) -> AssuranceRunResult | None:
        """Return the durable winner for a run key, or ``None`` before work."""

        try:
            return self._run_committer.lookup_run(idempotency_key, request_digest)
        except AssuranceRunConflictError as exc:
            raise AssuranceWebConflictError(str(exc)) from exc
        except AssuranceRunPersistenceError as exc:
            raise AssuranceWebError(str(exc)) from exc

    def commit_run(
        self,
        bundle: AssuranceRunBundle,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult:
        """Persist a complete GP-03 bundle in one short SQLite transaction."""

        try:
            return self._run_committer.commit_run(
                bundle,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
        except AssuranceRunConflictError as exc:
            raise AssuranceWebConflictError(str(exc)) from exc
        except AssuranceRunPersistenceError as exc:
            raise AssuranceWebError(str(exc)) from exc

    def commit_prepared_remediation(
        self,
        request: RemediationRequest,
        handoff: PreparedRemediationHandoff,
        *,
        idempotency_key: str,
    ) -> RemediationCommitReceipt:
        """Atomically commit one already-prepared remediation handoff.

        The handoff carries the derived rerun bundle, but the old Finding is
        deliberately reloaded from its immutable committed run inside the
        same write transaction.  No caller-supplied Case, Binding, Event,
        Finding, Receipt, or final status is accepted by this seam.
        """

        self._require_exact(request, RemediationRequest, "request")
        self._require_exact(handoff, PreparedRemediationHandoff, "handoff")
        if type(idempotency_key) is not str or not idempotency_key.strip():
            raise ValueError("idempotency_key must be nonblank")

        bundle = handoff.bundle
        if bundle is None:
            raise AssuranceWebConflictError(
                "successful remediation handoff requires its bundle"
            )
        if bundle is not None and idempotency_key == bundle.idempotency_key:
            raise AssuranceWebConflictError(
                "remediation idempotency key must differ from its run key"
            )
        payload = self._remediation_payload(request, handoff)

        try:
            with self._store._transaction() as unit_of_work:
                conn = unit_of_work.connection
                _validate_run_schema(conn)
                self._ensure_web_remediation_schema(conn)
                self._store._ensure_lifecycle_initialized(conn)

                replayed, cached = self._begin_mutation(
                    conn,
                    _REMEDIATION_OPERATION,
                    idempotency_key,
                    payload,
                )
                if replayed:
                    self._require_live_freshness_in_transaction(
                        conn, request.old_case_id, required=True
                    )
                    return self._replay_prepared_remediation_in_transaction(
                        unit_of_work,
                        request=request,
                        handoff=handoff,
                        idempotency_key=idempotency_key,
                        cached=cached,
                    )

                existing = conn.execute(
                    "SELECT remediation_id FROM assurance_remediations"
                    " WHERE remediation_id = ?",
                    (request.remediation_id,),
                ).fetchone()
                if existing is not None:
                    raise AssuranceWebConflictError(
                        f"remediation_id {request.remediation_id!r}"
                        " is already committed under another idempotency key"
                    )

                selected_finding, baseline_binding, baseline_bundle = (
                    self._load_authoritative_remediation_finding_in_transaction(
                        unit_of_work, request=request, handoff=handoff
                    )
                )
                self._require_live_freshness_in_transaction(
                    conn, request.old_case_id, required=True
                )
                expected_case, expected_binding, expected_event = (
                    self._derive_remediation_transition_in_transaction(
                        unit_of_work,
                        request=request,
                        handoff=handoff,
                        selected_finding=selected_finding,
                        baseline_binding=baseline_binding,
                        baseline_bundle=baseline_bundle,
                    )
                )
                expected_receipt = self._expected_remediation_receipt(
                    request=request,
                    handoff=handoff,
                    expected_case=expected_case,
                    expected_event=expected_event,
                )

                new_case_id = expected_case.case_id
                if self._load_web_case_in_transaction(conn, new_case_id) is not None:
                    raise AssuranceWebConflictError(
                        "new remediation case already has a web projection"
                    )

                receipt = self._store._commit_prepared_remediation_in_transaction(
                    unit_of_work,
                    request=request,
                    handoff=handoff,
                    selected_finding=selected_finding,
                )
                if receipt != expected_receipt:
                    raise AssuranceWebError(
                        "remediation lifecycle receipt is not server-derived"
                    )
                metadata = self._remediation_projection_metadata(bundle)
                self._touch_web_case(
                    conn,
                    new_case_id,
                    bundle.draft_case.updated_at,
                    metadata=metadata,
                    evidence=bundle.evidence,
                    findings=bundle.findings,
                    receipt=bundle.execution_receipt,
                )
                committed = self._run_committer._commit_run_in_transaction(
                    unit_of_work,
                    bundle,
                    idempotency_key=bundle.idempotency_key,
                    request_digest=bundle.request_digest,
                )
                if committed.cached:
                    raise AssuranceWebConflictError(
                        "remediation run unexpectedly replayed an existing row"
                    )
                self._assert_remediation_artifacts_in_transaction(
                    unit_of_work,
                    request=request,
                    handoff=handoff,
                    receipt=receipt,
                    expected_case=expected_case,
                    expected_binding=expected_binding,
                    expected_event=expected_event,
                    expected_receipt=expected_receipt,
                )
                result = receipt.model_dump(mode="json")
                self._record_mutation(
                    conn,
                    _REMEDIATION_OPERATION,
                    idempotency_key,
                    payload,
                    result,
                )
                return receipt
        except AssuranceWebError:
            raise
        except CaseNotFoundError as exc:
            raise AssuranceWebNotFoundError(str(exc)) from exc
        except (StoreConflictError, AssuranceRunConflictError) as exc:
            raise AssuranceWebConflictError(str(exc)) from exc
        except (
            LifecycleProjectionError,
            StorePersistenceError,
            AssuranceRunPersistenceError,
            AssuranceRunMigrationError,
        ) as exc:
            raise AssuranceWebError(str(exc)) from exc

    @staticmethod
    def _remediation_payload(
        request: RemediationRequest,
        handoff: PreparedRemediationHandoff,
    ) -> dict:
        """Build the complete idempotency payload without performing I/O."""

        bundle = handoff.bundle
        bundle_data = None
        if bundle is not None:
            bundle_data = bundle.model_dump(mode="json")
            # ``freshness_source_binding`` is intentionally excluded from the
            # public bundle, but it is part of the exact opaque handoff.
            bundle_data["freshness_source_binding"] = (
                bundle.freshness_source_binding.model_dump(mode="json")
            )
        return {
            "request": request.model_dump(mode="json"),
            "handoff": {
                "result": handoff.result.model_dump(mode="json"),
                "bundle": bundle_data,
            },
        }

    @staticmethod
    def _ensure_web_remediation_schema(conn: sqlite3.Connection) -> None:
        """Verify additive Web tables without creating or migrating them."""

        for table in ("assurance_web_cases", "assurance_web_idempotency"):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if row is None:
                raise AssuranceWebError(
                    f"assurance web table {table!r} is missing; call initialize()"
                )

    def _load_authoritative_remediation_finding_in_transaction(
        self,
        unit_of_work,
        *,
        request: RemediationRequest,
        handoff: PreparedRemediationHandoff,
        allow_invalidated: bool = False,
    ) -> tuple[Finding, AcceptanceBinding, AssuranceRunBundle]:
        """Read exactly one open Finding from immutable run bundles."""

        state = unit_of_work.load_case(request.old_case_id)
        if state.case.subject_digest != request.old_subject_digest:
            raise AssuranceWebConflictError(
                "remediation old subject digest does not match canonical Case"
            )
        if state.case.state == "INVALIDATED" and not allow_invalidated:
            raise AssuranceWebConflictError(
                "remediation old Case is already invalidated"
            )

        rows = unit_of_work.connection.execute(
            "SELECT * FROM assurance_web_runs WHERE case_id = ?"
            " ORDER BY committed_at ASC, run_id ASC",
            (request.old_case_id,),
        ).fetchall()
        candidates: list[Finding] = []
        baseline_binding = None
        baseline_bundle = None
        for row in rows:
            bundle = _load_bundle_from_row(row)
            _assert_row_columns(row, bundle)
            _load_pointer(unit_of_work.connection, row["idempotency_key"], bundle)
            if baseline_binding is None:
                baseline_binding = bundle.binding
                baseline_bundle = bundle
            elif bundle.binding != baseline_binding:
                raise AssuranceWebConflictError(
                    "stored remediation baseline bindings disagree"
                )
            if (
                bundle.case.case_id != request.old_case_id
                or bundle.draft_case.case_id != request.old_case_id
                or bundle.subject.subject_digest != request.old_subject_digest
            ):
                raise AssuranceWebConflictError(
                    "stored remediation baseline run is not bound to the old Case"
                )
            candidates.extend(
                finding
                for finding in bundle.findings
                if finding.finding_id == request.human_selected_finding_id
            )

        if not rows:
            raise AssuranceWebConflictError(
                "remediation old Case has no immutable committed run"
            )
        if baseline_bundle is None or baseline_binding is None:
            raise AssuranceWebConflictError(
                "remediation old Case has no authoritative baseline binding"
            )
        if len(candidates) != 1:
            raise AssuranceWebConflictError(
                "remediation selected Finding is missing or duplicated"
            )

        finding = candidates[0]
        result = handoff.result
        if (
            finding.finding_id != request.human_selected_finding_id
            or finding.subject_digest != request.old_subject_digest
            or finding.status != "open"
        ):
            raise AssuranceWebConflictError(
                "remediation selected Finding is not the requested open Finding"
            )
        if (
            result.old_case_id != request.old_case_id
            or result.old_subject_digest != request.old_subject_digest
            or result.human_selected_finding_id != finding.finding_id
            or len(result.rerun_roles) != 1
            or result.rerun_roles[0] != finding.reviewer_role
        ):
            raise AssuranceWebConflictError(
                "remediation Finding does not match the prepared rerun result"
            )
        return finding, baseline_binding, baseline_bundle

    def _derive_remediation_transition_in_transaction(
        self,
        unit_of_work,
        *,
        request: RemediationRequest,
        handoff: PreparedRemediationHandoff,
        selected_finding: Finding,
        baseline_binding: AcceptanceBinding,
        baseline_bundle: AssuranceRunBundle,
    ) -> tuple[AcceptanceCase, AcceptanceBinding, AcceptanceEvent]:
        """Derive canonical transition facts independently of nested bundle Cases."""

        bundle = handoff.bundle
        result = handoff.result
        subject_input = result.new_subject_input
        if type(subject_input) is not SubjectDigestInput:
            raise AssuranceWebConflictError(
                "remediation result has no exact new subject input"
            )
        try:
            new_subject_digest = compute_subject_digest(subject_input)
        except (TypeError, ValueError) as exc:
            raise AssuranceWebConflictError(
                "remediation new subject digest cannot be derived"
            ) from exc
        if result.new_subject_digest != new_subject_digest:
            raise AssuranceWebConflictError(
                "remediation new subject digest is not server-derived"
            )
        if baseline_binding.subject_digest != request.old_subject_digest:
            raise AssuranceWebConflictError(
                "remediation baseline binding is not bound to the old subject"
            )
        if (
            subject_input.policy_version != baseline_binding.policy_version
            or subject_input.rubric_version != baseline_binding.rubric_version
        ):
            raise AssuranceWebConflictError(
                "remediation new subject policy or rubric drifted"
            )
        if len(result.reviewer_receipts) != 1:
            raise AssuranceWebConflictError(
                "remediation must carry one reviewer provenance receipt"
            )
        reviewer_receipt = result.reviewer_receipts[0]
        if (
            reviewer_receipt.reviewer_role != selected_finding.reviewer_role
            or reviewer_receipt.reviewer.rubric_version
            != baseline_binding.rubric_version
            or reviewer_receipt.reviewer != bundle.reviewer
            or reviewer_receipt.execution_receipt != bundle.execution_receipt
        ):
            raise AssuranceWebConflictError(
                "remediation reviewer provenance is not authoritative"
            )

        subject = bundle.subject
        expected_change_id = "chg_" + hashlib.sha256(
            new_subject_digest.encode("ascii")
        ).hexdigest()[:32]
        if (
            subject.subject_digest != new_subject_digest
            or subject.change_id != expected_change_id
            or subject.repository != subject_input.repository
            or subject.base_revision != subject_input.base_revision
            or subject.head_revision != subject_input.head_revision
            or subject.task_digest != subject_input.task_digest
            or subject.policy_version != subject_input.policy_version
        ):
            raise AssuranceWebConflictError(
                "remediation bundle subject is not derived from the new subject"
            )
        expected_case = AcceptanceCase(
            schema_version="v1",
            case_id=AssuranceRunService._case_id(new_subject_digest),
            subject_digest=new_subject_digest,
            state="DRAFT",
            created_at=subject.created_at,
            updated_at=subject.created_at,
        )
        expected_binding = AcceptanceBinding(
            schema_version="v1",
            subject_digest=new_subject_digest,
            policy_version=subject_input.policy_version,
            rubric_version=baseline_binding.rubric_version,
        )
        try:
            expected_reviewer_receipt = ReviewerRerunReceipt(
                reviewer_role=selected_finding.reviewer_role,
                subject_digest=new_subject_digest,
                reviewer=bundle.reviewer,
                execution_receipt=bundle.execution_receipt,
            )
            expected_result = RemediationController._result(
                request=request,
                status=RemediationStatus.SUCCEEDED,
                reason_code="prepared_new_subject",
                attempts=result.attempts,
                validation_calls=result.validation_calls,
                attempt_receipts=list(result.attempt_receipts),
                patch_digests=list(result.patch_digests),
                last_validation=result.last_validation,
                new_subject_input=subject_input,
                new_subject_digest=new_subject_digest,
                rerun_roles=(selected_finding.reviewer_role,),
                reviewer_receipts=[expected_reviewer_receipt],
            )
        except (TypeError, ValueError) as exc:
            raise AssuranceWebConflictError(
                "remediation successful result cannot be canonically rebuilt"
            ) from exc
        if result != expected_result:
            raise AssuranceWebConflictError(
                "remediation successful result is not server-derived"
            )
        expected_event = AcceptanceEvent(
            event_id=f"remediation:{request.remediation_id}:invalidate",
            subject_digest=request.old_subject_digest,
            kind="INVALIDATE",
            reason=(
                f"remediation:{request.remediation_id}:superseded_by:"
                f"{expected_case.case_id}"
            ),
            occurred_at=expected_case.created_at,
        )
        expected_rerun_key = (
            "remediation-review:"
            + baseline_bundle.run_id
            + ":"
            + selected_finding.reviewer_role
            + ":"
            + new_subject_digest
        )
        expected_run_id = AssuranceRunService._run_id(
            bundle.request_digest, expected_rerun_key
        )
        if (
            bundle.idempotency_key != expected_rerun_key
            or bundle.run_id != expected_run_id
            or bundle.draft_case != expected_case
            or bundle.binding != expected_binding
            or bundle.case.case_id != expected_case.case_id
            or bundle.case.subject_digest != new_subject_digest
        ):
            raise AssuranceWebConflictError(
                "remediation handoff Case or run provenance is not server-derived"
            )
        return expected_case, expected_binding, expected_event

    @staticmethod
    def _expected_remediation_receipt(
        *,
        request: RemediationRequest,
        handoff: PreparedRemediationHandoff,
        expected_case: AcceptanceCase,
        expected_event: AcceptanceEvent,
    ) -> RemediationCommitReceipt:
        result_json = _canonical_json(handoff.result)
        return RemediationCommitReceipt(
            remediation_id=request.remediation_id,
            old_case_id=request.old_case_id,
            new_case_id=expected_case.case_id,
            old_subject_digest=request.old_subject_digest,
            new_subject_digest=expected_case.subject_digest,
            human_selected_finding_id=request.human_selected_finding_id,
            invalidation_event_id=expected_event.event_id,
            result_digest="sha256:"
            + hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
            committed_at=expected_event.occurred_at,
        )

    @staticmethod
    def _remediation_projection_metadata(bundle: AssuranceRunBundle) -> dict:
        source = bundle.freshness_source_binding
        return {
            "author": source.author,
            "author_provenance": source.author_provenance,
            "risk": bundle.risk.classification.risk_level,
            "run_id": bundle.run_id,
        }

    def _assert_remediation_artifacts_in_transaction(
        self,
        unit_of_work,
        *,
        request: RemediationRequest,
        handoff: PreparedRemediationHandoff,
        receipt: RemediationCommitReceipt,
        expected_case: AcceptanceCase,
        expected_binding: AcceptanceBinding,
        expected_event: AcceptanceEvent,
        expected_receipt: RemediationCommitReceipt,
        idempotency_key: str | None = None,
    ) -> None:
        """Replay every durable component before acknowledging a commit."""

        bundle = handoff.bundle
        if bundle is None:
            raise AssuranceWebError("stored remediation handoff has no bundle")
        if (
            receipt != expected_receipt
            or bundle.draft_case != expected_case
            or bundle.binding != expected_binding
        ):
            raise AssuranceWebError("remediation receipt or transition is not derived")
        conn = unit_of_work.connection
        lineage_row = conn.execute(
            "SELECT * FROM assurance_remediations WHERE remediation_id = ?",
            (request.remediation_id,),
        ).fetchone()
        if lineage_row is None:
            raise AssuranceWebError("remediation lineage row is missing")
        stored_receipt = self._store._remediation_from_row(lineage_row)
        if stored_receipt != receipt:
            raise AssuranceWebError("remediation lineage receipt does not match result")

        old_state = unit_of_work.load_case(request.old_case_id)
        if old_state.case.state != "INVALIDATED":
            raise AssuranceWebError("stored remediation old Case is not invalidated")
        if (
            not old_state.applied_events
            or old_state.applied_events[-1] != expected_event
        ):
            raise AssuranceWebError("stored remediation invalidation replay mismatch")

        new_state = unit_of_work.load_case(expected_case.case_id)
        if new_state.case != expected_case or new_state.applied_events != ():
            raise AssuranceWebError("stored remediation new DRAFT replay mismatch")
        if unit_of_work.get_binding(expected_case.case_id) != expected_binding:
            raise AssuranceWebError("stored remediation binding replay mismatch")
        if bundle.case.state not in {"EVIDENCE_COLLECTED", "NEEDS_EVIDENCE"}:
            raise AssuranceWebError("remediation run bundle is not evidence-gated")

        run_rows = conn.execute(
            "SELECT * FROM assurance_web_runs WHERE case_id = ?",
            (expected_case.case_id,),
        ).fetchall()
        if len(run_rows) != 1:
            raise AssuranceWebError("remediation must have exactly one run row")
        run_row = conn.execute(
            "SELECT * FROM assurance_web_runs WHERE idempotency_key = ?",
            (bundle.idempotency_key,),
        ).fetchone()
        if run_row is None:
            raise AssuranceWebError("remediation run row is missing")
        stored_bundle = _load_bundle_from_row(run_row)
        _assert_row_columns(run_row, stored_bundle)
        _load_pointer(conn, bundle.idempotency_key, stored_bundle)
        if stored_bundle != bundle or run_row["case_id"] != expected_case.case_id:
            raise AssuranceWebError("remediation run row does not match handoff")

        web = self._load_web_case_in_transaction(conn, expected_case.case_id)
        if web is None:
            raise AssuranceWebError("remediation web projection is missing")
        projection = self._projection_in_transaction(
            unit_of_work,
            expected_case.case_id,
            require_run_pointers=True,
            check_live=False,
        )
        if projection["case"] != expected_case.model_dump(mode="json"):
            raise AssuranceWebError("remediation projection Case is not the DRAFT case")
        if projection["evidence"] != [
            item.model_dump(mode="json") for item in bundle.evidence
        ]:
            raise AssuranceWebError("remediation projection evidence does not match run")
        if projection["receipt"] != bundle.execution_receipt.model_dump(mode="json"):
            raise AssuranceWebError("remediation projection receipt does not match run")
        if projection["decisions"]:
            raise AssuranceWebError("remediation DRAFT projection contains a decision")
        if projection["questions"] != [
            item.model_dump(mode="json") for item in bundle.questions
        ]:
            raise AssuranceWebError("remediation projection questions do not match run")

        if idempotency_key is not None:
            pointer = conn.execute(
                "SELECT operation, payload_digest, result_json"
                " FROM assurance_web_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if pointer is None:
                raise AssuranceWebError("remediation idempotency pointer is missing")
            if pointer["operation"] != _REMEDIATION_OPERATION:
                raise AssuranceWebError("remediation idempotency operation is invalid")
            if pointer["result_json"] != _canonical_json(expected_receipt):
                raise AssuranceWebError("remediation idempotency receipt is not derived")

    def _replay_prepared_remediation_in_transaction(
        self,
        unit_of_work,
        *,
        request: RemediationRequest,
        handoff: PreparedRemediationHandoff,
        idempotency_key: str,
        cached: dict | None,
    ) -> RemediationCommitReceipt:
        if not isinstance(cached, dict):
            raise AssuranceWebError("invalid cached remediation result")
        conn = unit_of_work.connection
        pointer = conn.execute(
            "SELECT operation, payload_digest, result_json"
            " FROM assurance_web_idempotency WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if pointer is None:
            raise AssuranceWebError("remediation idempotency pointer is missing")
        if pointer["operation"] != _REMEDIATION_OPERATION:
            raise AssuranceWebError("remediation idempotency operation is invalid")
        try:
            if _canonical_json(cached) != pointer["result_json"]:
                raise AssuranceWebError(
                    "remediation idempotency result is not canonical"
                )
            receipt = RemediationCommitReceipt.model_validate(cached)
        except (ValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, AssuranceWebError):
                raise
            raise AssuranceWebError("invalid cached remediation result") from exc

        selected_finding, baseline_binding, baseline_bundle = (
            self._load_authoritative_remediation_finding_in_transaction(
                unit_of_work,
                request=request,
                handoff=handoff,
                allow_invalidated=True,
            )
        )
        expected_case, expected_binding, expected_event = (
            self._derive_remediation_transition_in_transaction(
                unit_of_work,
                request=request,
                handoff=handoff,
                selected_finding=selected_finding,
                baseline_binding=baseline_binding,
                baseline_bundle=baseline_bundle,
            )
        )
        expected_receipt = self._expected_remediation_receipt(
            request=request,
            handoff=handoff,
            expected_case=expected_case,
            expected_event=expected_event,
        )
        if (
            receipt != expected_receipt
            or pointer["result_json"] != _canonical_json(expected_receipt)
            or pointer["payload_digest"]
            != self._payload_digest(self._remediation_payload(request, handoff))
        ):
            raise AssuranceWebError("remediation replay receipt is not server-derived")
        lineage_row = conn.execute(
            "SELECT * FROM assurance_remediations WHERE remediation_id = ?",
            (request.remediation_id,),
        ).fetchone()
        if lineage_row is None:
            raise AssuranceWebError("remediation idempotency result has no lineage")
        lineage_receipt = self._store._remediation_from_row(lineage_row)
        if lineage_receipt != expected_receipt:
            raise AssuranceWebError("remediation lineage receipt is not server-derived")
        stored_receipt = self._store._commit_prepared_remediation_in_transaction(
            unit_of_work,
            request=request,
            handoff=handoff,
            selected_finding=selected_finding,
        )
        if stored_receipt != expected_receipt:
            raise AssuranceWebError("cached remediation receipt does not match lineage")
        self._assert_remediation_artifacts_in_transaction(
            unit_of_work,
            request=request,
            handoff=handoff,
            receipt=receipt,
            expected_case=expected_case,
            expected_binding=expected_binding,
            expected_event=expected_event,
            expected_receipt=expected_receipt,
            idempotency_key=idempotency_key,
        )
        return receipt

    # RunCommitter's GP-02 short names make this repository directly usable as
    # the service port while the explicit *_run methods remain discoverable.
    def lookup(self, idempotency_key: str, request_digest: str):
        return self.lookup_run(idempotency_key, request_digest)

    def commit(self, bundle, *, idempotency_key: str, request_digest: str):
        return self.commit_run(
            bundle,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )

    def _lookup_run_in_transaction_boundary(
        self, idempotency_key: str, request_digest: str
    ) -> AssuranceRunResult | None:
        if type(idempotency_key) is not str or not idempotency_key.strip():
            raise ValueError("idempotency_key must be nonblank")
        if type(request_digest) is not str or not request_digest.startswith("sha256:"):
            raise ValueError("request_digest must be a sha256 digest")
        with self._store._transaction(write=False) as unit_of_work:
            conn = unit_of_work.connection
            # A read lookup must never mutate migration state.  Initialization
            # owns schema creation; this boundary only verifies it.
            _validate_run_schema(conn)
            row = conn.execute(
                "SELECT * FROM assurance_web_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            pointer = conn.execute(
                "SELECT operation, payload_digest, result_json"
                " FROM assurance_web_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                if pointer is not None:
                    if pointer["operation"] == "run":
                        raise AssuranceRunPersistenceError(
                            "run idempotency pointer exists without its run row"
                        )
                    raise AssuranceRunConflictError(
                        "idempotency key is already used by another operation"
                    )
                return None
            if row["request_digest"] != request_digest:
                raise AssuranceRunConflictError(
                    "idempotency key is bound to another request digest"
                )
            bundle = _load_bundle_from_row(row)
            _assert_row_columns(row, bundle)
            _load_pointer(conn, idempotency_key, bundle)
            self._projection_in_transaction(
                unit_of_work, bundle.case.case_id, check_live=False
            )
            return AssuranceRunResult(
                run_id=bundle.run_id,
                request_digest=bundle.request_digest,
                cached=True,
                bundle=bundle,
            )

    def _commit_run_in_transaction_boundary(
        self,
        bundle: AssuranceRunBundle,
        *,
        idempotency_key: str,
        request_digest: str,
    ) -> AssuranceRunResult:
        from web.assurance_run_committer import _validate_run_arguments

        _validate_run_arguments(bundle, idempotency_key, request_digest)
        public_json = _public_bundle_json(bundle)
        source_json = _source_binding_json(bundle.freshness_source_binding)
        if str(bundle.freshness_source_binding.repository_path) in public_json:
            raise AssuranceRunPersistenceError(
                "absolute repository path must remain in source binding only"
            )
        source_path = bundle.freshness_source_binding.repository_path
        source_error = None
        try:
            resolved_source = source_path.resolve(strict=True)
            source_stat = resolved_source.lstat()
        except (OSError, RuntimeError) as exc:
            source_error = AssuranceRunPersistenceError(
                "freshness source repository is no longer available"
            )
            source_error.__cause__ = exc
        else:
            import stat

            if (
                not source_path.is_absolute()
                or resolved_source != source_path
                or stat.S_ISLNK(source_stat.st_mode)
                or not stat.S_ISDIR(source_stat.st_mode)
            ):
                source_error = AssuranceRunPersistenceError(
                    "freshness source repository must be an existing real directory"
                )
        if source_error is not None:
            winner = self._lookup_run_in_transaction_boundary(
                idempotency_key, request_digest
            )
            if winner is not None:
                return winner
            raise source_error

        with self._store._transaction() as unit_of_work:
            conn = unit_of_work.connection
            # The repository is initialized before a commit.  Keeping the
            # write transaction validation-only avoids an implicit migration
            # competing with the idempotent run transaction.
            _validate_run_schema(conn)
            existing = conn.execute(
                "SELECT * FROM assurance_web_runs WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            pointer = conn.execute(
                "SELECT operation, payload_digest, result_json"
                " FROM assurance_web_idempotency WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise AssuranceRunConflictError(
                        "idempotency key is bound to another request digest"
                    )
                winner = _load_bundle_from_row(existing)
                _assert_row_columns(existing, winner)
                _load_pointer(conn, idempotency_key, winner)
                self._projection_in_transaction(
                    unit_of_work, winner.case.case_id, check_live=False
                )
                return AssuranceRunResult(
                    run_id=winner.run_id,
                    request_digest=winner.request_digest,
                    cached=True,
                    bundle=winner,
                )
            if pointer is not None:
                if (
                    pointer["operation"] != "run"
                    or pointer["payload_digest"] != request_digest
                ):
                    raise AssuranceRunConflictError(
                        "idempotency key is already used by another operation"
                    )
                raise AssuranceRunPersistenceError(
                    "run idempotency pointer exists without its run row"
                )

            existing_case = conn.execute(
                "SELECT case_id FROM assurance_cases WHERE case_id = ?",
                (bundle.case.case_id,),
            ).fetchone()
            subject_case = conn.execute(
                "SELECT case_id FROM assurance_cases WHERE subject_digest = ?",
                (bundle.subject.subject_digest,),
            ).fetchone()
            if existing_case is not None or subject_case is not None:
                raise AssuranceRunConflictError(
                    "deterministic case already exists for another run key"
                )

            unit_of_work.create_case(bundle.draft_case, bundle.binding)
            unit_of_work.append_policy_decision(
                bundle.case.case_id, bundle.policy.decision
            )
            for event in bundle.events:
                unit_of_work.append_event(bundle.case.case_id, event)
            replayed = unit_of_work.load_case(bundle.case.case_id)
            if replayed.case != bundle.case or replayed.applied_events != bundle.events:
                raise AssuranceRunPersistenceError(
                    "stored Case replay does not equal the complete run bundle"
                )

            source = bundle.freshness_source_binding
            metadata = {
                "author": source.author,
                "author_provenance": source.author_provenance,
                "risk": bundle.risk.classification.risk_level,
                "run_id": bundle.run_id,
            }
            self._touch_web_case(
                conn,
                bundle.case.case_id,
                bundle.case.updated_at,
                metadata=metadata,
                evidence=bundle.evidence,
                findings=bundle.findings,
                receipt=bundle.execution_receipt,
            )
            committed_at = bundle.completed_at.isoformat()
            conn.execute(
                "INSERT INTO assurance_web_runs (idempotency_key, request_digest,"
                " run_id, case_id, subject_digest, bundle_json,"
                " source_binding_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_digest,
                    bundle.run_id,
                    bundle.case.case_id,
                    bundle.subject.subject_digest,
                    public_json,
                    source_json,
                    committed_at,
                ),
            )
            projection = self._projection_in_transaction(
                unit_of_work,
                bundle.case.case_id,
                require_run_pointers=False,
                check_live=False,
            )
            if projection["case"] != bundle.case.model_dump(mode="json"):
                raise AssuranceRunPersistenceError(
                    "committed projection Case does not equal bundle Case"
                )
            if projection["receipt"] != bundle.execution_receipt.model_dump(mode="json"):
                raise AssuranceRunPersistenceError(
                    "committed projection Receipt does not equal bundle Receipt"
                )
            pointer_data = _result_pointer(
                bundle,
                bundle_json=public_json,
                source_binding_json=source_json,
            )
            conn.execute(
                "INSERT INTO assurance_web_idempotency"
                " (idempotency_key, operation, payload_digest, result_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    "run",
                    request_digest,
                    _canonical_json(pointer_data),
                    committed_at,
                ),
            )
            return AssuranceRunResult(
                run_id=bundle.run_id,
                request_digest=request_digest,
                cached=False,
                bundle=bundle,
            )

    def create_change(self, case, binding, metadata, idempotency_key, payload) -> dict:
        self._require_exact(case, AcceptanceCase, "case")
        self._require_exact(binding, AcceptanceBinding, "binding")

        def change(unit_of_work) -> None:
            state = unit_of_work.create_case(case, binding)
            self._touch_web_case(
                unit_of_work.connection,
                case.case_id,
                state.case.updated_at,
                metadata=metadata,
            )

        return self._mutate(
            "create_change",
            case.case_id,
            idempotency_key,
            payload,
            change,
        )

    def list_changes(self) -> list[dict]:
        return [
            self._projection(s.case.case_id, check_live=not self._live_required)
            for s in self._store.list_cases()
        ]

    def get_change(self, case_id: str) -> dict:
        self._require_case(case_id)
        return self._projection(case_id)

    def collect(self, case_id, event, evidence, idempotency_key, payload) -> dict:
        self._require_exact(event, AcceptanceEvent, "event")
        if evidence is not None:
            self._require_exact(evidence, Evidence, "evidence")
        stored_evidence = (evidence,) if evidence is not None else ()

        def change(unit_of_work) -> None:
            state = unit_of_work.append_event(case_id, event)
            self._touch_web_case(
                unit_of_work.connection,
                case_id,
                state.case.updated_at,
                evidence=stored_evidence,
            )

        return self._mutate(
            "collect", case_id, idempotency_key, payload, change
        )

    def review(self, case_id, event, findings, receipt, policy_decision, idempotency_key, payload) -> dict:
        self._require_exact(event, AcceptanceEvent, "event")
        self._require_exact(receipt, ExecutionReceipt, "receipt")
        self._require_exact(policy_decision, PolicyDecision, "policy_decision")
        if isinstance(findings, Finding):
            findings = (findings,)
        elif not isinstance(findings, tuple):
            findings = tuple(findings)
        for finding in findings:
            self._require_exact(finding, Finding, "finding")

        def change(unit_of_work) -> None:
            unit_of_work.append_policy_decision(case_id, policy_decision)
            state = unit_of_work.append_event(case_id, event)
            self._touch_web_case(
                unit_of_work.connection,
                case_id,
                state.case.updated_at,
                findings=findings,
                receipt=receipt,
            )

        return self._mutate(
            "review", case_id, idempotency_key, payload, change
        )

    def decide(self, case_id, human_decision, event, idempotency_key, payload) -> dict:
        self._require_exact(human_decision, HumanDecision, "human_decision")
        self._require_exact(event, AcceptanceEvent, "event")

        def change(unit_of_work) -> None:
            decisions = unit_of_work.list_decisions(case_id)
            latest_policy = next(
                (
                    decision
                    for decision in reversed(decisions)
                    if isinstance(decision, PolicyDecision)
                ),
                None,
            )
            approval = human_decision.decision != "reject"
            if approval and latest_policy is None:
                raise AssuranceWebConflictError(
                    "approval requires a policy decision"
                )
            if approval and latest_policy.outcome in {
                "BLOCKED",
                "STALE",
                "PASS_WITH_WAIVER",
            }:
                raise AssuranceWebConflictError(
                    f"policy outcome {latest_policy.outcome} does not allow approval"
                )
            if (
                approval
                and latest_policy.outcome == "NEEDS_HUMAN"
                and latest_policy.required_human_role is not None
                and human_decision.owner_role
                != latest_policy.required_human_role
            ):
                raise AssuranceWebConflictError(
                    "human decision role does not satisfy policy requirement"
                )
            if approval and event.policy_decision_refs != (
                latest_policy.decision_id,
            ):
                raise AssuranceWebConflictError(
                    "approval event must reference the latest policy decision"
                )
            self._require_live_freshness_in_transaction(
                unit_of_work.connection, case_id
            )
            unit_of_work.append_human_decision(case_id, human_decision)
            state = unit_of_work.append_event(case_id, event)
            self._touch_web_case(
                unit_of_work.connection, case_id, state.case.updated_at
            )

        return self._mutate(
            "decide", case_id, idempotency_key, payload, change
        )

    def _mutate(
        self, operation, case_id, idempotency_key, payload, change
    ) -> dict:
        mutation_payload = {"case_id": case_id, "payload": payload}
        try:
            with self._store._transaction() as unit_of_work:
                replayed, cached = self._begin_mutation(
                    unit_of_work.connection,
                    operation,
                    idempotency_key,
                    mutation_payload,
                )
                if replayed:
                    cached_case_id = cached.get("case", {}).get("case_id")
                    if cached_case_id != case_id:
                        raise AssuranceWebError(
                            "idempotency result does not match its case"
                        )
                    if operation == "decide":
                        self._require_live_freshness_in_transaction(
                            unit_of_work.connection, case_id
                        )
                    return cached
                change(unit_of_work)
                result = self._projection_in_transaction(
                    unit_of_work, case_id
                )
                self._record_mutation(
                    unit_of_work.connection,
                    operation,
                    idempotency_key,
                    mutation_payload,
                    result,
                )
                return result
        except CaseNotFoundError as exc:
            raise AssuranceWebNotFoundError(
                f"case {case_id!r} not found"
            ) from exc

    def get_evidence(self, case_id: str, evidence_id: str) -> dict:
        self._require_case(case_id)
        for item in self._projection(case_id)["evidence"]:
            if item["evidence_id"] == evidence_id:
                return item
        raise AssuranceWebNotFoundError(f"evidence {evidence_id!r} not found for case {case_id!r}")

    def get_receipt(self, case_id: str) -> dict:
        self._require_case(case_id)
        receipt = self._projection(case_id)["receipt"]
        if receipt is None:
            raise AssuranceWebNotFoundError(f"no receipt recorded for case {case_id!r}")
        return receipt

    def get_passport(self, case_id: str) -> dict:
        projection = self.get_change(case_id)
        return {"canonical": self._passport_canonical(projection), "markdown": self._passport_markdown(projection)}

    def _projection(self, case_id: str, *, check_live: bool = True) -> dict:
        with self._store._transaction(write=False) as unit_of_work:
            return self._projection_in_transaction(
                unit_of_work, case_id, check_live=check_live
            )

    def _projection_in_transaction(
        self,
        unit_of_work,
        case_id: str,
        *,
        require_run_pointers: bool = True,
        check_live: bool = True,
    ) -> dict:
        state = unit_of_work.load_case(case_id)
        binding = unit_of_work.get_binding(case_id)
        decisions = unit_of_work.list_decisions(case_id)
        web = self._load_web_case_in_transaction(
            unit_of_work.connection, case_id
        )
        try:
            runs = self._load_web_runs_in_transaction(
                unit_of_work.connection,
                case_id,
                require_pointers=require_run_pointers,
            )
        except AssuranceWebError:
            if not self._live_required:
                raise
            # A live-required projection must remain readable even when its
            # persisted run baseline is corrupt; the live overlay reports the
            # unavailable reason and never falls back to the legacy boolean.
            runs = ()
        release_observations = (
            self._store._list_release_observations_in_transaction(
                unit_of_work.connection, case_id
            )
        )
        live_baseline = None
        if self._live_required and check_live:
            live_baseline = self._load_latest_run_baseline_in_transaction(
                unit_of_work.connection,
                case_id,
                require_pointer=require_run_pointers,
            )
        if not self._live_required:
            # Preserve the original private helper call shape for explicit
            # database-only fixtures and their failure-injection tests.
            return self._render_projection(
                state, binding, decisions, web, release_observations, runs
            )
        return self._render_projection(
            state,
            binding,
            decisions,
            web,
            release_observations,
            runs,
            live_baseline=live_baseline,
            check_live=check_live,
        )

    def _load_latest_run_baseline_in_transaction(
        self,
        conn: sqlite3.Connection,
        case_id: str,
        *,
        require_pointer: bool = True,
    ):
        """Load only the latest committed run used by the live freshness fence."""

        row = conn.execute(
            "SELECT * FROM assurance_web_runs WHERE case_id = ? "
            "ORDER BY committed_at DESC, run_id DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            bundle = _load_bundle_from_row(row)
            _assert_row_columns(row, bundle)
            if require_pointer:
                _load_pointer(conn, row["idempotency_key"], bundle)
            if bundle.case.case_id != case_id:
                raise AssuranceRunPersistenceError(
                    "stored run case binding does not match projection"
                )
            return (
                bundle.freshness_source_binding,
                bundle.git.snapshot,
                bundle.intake.snapshot,
            )
        except Exception:
            return _LIVE_BASELINE_CORRUPT

    def _live_freshness_result(self, live_baseline) -> LiveFreshness:
        checked_at = datetime.now(timezone.utc)
        if self._freshness_checker is None:
            return LiveFreshness(
                status=FreshnessStatus.UNAVAILABLE,
                reason_code="NO_FRESHNESS_CHECKER",
                checked_at=checked_at,
            )
        if live_baseline is _LIVE_BASELINE_CORRUPT:
            return LiveFreshness(
                status=FreshnessStatus.UNAVAILABLE,
                reason_code="BASELINE_CORRUPT",
                checked_at=checked_at,
            )
        if live_baseline is None:
            return LiveFreshness(
                status=FreshnessStatus.UNAVAILABLE,
                reason_code="BASELINE_MISSING",
                checked_at=checked_at,
            )
        try:
            result = self._freshness_checker.check(*live_baseline)
        except Exception:
            return LiveFreshness(
                status=FreshnessStatus.UNAVAILABLE,
                reason_code="FRESHNESS_CHECK_FAILED",
                checked_at=checked_at,
            )
        if not isinstance(result, LiveFreshness):
            return LiveFreshness(
                status=FreshnessStatus.UNAVAILABLE,
                reason_code="INVALID_FRESHNESS_RESULT",
                checked_at=checked_at,
            )
        return result

    def _require_live_freshness_in_transaction(
        self,
        conn: sqlite3.Connection,
        case_id: str,
        *,
        required: bool = False,
    ) -> None:
        if not required and not self._live_required:
            return
        baseline = self._load_latest_run_baseline_in_transaction(conn, case_id)
        result = self._live_freshness_result(baseline)
        if result.status is not FreshnessStatus.FRESH:
            raise AssuranceWebConflictError(
                "live freshness check did not pass: " + result.reason_code
            )

    def _render_projection(
        self,
        state,
        binding,
        decisions,
        web,
        release_observations,
        runs=(),
        *,
        live_baseline=None,
        check_live: bool = True,
    ) -> dict:
        evidence = self._decode_models((web or {}).get("evidence_json", "[]"), Evidence)
        findings = self._decode_models((web or {}).get("findings_json", "[]"), Finding)
        receipt = self._decode_receipt(web)
        evidence_ids = {item.evidence_id for item in evidence}
        findings_out = []
        for item in findings:
            data = item.model_dump(mode="json")
            data["evidence_status"] = "backed" if all(ref in evidence_ids for ref in item.evidence_refs) else "missing"
            findings_out.append(data)
        decisions_out = []
        for decision in decisions:
            data = decision.model_dump(mode="json")
            data["kind"] = "policy" if isinstance(decision, PolicyDecision) else "human"
            decisions_out.append(data)
        gate, attention = self._gate_and_attention(state, decisions)
        metadata = json.loads(web["metadata_json"]) if web is not None else None
        questions_by_id = {}
        reviewer_runs_by_id = {}
        receipts_by_id = {}
        for bundle in runs:
            for question in bundle.questions:
                question_data = question.model_dump(mode="json")
                self._append_immutable_projection(
                    questions_by_id,
                    question_data["question_id"],
                    question_data,
                    "question",
                )
            reviewer = bundle.reviewer
            reviewer_data = {
                "run_id": bundle.run_id,
                "status": reviewer.status,
                "planned_route": reviewer.planned_route.model_dump(mode="json"),
                "rubric_version": reviewer.rubric_version,
                "prompt_id": reviewer.prompt_id,
                "prompt_digest": reviewer.prompt_digest,
                "actual_provider": reviewer.actual_provider,
                "actual_model_ref": reviewer.actual_model_ref,
                "schema_status": reviewer.schema_status,
                "raw_response_artifact_digest": reviewer.raw_response_artifact_digest,
                "canonical_response_digest": reviewer.canonical_response_digest,
                "result_id": reviewer.result_id,
                "result_digest": reviewer.result_digest,
                "usage_status": reviewer.usage_status,
                "input_tokens": reviewer.input_tokens,
                "output_tokens": reviewer.output_tokens,
                "cost_usd": reviewer.cost_usd,
                "error_code": reviewer.error_code,
            }
            self._append_immutable_projection(
                reviewer_runs_by_id, bundle.run_id, reviewer_data, "reviewer run"
            )
            receipt_data = bundle.execution_receipt.model_dump(mode="json")
            self._append_immutable_projection(
                receipts_by_id,
                receipt_data["receipt_id"],
                receipt_data,
                "receipt",
            )
        questions = list(questions_by_id.values())
        reviewer_runs = list(reviewer_runs_by_id.values())
        receipts = list(receipts_by_id.values())
        if receipts and receipt is not None and receipts[-1] != receipt.model_dump(mode="json"):
            raise AssuranceWebError(
                "legacy receipt disagrees with immutable run receipt"
            )
        if receipts and receipt is None:
            receipt = ExecutionReceipt.model_validate(receipts[-1])
        if runs:
            self._crosscheck_legacy_run_projection(
                web=web,
                metadata=metadata,
                evidence=evidence,
                findings=findings,
                receipt=receipt,
                runs=runs,
            )
        revision = len(state.applied_events) + len(decisions)
        digest_freshness = self._digest_freshness(
            state.case, binding, evidence, findings, receipt
        )
        projection = {
            "case": state.case.model_dump(mode="json"),
            "binding": binding.model_dump(mode="json"),
            "metadata": metadata,
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "findings": findings_out,
            "receipt": receipt.model_dump(mode="json") if receipt is not None else None,
            "questions": questions,
            "reviewer_runs": reviewer_runs,
            "receipts": receipts,
            "decisions": decisions_out,
            "timeline": self._timeline(state, decisions, receipt),
            "revision": revision,
            "gate": gate,
            "digest_freshness": digest_freshness,
            "attention_reason": attention,
            "freshness_mode": (
                "live_required" if self._live_required else "database_only_fixture"
            ),
        }
        case_view = build_case_view(
            case_id=state.case.case_id,
            subject_digest=state.case.subject_digest,
            revision=revision,
            acceptance_state=state.case.state,
            decisions=decisions,
            release_observations=tuple(
                item.observation for item in release_observations
            ),
            digest_freshness=digest_freshness,
            risk=(metadata or {}).get("risk"),
        )
        if self._live_required:
            if not check_live:
                live_result = LiveFreshness(
                    status=FreshnessStatus.UNAVAILABLE,
                    reason_code="FRESHNESS_NOT_CHECKED",
                    checked_at=datetime.now(timezone.utc),
                )
            else:
                live_result = self._live_freshness_result(
                    live_baseline
                )
            case_view = apply_live_freshness(case_view, live_result)
            projection["freshness"] = case_view["freshness"]
            projection["digest_freshness"] = case_view["digest_freshness"]
        projection.update(case_view)
        return projection

    def _crosscheck_legacy_run_projection(
        self,
        *,
        web,
        metadata,
        evidence,
        findings,
        receipt,
        runs,
    ) -> None:
        if web is None or metadata is None:
            raise AssuranceWebError("legacy projection is missing run metadata")
        latest = runs[-1]
        expected_metadata = {
            "author": latest.freshness_source_binding.author,
            "author_provenance": latest.freshness_source_binding.author_provenance,
            "risk": latest.risk.classification.risk_level,
            "run_id": latest.run_id,
        }
        if metadata != expected_metadata:
            raise AssuranceWebError("legacy metadata disagrees with run bundle")

        expected_evidence = {}
        expected_findings = {}
        for bundle in runs:
            for item in bundle.evidence:
                self._append_immutable_projection(
                    expected_evidence,
                    item.evidence_id,
                    item.model_dump(mode="json"),
                    "evidence",
                )
            for item in bundle.findings:
                self._append_immutable_projection(
                    expected_findings,
                    item.finding_id,
                    item.model_dump(mode="json"),
                    "finding",
                )
        actual_evidence = [item.model_dump(mode="json") for item in evidence]
        actual_findings = [item.model_dump(mode="json") for item in findings]
        if actual_evidence != list(expected_evidence.values()):
            raise AssuranceWebError("legacy evidence disagrees with run bundle")
        if actual_findings != list(expected_findings.values()):
            raise AssuranceWebError("legacy findings disagree with run bundle")
        expected_receipt = latest.execution_receipt.model_dump(mode="json")
        if receipt is None or receipt.model_dump(mode="json") != expected_receipt:
            raise AssuranceWebError("legacy receipt disagrees with run bundle")

    @staticmethod
    def _append_immutable_projection(
        destination: dict, item_id: str, value: dict, label: str
    ) -> None:
        existing = destination.get(item_id)
        if existing is not None:
            if existing != value:
                raise AssuranceWebError(
                    f"conflicting immutable {label} {item_id!r} in run projection"
                )
            return
        destination[item_id] = value

    def _timeline(self, state, decisions, receipt) -> list:
        timeline = []
        for sequence, event in enumerate(state.applied_events, start=1):
            timeline.append({"type": "event", "id": event.event_id, "sequence": sequence, "at": event.occurred_at.isoformat(), "kind": event.kind, "evidence_refs": list(event.evidence_refs), "finding_refs": list(event.finding_refs), "policy_decision_refs": list(event.policy_decision_refs), "human_decision_refs": list(event.human_decision_refs), "reason": event.reason})
        for sequence, decision in enumerate(decisions, start=1):
            is_policy = isinstance(decision, PolicyDecision)
            timeline.append({"type": "decision", "id": decision.decision_id, "sequence": sequence, "at": (decision.evaluated_at if is_policy else decision.decided_at).isoformat(), "kind": "policy" if is_policy else "human", "outcome": decision.outcome if is_policy else decision.decision})
        if receipt is not None:
            for step in receipt.steps:
                timeline.append({"type": "receipt_step", "id": f"{receipt.receipt_id}:step:{step.sequence}", "sequence": step.sequence, "at": receipt.completed_at.isoformat(), "receipt_id": receipt.receipt_id, "routing_rule": step.routing_rule, "result": step.result})
        timeline.sort(key=lambda item: (datetime.fromisoformat(item["at"]).timestamp(), item["type"], item["sequence"]))
        return timeline

    def _gate_and_attention(self, state, decisions) -> tuple:
        case_state = state.case.state
        if case_state == "INVALIDATED":
            return "INVALIDATED", state.case.invalidation_reason
        if case_state == "ACCEPTED":
            return "ACCEPTED", None
        if case_state == "CONDITIONAL_ACCEPTED":
            return "CONDITIONAL", "conditions: " + "; ".join(state.case.conditions)
        if case_state == "REJECTED":
            return "REJECTED", None
        if case_state == "NEEDS_EVIDENCE":
            return "NEEDS_EVIDENCE", "missing evidence: " + ", ".join(state.case.missing_evidence)
        if case_state == "CONFLICTED":
            return "CONFLICTED", "conflicts: " + ", ".join(state.case.conflicts)
        policy = next((d for d in reversed(decisions) if isinstance(d, PolicyDecision)), None)
        if policy is None:
            return "PENDING", "awaiting evidence" if case_state == "DRAFT" else "awaiting policy evaluation"
        if policy.outcome in ("STALE", "BLOCKED"):
            return policy.outcome, f"policy {policy.outcome.lower()}: " + ", ".join(policy.reason_codes)
        if policy.outcome == "NEEDS_HUMAN":
            return policy.outcome, f"needs human decision ({policy.required_human_role})"
        if policy.outcome == "PASS_WITH_WAIVER":
            return policy.outcome, f"passed with waiver {policy.waiver_ref}"
        return policy.outcome, "awaiting human decision" if case_state == "EVIDENCE_COLLECTED" else None

    def _digest_freshness(self, case, binding, evidence, findings, receipt) -> bool:
        expected = case.subject_digest
        return (binding.subject_digest == expected and all(i.subject_digest == expected for i in evidence) and all(i.subject_digest == expected for i in findings) and (receipt is None or receipt.subject_digest == expected))

    def _passport_canonical(self, p) -> dict:
        case, binding = p["case"], p["binding"]
        canonical = {
            "schema": "codemesh.assurance.passport.v1",
            "case_id": case["case_id"], "subject_digest": case["subject_digest"],
            "state": case["state"], "gate": p["gate"], "revision": p["revision"], "updated_at": case["updated_at"],
            "binding": {k: binding[k] for k in ("policy_version", "rubric_version", "waiver_id", "waiver_expires_at")},
            "evidence": [{k: e[k] for k in ("evidence_id", "kind", "status", "trust_level", "artifact_digest", "source_ref", "collected_at")} for e in p["evidence"]],
            "findings": [{"finding_id": f["finding_id"], "severity": f["severity"], "status": f["status"], "basis": f["basis"], "confidence": f["confidence"], "evidence_status": f["evidence_status"], "evidence_refs": list(f["evidence_refs"])} for f in p["findings"]],
            "receipt": ({"receipt_id": p["receipt"]["receipt_id"], "run_id": p["receipt"]["run_id"], "overall_result": p["receipt"]["overall_result"], "steps": [{k: s[k] for k in ("sequence", "planned_role", "actual_role", "routing_rule", "result", "schema_status")} for s in p["receipt"]["steps"]]} if p["receipt"] is not None else None),
            "policy_decisions": [{k: d[k] for k in ("decision_id", "outcome", "required_human_role", "evaluated_at")} | {"reason_codes": list(d["reason_codes"])} for d in p["decisions"] if d["kind"] == "policy"],
            "human_decisions": [{k: d[k] for k in ("decision_id", "decision", "owner", "owner_role", "decided_at", "waiver_id")} | {"conditions": list(d["conditions"])} for d in p["decisions"] if d["kind"] == "human"],
            "missing_evidence": list(case["missing_evidence"]), "conditions": list(case["conditions"]), "conflicts": list(case["conflicts"]),
            "unverified_labels": self._unverified_labels(p), "invalidation_reason": case["invalidation_reason"],
        }
        if p.get("freshness") is not None:
            canonical["freshness"] = p["freshness"]
        return canonical

    def _passport_markdown(self, p) -> str:
        case, binding = p["case"], p["binding"]
        lines = [f"# Assurance Passport: {case['case_id']}", "", f"- State: **{case['state']}**", f"- Gate: **{p['gate']}**", f"- Revision: {p['revision']}", f"- Updated: {case['updated_at']}", f"- Subject: `{case['subject_digest'][:32]}...`", "", "## Binding", f"- Policy: {binding['policy_version']} / Rubric: {binding['rubric_version']}"]
        if binding.get("waiver_id"):
            lines.append(f"- Waiver: {binding['waiver_id']} (expires {binding['waiver_expires_at']})")
        lines += ["", "## Evidence"] + ([f"- `{e['evidence_id']}` {e['kind']} ({e['status']}, {e['trust_level']}) — {e['artifact_digest'][:16]}…" for e in p["evidence"]] or ["- _none_"])
        lines += ["", "## Findings"] + ([f"- `{f['finding_id']}` [{f['severity']}/{f['status']}] {f['evidence_status']} refs={','.join(f['evidence_refs'])}" for f in p["findings"]] or ["- _none_"])
        lines += ["", "## Decisions"] + ([f"- `{d['decision_id']}` {d['kind']}: {d['outcome'] if d['kind'] == 'policy' else d['decision']}" for d in p["decisions"]] or ["- _none_"])
        lines += ["", "## Missing / Conditions / Conflicts"]
        if not (case["missing_evidence"] or case["conditions"] or case["conflicts"]):
            lines.append("- _none_")
        if case["missing_evidence"]:
            lines.append("- Missing: " + ", ".join(case["missing_evidence"]))
        if case["conditions"]:
            lines.append("- Conditions: " + "; ".join(case["conditions"]))
        if case["conflicts"]:
            lines.append("- Conflicts: " + "; ".join(case["conflicts"]))
        freshness = p.get("freshness")
        if freshness is not None:
            lines += ["", "## Live Freshness", f"- Status: **{freshness['status']}**", f"- Reason: `{freshness['reason_code']}`"]
        if case["invalidation_reason"]:
            lines.append(f"- Invalidation: {case['invalidation_reason']}")
        unverified = self._unverified_labels(p)
        if unverified:
            lines.append("- Unverified: " + "; ".join(unverified))
        return "\n".join(lines)

    def _unverified_labels(self, p) -> list:
        labels = [f"UNVERIFIED:{e['evidence_id']}:{e['trust_level']}" for e in p["evidence"] if e["trust_level"] not in ("deterministic", "human_attested")]
        labels += [f"MISSING:{m}" for m in p["case"]["missing_evidence"]]
        return labels

    def _begin_mutation(
        self, conn, operation, idempotency_key, payload
    ) -> tuple[bool, dict | None]:
        try:
            row = conn.execute("SELECT operation, payload_digest, result_json FROM assurance_web_idempotency WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if row is None:
                return False, None
            if row["operation"] != operation or row["payload_digest"] != self._payload_digest(payload):
                raise AssuranceWebConflictError(f"idempotency key {idempotency_key!r} was reused with a different operation or payload")
            try:
                cached = json.loads(row["result_json"])
            except (json.JSONDecodeError, TypeError) as exc:
                raise AssuranceWebError(
                    f"invalid cached idempotency result for {idempotency_key!r}"
                ) from exc
            if not isinstance(cached, dict):
                raise AssuranceWebError(
                    f"invalid cached idempotency result for {idempotency_key!r}"
                )
            return True, cached
        except sqlite3.Error as exc:
            raise AssuranceWebError(f"idempotency check failed for {idempotency_key!r}: {exc}") from exc

    def _record_mutation(
        self, conn, operation, idempotency_key, payload, result
    ) -> None:
        try:
            conn.execute("INSERT INTO assurance_web_idempotency (idempotency_key, operation, payload_digest, result_json, created_at) VALUES (?, ?, ?, ?, ?)", (idempotency_key, operation, self._payload_digest(payload), json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")), _now_iso()))
        except sqlite3.IntegrityError as exc:
            raise AssuranceWebConflictError(f"idempotency key {idempotency_key!r} was created concurrently") from exc
        except sqlite3.Error as exc:
            raise AssuranceWebError(f"failed to record idempotency result for {idempotency_key!r}: {exc}") from exc

    def _payload_digest(self, payload) -> str:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _touch_web_case(
        self,
        conn,
        case_id,
        updated_at,
        *,
        metadata=_UNSET,
        evidence=(),
        findings=(),
        receipt=_UNSET,
    ) -> None:
        try:
            row = conn.execute("SELECT metadata_json, evidence_json, findings_json, receipt_json FROM assurance_web_cases WHERE case_id = ?", (case_id,)).fetchone()
            data = {"metadata_json": "{}", "evidence_json": "[]", "findings_json": "[]", "receipt_json": None}
            if row is not None:
                data = dict(row)
            if metadata is not _UNSET:
                data["metadata_json"] = json.dumps(metadata, ensure_ascii=False)
            stored_evidence = json.loads(data["evidence_json"])
            if evidence:
                stored_evidence = self._merge_by_id(stored_evidence, evidence, "evidence_id")
            stored_findings = json.loads(data["findings_json"])
            if findings:
                stored_findings = self._merge_by_id(stored_findings, findings, "finding_id")
            data["evidence_json"] = json.dumps(stored_evidence, ensure_ascii=False)
            data["findings_json"] = json.dumps(stored_findings, ensure_ascii=False)
            if receipt is not _UNSET:
                data["receipt_json"] = json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False) if receipt is not None else None
            updated_at_iso = updated_at.isoformat()
            if row is None:
                conn.execute("INSERT INTO assurance_web_cases (case_id, metadata_json, evidence_json, findings_json, receipt_json, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (case_id, data["metadata_json"], data["evidence_json"], data["findings_json"], data["receipt_json"], updated_at_iso))
            else:
                conn.execute("UPDATE assurance_web_cases SET metadata_json = ?, evidence_json = ?, findings_json = ?, receipt_json = ?, updated_at = ? WHERE case_id = ?", (data["metadata_json"], data["evidence_json"], data["findings_json"], data["receipt_json"], updated_at_iso, case_id))
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise AssuranceWebError(f"failed to update web case {case_id!r}: {exc}") from exc

    def _merge_by_id(self, stored, new_models, id_field) -> list:
        result = list(stored)
        index = {item[id_field]: i for i, item in enumerate(result)}
        for model in new_models:
            data = model.model_dump(mode="json")
            if data[id_field] in index:
                result[index[data[id_field]]] = data
            else:
                index[data[id_field]] = len(result)
                result.append(data)
        return result

    def _load_web_case_in_transaction(self, conn, case_id) -> dict | None:
        row = conn.execute("SELECT metadata_json, evidence_json, findings_json, receipt_json FROM assurance_web_cases WHERE case_id = ?", (case_id,)).fetchone()
        return dict(row) if row is not None else None

    def _load_web_runs_in_transaction(
        self, conn, case_id, *, require_pointers: bool = True
    ) -> tuple:
        rows = conn.execute(
            "SELECT * FROM assurance_web_runs WHERE case_id = ?"
            " ORDER BY committed_at ASC, run_id ASC",
            (case_id,),
        ).fetchall()
        bundles = []
        for row in rows:
            try:
                bundle = _load_bundle_from_row(row)
                _assert_row_columns(row, bundle)
                if require_pointers:
                    _load_pointer(conn, row["idempotency_key"], bundle)
            except AssuranceRunPersistenceError as exc:
                raise AssuranceWebError(str(exc)) from exc
            if bundle.case.case_id != case_id:
                raise AssuranceWebError(
                    "stored run case binding does not match projection"
                )
            bundles.append(bundle)
        return tuple(bundles)

    def _decode_models(self, raw, model_cls) -> tuple:
        try:
            return tuple(
                model_cls.model_validate_json(
                    json.dumps(item, ensure_ascii=False)
                )
                for item in json.loads(raw)
            )
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise AssuranceWebError(f"invalid stored {model_cls.__name__} JSON") from exc

    def _decode_receipt(self, web):
        if web is None or not web["receipt_json"]:
            return None
        try:
            return ExecutionReceipt.model_validate_json(web["receipt_json"])
        except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
            raise AssuranceWebError("invalid stored receipt JSON") from exc

    def _require_case(self, case_id) -> None:
        try:
            self._store.load_case(case_id)
        except CaseNotFoundError as exc:
            raise AssuranceWebNotFoundError(f"case {case_id!r} not found") from exc

    def _require_exact(self, obj, model_cls, name) -> None:
        if type(obj) is not model_cls:
            raise TypeError(f"{name} must be an exact {model_cls.__name__}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


@lru_cache(maxsize=1)
def get_assurance_repository(db_path: Path | None = None) -> AssuranceWebRepository:
    # This is a product dependency factory.  Without an explicitly composed
    # checker it must fail closed instead of silently exposing the old
    # database-only freshness boolean.  Tests/fixtures construct the
    # repository directly with the default ``live_required=False`` mode.
    raise AssuranceWebError("assurance repository requires explicit live composition")
