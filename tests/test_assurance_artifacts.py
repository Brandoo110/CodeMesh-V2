"""Focused contract tests for assurance.artifacts (V2-P1-04A)."""

import pytest

import assurance
from assurance import (
    ArtifactDigestError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from assurance import artifacts as artifact_module


ABC_DIGEST = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
ABC_HEX = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def _target(root, hex_digest=ABC_HEX):
    return root / "sha256" / hex_digest[:2] / hex_digest[2:]


def test_known_vector_and_exact_sharded_path(tmp_path):
    store = ArtifactStore(tmp_path)
    digest = store.put_bytes(b"abc")
    assert digest == ABC_DIGEST
    target = _target(tmp_path)
    assert target.read_bytes() == b"abc"
    assert not any(".tmp" in p.name for p in tmp_path.rglob("*") if p.is_file())


def test_round_trip_get_exists_verify(tmp_path):
    store = ArtifactStore(tmp_path)
    payload = b"\x00\xff artifact payload \x01\n"
    digest = store.put_bytes(payload)
    assert digest.startswith("sha256:")
    assert len(digest) == 7 + 64
    assert store.exists(digest) is True
    assert store.get_bytes(digest) == payload
    assert store.verify(digest) is True


def test_repeated_put_is_idempotent_and_does_not_rewrite(tmp_path):
    store = ArtifactStore(tmp_path)
    digest = store.put_bytes(b"abc")
    target = _target(tmp_path)
    before = target.stat().st_mtime_ns
    assert store.put_bytes(b"abc") == digest
    after = target.stat().st_mtime_ns
    assert target.read_bytes() == b"abc"
    assert before == after


@pytest.mark.parametrize(
    "payload",
    [
        bytearray(b"abc"),
        memoryview(b"abc"),
        "abc",
        123,
        None,
        [b"abc"],
        {"payload": b"abc"},
    ],
)
def test_put_bytes_rejects_non_exact_bytes(payload, tmp_path):
    store = ArtifactStore(tmp_path)
    with pytest.raises(TypeError):
        store.put_bytes(payload)
    assert not any(tmp_path.rglob("*"))


INVALID_DIGESTS = [
    "",
    "sha256:",
    "SHA256:" + ABC_HEX,
    "sha256:" + ABC_HEX.upper(),
    "sha256:" + ABC_HEX[:-1],
    "sha256:" + ABC_HEX + "0",
    "md5:" + ABC_HEX,
    " sha256:" + ABC_HEX,
    "sha256:" + ABC_HEX + "\n",
    "sha256:" + ABC_HEX[:20] + " " + ABC_HEX[20:],
    "sha256:" + ABC_HEX[:2] + "/" + ABC_HEX[2:],
    "sha256:" + ABC_HEX[:2] + "\\" + ABC_HEX[2:],
    "sha256:" + ABC_HEX[:2] + ":" + ABC_HEX[2:],
    "sha256:" + ABC_HEX[:2] + ".." + ABC_HEX[4:],
    "sha256:../" + ABC_HEX,
    "sha256:./" + ABC_HEX,
    "sha256:..",
    "sha256:." + ABC_HEX,
    ABC_HEX,
    "sha256:" + "z" * 64,
    "sha256:" + "-" * 64,
]


@pytest.mark.parametrize("digest", INVALID_DIGESTS)
def test_invalid_digests_raise_and_never_touch_disk(tmp_path, digest):
    root = tmp_path / "store"
    store = ArtifactStore(root)
    with pytest.raises(ArtifactDigestError):
        store.exists(digest)
    with pytest.raises(ArtifactDigestError):
        store.get_bytes(digest)
    with pytest.raises(ArtifactDigestError):
        store.verify(digest)
    assert not root.exists()


def test_digest_validation_precedes_filesystem_path_construction(tmp_path):
    root = tmp_path / "root-is-file"
    root.write_text("not a directory")
    store = ArtifactStore(root)
    with pytest.raises(ArtifactDigestError):
        store.exists(INVALID_DIGESTS[0])


def test_missing_artifact_get_not_found_verify_false(tmp_path):
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactNotFoundError):
        store.get_bytes(ABC_DIGEST)
    assert store.exists(ABC_DIGEST) is False
    assert store.verify(ABC_DIGEST) is False
    assert not any(tmp_path.rglob("*"))


def test_tampered_content_raises_integrity_error(tmp_path):
    store = ArtifactStore(tmp_path)
    store.put_bytes(b"abc")
    target = _target(tmp_path)
    target.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.get_bytes(ABC_DIGEST)
    with pytest.raises(ArtifactIntegrityError):
        store.verify(ABC_DIGEST)


def test_existing_corrupt_target_refused_and_preserved(tmp_path):
    store = ArtifactStore(tmp_path)
    target = _target(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    with pytest.raises(ArtifactIntegrityError):
        store.put_bytes(b"abc")
    assert target.read_bytes() == b"corrupt"
    assert not any(".tmp" in p.name for p in tmp_path.rglob("*") if p.is_file())


def test_replace_failure_propagates_and_leaves_no_target_or_temp(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    target = _target(tmp_path)

    def fail_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(artifact_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.put_bytes(b"abc")
    assert not target.exists()
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_exception_hierarchy_is_simple_and_idiomatic():
    assert issubclass(ArtifactDigestError, ValueError)
    assert issubclass(ArtifactNotFoundError, FileNotFoundError)
    assert issubclass(ArtifactIntegrityError, ValueError)


def test_artifacts_public_api_is_minimal():
    public_methods = sorted(
        name for name in vars(ArtifactStore) if not name.startswith("_")
    )
    assert public_methods == ["exists", "get_bytes", "put_bytes", "verify"]


PRIOR_PUBLIC_NAMES = [
    "AcceptanceCase",
    "ChangeSubject",
    "Evidence",
    "ExecutionReceipt",
    "ExecutionStep",
    "Finding",
    "HumanDecision",
    "PolicyDecision",
    "SubjectDigestInput",
    "canonical_subject_payload",
    "changed_subject_fields",
    "compute_normalized_diff_digest",
    "compute_subject_digest",
    "normalize_line_endings",
    "normalize_repo_path",
    "normalize_repository_identity",
    "AcceptanceEvent",
    "AcceptanceBinding",
    "AcceptanceMachineState",
    "InvalidTransitionError",
    "EventConflictError",
    "StaleSubjectError",
    "apply_acceptance_event",
    "allowed_event_kinds",
    "invalidation_reasons",
    "invalidate_if_needed",
]
NEW_PUBLIC_NAMES = {
    "ArtifactStore",
    "ArtifactDigestError",
    "ArtifactNotFoundError",
    "ArtifactIntegrityError",
}


def test_package_exports_preserve_prior_names_and_add_artifact_api():
    assert set(PRIOR_PUBLIC_NAMES) | NEW_PUBLIC_NAMES <= set(assurance.__all__)
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert assurance.ArtifactStore is artifact_module.ArtifactStore
