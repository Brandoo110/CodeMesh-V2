from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from assurance.evidence_bundle import (
    BundleError,
    _read_stable,
    build_evidence_bundle,
    parse_transport_ref,
    transport_ref_for,
    verify_evidence_bundle,
)


CASE_ID = "case_fixture"
RUN_ID = "run_fixture"
SUBJECT = "sha256:" + "1" * 64
PRODUCER_HEAD = "a" * 40
TRANSPORT_HEAD = "b" * 40


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "evidence"
    artifact_root = root / "mvp-08f-remediation-post-fix-03-artifacts"
    artifact_root.mkdir(parents=True)

    payload = b"authoritative evidence\n"
    artifact_digest = _digest(payload)
    artifact_path = artifact_root / ("sha256_" + artifact_digest.removeprefix("sha256:"))
    artifact_path.write_bytes(payload)
    index = {
        "schema_version": "v1",
        "case_id": CASE_ID,
        "evidence_id": "ev_fixture",
        "evidence_kind": "command_batch",
        "artifacts": [
            {
                "schema_version": "v1",
                "digest": artifact_digest,
                "kind": "stdout",
                "label": "fixture:stdout",
                "byte_size": len(payload),
                "media_type": "text/plain",
                "integrity_status": "SHA-256 integrity verified",
                "role": "stdout",
                "path": None,
                "command_id": "fixture",
                "stream": "stdout",
            }
        ],
    }
    _write_json(artifact_root / "ev_fixture-index.json", index)
    evidence = {
        "schema_version": "v1",
        "evidence_id": "ev_fixture",
        "kind": "command_batch",
        "status": "success",
        "trust_level": "deterministic",
        "artifact_digest": artifact_digest,
        "source_ref": "fixture",
        "subject_digest": SUBJECT,
    }
    case = {
        "schema_version": "v1",
        "case_id": CASE_ID,
        "subject_digest": SUBJECT,
        "acceptance_state": "DRAFT",
        "gate": "PENDING",
        "policy_gate": {"status": "NOT_EVALUATED"},
        "freshness": {"status": "FRESH", "checked_at": "fixture"},
        "revision": 0,
        "metadata": {"run_id": RUN_ID, "risk": "low"},
        "binding": {"subject_digest": SUBJECT},
        "evidence": [evidence],
        "findings": [],
        "decisions": [],
        "timeline": [],
    }
    passport = {
        "schema": "codemesh.assurance.passport.v1",
        "case_id": CASE_ID,
        "subject_digest": SUBJECT,
        "state": "DRAFT",
        "gate": "PENDING",
        "revision": 0,
        "freshness": {"status": "FRESH", "checked_at": "fixture"},
        "binding": {"subject_digest": SUBJECT},
        "evidence": [evidence],
        "findings": [],
        "policy_decisions": [],
        "human_decisions": [],
    }
    run = {
        "schema_version": "v1",
        "cached": False,
        "case_view": {
            "schema_version": "v1",
            "case_id": CASE_ID,
            "subject_digest": SUBJECT,
            "acceptance_state": "DRAFT",
            "gate": "PENDING",
            "freshness": {"status": "FRESH", "checked_at": "fixture"},
        },
        "receipt": {
            "schema_version": "v1",
            "run_id": RUN_ID,
            "case_id": CASE_ID,
            "subject_digest": SUBJECT,
        },
    }
    _write_json(root / "mvp-08f-remediation-post-fix-03-authoritative-case.json", case)
    _write_json(root / "mvp-08f-remediation-post-fix-03-passport.json", passport)
    (root / "mvp-08f-remediation-post-fix-03-passport.md").write_text(
        "# Fixture Passport\n", encoding="utf-8"
    )
    _write_json(root / "mvp-08f-remediation-post-fix-03-response.json", run)
    return root, artifact_path, artifact_digest


def _build(tmp_path: Path):
    root, _, _ = _fixture(tmp_path)
    return build_evidence_bundle(
        root,
        case_id=CASE_ID,
        repository="acme/codemesh",
        pr_number=2,
        producer_head=PRODUCER_HEAD,
        transport_head=TRANSPORT_HEAD,
    )


