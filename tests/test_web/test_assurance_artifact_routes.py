from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from assurance.artifacts import ArtifactStore
from assurance.contracts import AcceptanceCase, Evidence
from assurance.state_machine import AcceptanceBinding, AcceptanceEvent
from web.assurance_artifacts import AssuranceArtifactReader
from web.assurance_run_composition import AssuranceRunWebDependencies
from web.assurance_store import AssuranceWebRepository
from web.server import create_app


SUBJECT = "sha256:" + "1" * 64


def _repository(tmp_path: Path) -> AssuranceWebRepository:
    repository = AssuranceWebRepository(tmp_path / "assurance.sqlite")
    repository.initialize()
    return repository


def _add_evidence(
    repository: AssuranceWebRepository,
    *,
    case_id: str,
    evidence: Evidence,
) -> None:
    now = evidence.collected_at
    repository.create_change(
        AcceptanceCase(
            case_id=case_id,
            subject_digest=evidence.subject_digest,
            state="DRAFT",
            created_at=now,
            updated_at=now,
        ),
        AcceptanceBinding(
            subject_digest=evidence.subject_digest,
            policy_version="policy-v1",
            rubric_version="rubric-v1",
        ),
        {"author": "test", "risk": "medium"},
        f"create:{case_id}",
        {"case_id": case_id},
    )
    repository.collect(
        case_id,
        AcceptanceEvent(
            event_id=f"collect:{case_id}",
            subject_digest=evidence.subject_digest,
            kind="COLLECT_EVIDENCE",
            evidence_refs=(evidence.evidence_id,),
            occurred_at=now,
        ),
        evidence,
        f"collect:{case_id}",
        {"evidence_id": evidence.evidence_id},
    )


def _app(tmp_path: Path, store: ArtifactStore, repository: AssuranceWebRepository):
    dependencies = AssuranceRunWebDependencies(
        service=object(),
        repository=repository,
        artifact_reader=AssuranceArtifactReader(repository, store),
    )
    return create_app(assurance_run_dependencies=dependencies)


def _evidence(digest: str, *, evidence_id: str = "evidence-git") -> Evidence:
    from datetime import datetime, timezone

    return Evidence(
        evidence_id=evidence_id,
        subject_digest=SUBJECT,
        kind="git_snapshot",
        producer="collector.git",
        artifact_digest=digest,
        source_ref="test://git",
        status="success",
        trust_level="deterministic",
        collected_at=datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc),
    )


def test_artifact_routes_list_authorized_index_and_read_plaintext(tmp_path: Path):
    store = ArtifactStore(tmp_path / "private-artifacts")
    repository = _repository(tmp_path)
    data = b"diff --git a/a.txt b/a.txt\n+verified\n"
    digest = store.put_bytes(data)
    evidence = _evidence(digest)
    _add_evidence(repository, case_id="case-one", evidence=evidence)
    client = TestClient(_app(tmp_path, store, repository))

    index = client.get(
        f"/api/assurance/changes/case-one/evidence/{evidence.evidence_id}/artifacts"
    )
    assert index.status_code == 200
    assert index.json()["case_id"] == "case-one"
    assert index.json()["evidence_id"] == evidence.evidence_id
    assert [item["digest"] for item in index.json()["artifacts"]] == [digest]
    assert str(store.root) not in index.text

    response = client.get(
        f"/api/assurance/changes/case-one/evidence/{evidence.evidence_id}/artifacts/{digest}"
    )
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-artifact-digest"] == digest
    assert response.headers["x-artifact-size"] == str(len(data))


def test_artifact_routes_fail_closed_for_cross_case_unreferenced_missing_and_tampered(
    tmp_path: Path,
):
    store = ArtifactStore(tmp_path / "private-artifacts")
    repository = _repository(tmp_path)
    first_digest = store.put_bytes(b"first\n")
    second_digest = store.put_bytes(b"second\n")
    first = _evidence(first_digest, evidence_id="evidence-first")
    second = _evidence(second_digest, evidence_id="evidence-second")
    _add_evidence(repository, case_id="first", evidence=first)
    _add_evidence(repository, case_id="second", evidence=second)
    client = TestClient(_app(tmp_path, store, repository))
    base = "/api/assurance/changes/first/evidence/evidence-first/artifacts"

    requests = (
        client.get(f"{base}/{second_digest}"),
        client.get(f"{base}/sha256:{'0' * 64}"),
        client.get(f"{base}/missing-evidence"),
        client.get(
            "/api/assurance/changes/missing-case/evidence/missing-evidence/artifacts"
        ),
    )
    tampered_path = store._artifact_path(first_digest)
    tampered_path.write_bytes(b"tampered\n")
    requests += (client.get(f"{base}/{first_digest}"),)

    for response in requests:
        assert response.status_code == 404
        assert response.json() == {
            "detail": {
                "code": "ASSURANCE_NOT_FOUND",
                "message": "artifact is unavailable",
                "reason_codes": ["NOT_FOUND"],
            }
        }
        assert str(store.root) not in response.text
        assert "tampered" not in response.text
