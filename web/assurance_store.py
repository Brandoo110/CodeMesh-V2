"""Synchronous web-facing assurance repository over SQLiteAssuranceStore."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from pydantic import ValidationError
from assurance.contracts import (
    AcceptanceCase, Evidence, ExecutionReceipt, Finding,
    HumanDecision, PolicyDecision,
)
from assurance.lifecycle_store import SQLiteAssuranceLifecycleStore
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from assurance.store import CaseNotFoundError
from web.assurance_case_view import build_case_view


class AssuranceWebError(Exception):
    """Base error for the web-facing assurance repository."""


class AssuranceWebConflictError(AssuranceWebError):
    """Idempotency key reused with a different operation or payload."""


class AssuranceWebNotFoundError(AssuranceWebError):
    """Case or supplemental resource does not exist."""


_UNSET = object()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AssuranceWebRepository:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path or Path.home() / ".codemesh" / "assurance.sqlite")
        self._store = SQLiteAssuranceLifecycleStore(self._db_path)

    def initialize(self) -> None:
        self._store.initialize()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute("CREATE TABLE IF NOT EXISTS assurance_web_cases (case_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL, evidence_json TEXT NOT NULL, findings_json TEXT NOT NULL, receipt_json TEXT, updated_at TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS assurance_web_idempotency (idempotency_key TEXT PRIMARY KEY, operation TEXT NOT NULL, payload_digest TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise AssuranceWebError(f"failed to initialize assurance web tables: {exc}") from exc
        finally:
            conn.close()

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
        return [self._projection(s.case.case_id) for s in self._store.list_cases()]

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

    def _projection(self, case_id: str) -> dict:
        with self._store._transaction(write=False) as unit_of_work:
            return self._projection_in_transaction(unit_of_work, case_id)

    def _projection_in_transaction(self, unit_of_work, case_id: str) -> dict:
        state = unit_of_work.load_case(case_id)
        binding = unit_of_work.get_binding(case_id)
        decisions = unit_of_work.list_decisions(case_id)
        web = self._load_web_case_in_transaction(
            unit_of_work.connection, case_id
        )
        release_observations = (
            self._store._list_release_observations_in_transaction(
                unit_of_work.connection, case_id
            )
        )
        return self._render_projection(
            state, binding, decisions, web, release_observations
        )

    def _render_projection(
        self, state, binding, decisions, web, release_observations
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
            "decisions": decisions_out,
            "timeline": self._timeline(state, decisions, receipt),
            "revision": revision,
            "gate": gate,
            "digest_freshness": digest_freshness,
            "attention_reason": attention,
        }
        projection.update(
            build_case_view(
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
        )
        return projection

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
        return {
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
    repository = AssuranceWebRepository(db_path)
    repository.initialize()
    return repository