def test_bundle_is_canonical_content_addressed_and_closes_objects(tmp_path: Path) -> None:
    built = _build(tmp_path)

    verified = verify_evidence_bundle(built.bundle_bytes)
    document = verified.document

    assert built.transport_ref == (
        "refs/heads/codex/evidence-v2/" + PRODUCER_HEAD + "/" + TRANSPORT_HEAD
    )
    assert built.transport_ref == transport_ref_for(
        producer_head=PRODUCER_HEAD,
        transport_head=TRANSPORT_HEAD,
    )
    assert parse_transport_ref(built.transport_ref) == (PRODUCER_HEAD, TRANSPORT_HEAD)
    assert built.bundle_digest == document["bundle_digest"]
    assert built.bundle_bytes == (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    objects = {item["digest"]: item for item in document["objects"]}
    assert set(document["object_closure"]) == set(objects)
    assert len(objects) == len(document["objects"])
    for item in objects.values():
        raw = base64.b64decode(item["data_base64"], validate=True)
        assert len(raw) == item["size"]
        assert _digest(raw) == item["digest"]


def test_bundle_rejects_legacy_single_segment_transport_ref(tmp_path: Path) -> None:
    built = _build(tmp_path)
    document = json.loads(built.bundle_bytes)
    document["transport_ref"] = "refs/heads/codex/evidence/" + PRODUCER_HEAD
    without_digest = dict(document)
    del without_digest["bundle_digest"]
    document["bundle_digest"] = _digest(
        (
            json.dumps(
                without_digest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )
    tampered = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")

    with pytest.raises(BundleError, match="transport_ref"):
        verify_evidence_bundle(tampered)


def test_bare_git_ref_probe_separates_legacy_leaf_from_evidence_v2_child(tmp_path: Path) -> None:
    bare = tmp_path / "probe.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    env = {
        **os.environ,
        "GIT_DIR": str(bare),
        "GIT_AUTHOR_NAME": "codemesh-probe",
        "GIT_AUTHOR_EMAIL": "codemesh-probe@example.invalid",
        "GIT_COMMITTER_NAME": "codemesh-probe",
        "GIT_COMMITTER_EMAIL": "codemesh-probe@example.invalid",
    }
    commit = subprocess.check_output(
        [
            "git",
            "commit-tree",
            "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
            "-m",
            "ref namespace probe",
        ],
        env=env,
        text=True,
    ).strip()
    legacy = "refs/heads/codex/evidence/" + PRODUCER_HEAD
    evidence_v2 = "refs/heads/codex/evidence-v2/" + PRODUCER_HEAD + "/" + TRANSPORT_HEAD
    legacy_child = legacy + "/" + TRANSPORT_HEAD

    for ref in (legacy, evidence_v2):
        subprocess.run(["git", "update-ref", ref, commit], env=env, check=True)
    for ref in (legacy, evidence_v2):
        subprocess.run(["git", "show-ref", "--verify", "--quiet", ref], env=env, check=True)

    conflict = subprocess.run(
        ["git", "update-ref", legacy_child, commit],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert conflict.returncode != 0
    assert "exists; cannot create" in conflict.stderr


def test_bundle_rejects_tampered_object_bytes(tmp_path: Path) -> None:
    built = _build(tmp_path)
    document = json.loads(built.bundle_bytes)
    document["objects"][0]["data_base64"] = base64.b64encode(b"tampered").decode()
    tampered = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")

    with pytest.raises(BundleError, match="digest|canonical"):
        verify_evidence_bundle(tampered)


def test_bundle_rejects_extra_unreferenced_artifact(tmp_path: Path) -> None:
    root, artifact_path, _ = _fixture(tmp_path)
    extra = artifact_path.parent / ("sha256_" + "c" * 64)
    extra.write_bytes(b"unreferenced")

    with pytest.raises(BundleError, match="extra|unreferenced"):
        build_evidence_bundle(
            root,
            case_id=CASE_ID,
            repository="acme/codemesh",
            pr_number=2,
            producer_head=PRODUCER_HEAD,
            transport_head=TRANSPORT_HEAD,
        )


def test_bundle_rejects_missing_referenced_artifact(tmp_path: Path) -> None:
    root, artifact_path, _ = _fixture(tmp_path)
    artifact_path.unlink()

    with pytest.raises(BundleError, match="missing|unreferenced"):
        build_evidence_bundle(
            root,
            case_id=CASE_ID,
            repository="acme/codemesh",
            pr_number=2,
            producer_head=PRODUCER_HEAD,
            transport_head=TRANSPORT_HEAD,
        )


def test_bundle_rejects_suspected_secret_in_artifact(tmp_path: Path) -> None:
    root, artifact_path, _ = _fixture(tmp_path)
    artifact_path.write_bytes(b"Authorization: Bearer ghp_" + b"x" * 40)

    with pytest.raises(BundleError, match="secret"):
        build_evidence_bundle(
            root,
            case_id=CASE_ID,
            repository="acme/codemesh",
            pr_number=2,
            producer_head=PRODUCER_HEAD,
            transport_head=TRANSPORT_HEAD,
        )


def test_bundle_rejects_object_over_size_limit(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)

    with pytest.raises(BundleError, match="size"):
        build_evidence_bundle(
            root,
            case_id=CASE_ID,
            repository="acme/codemesh",
            pr_number=2,
            producer_head=PRODUCER_HEAD,
            transport_head=TRANSPORT_HEAD,
            max_object_bytes=4,
        )


def test_bundle_rejects_symlinked_artifact_root(tmp_path: Path) -> None:
    root, _, _ = _fixture(tmp_path)
    artifact_root = root / "mvp-08f-remediation-post-fix-03-artifacts"
    real_root = tmp_path / "real-artifacts"
    artifact_root.rename(real_root)
    artifact_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(BundleError, match="symlink|artifact"):
        build_evidence_bundle(
            root,
            case_id=CASE_ID,
            repository="acme/codemesh",
            pr_number=2,
            producer_head=PRODUCER_HEAD,
            transport_head=TRANSPORT_HEAD,
        )


def test_bundle_rejects_file_metadata_change_during_read(tmp_path: Path, monkeypatch) -> None:
    _, artifact_path, _ = _fixture(tmp_path)
    real_fstat = os.fstat
    calls = 0

    def fake_fstat(fd):
        nonlocal calls
        calls += 1
        result = real_fstat(fd)
        if calls == 2:
            fields = list(result)
            fields[6] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "fstat", fake_fstat)
    with pytest.raises(BundleError, match="TOCTOU"):
        _read_stable(artifact_path, label="fixture artifact", max_bytes=1024)
