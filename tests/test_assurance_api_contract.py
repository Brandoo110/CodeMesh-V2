"""Focused contracts for the deterministic API-contract collector."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from assurance.api_contract import (
    CONTRACT_SOURCE_PATH,
    ApiContractCollector,
    ApiContractIntegrityError,
)
from assurance.artifacts import ArtifactStore


SUBJECT = "sha256:" + "1" * 64
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True
    )
    return result.stdout.decode().strip()


def _repository(
    tmp_path: Path,
    body: bytes = b'{"openapi":"3.0.0","paths":{}}\n',
) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "CodeMesh Test")
    (root / "contracts").mkdir()
    (root / CONTRACT_SOURCE_PATH).write_bytes(body)
    _git(root, "add", CONTRACT_SOURCE_PATH)
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _collect(
    root: Path,
    head: str,
    store: ArtifactStore,
    *,
    source_path: str | None = None,
    subject_digest: str = SUBJECT,
    collector: ApiContractCollector | None = None,
):
    return (collector or ApiContractCollector()).collect(
        root,
        subject_digest=subject_digest,
        head_revision=head,
        source_path=source_path,
        artifact_store=store,
        collected_at=NOW,
    )


def test_checked_in_contract_matches_canonical_app_openapi():
    from web.server import app

    expected = json.dumps(
        app.openapi(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    contract_path = Path(__file__).parents[1] / CONTRACT_SOURCE_PATH
    assert contract_path.read_text(encoding="utf-8") == expected


def test_real_openapi_contract_is_typed_and_content_addressed(tmp_path):
    root, head = _repository(tmp_path)
    result = _collect(root, head, ArtifactStore(tmp_path / "artifacts"))

    assert result.snapshot.complete is True
    assert result.snapshot.status == "success"
    assert result.snapshot.source_path == CONTRACT_SOURCE_PATH
    assert result.snapshot.source_digest == result.snapshot.artifact_digest
    assert result.snapshot.source_byte_size > 0
    assert result.snapshot.omissions == ()
    assert result.evidence.kind == "api_contract"
    assert result.evidence.producer == "collector.api_contract"
    assert result.evidence.subject_digest == SUBJECT
    assert result.evidence.artifact_digest == result.snapshot.artifact_digest
    assert result.evidence.status == "success"
    assert result.evidence.trust_level == "deterministic"
    assert result.evidence.collected_at == NOW

    rebound = type(result).model_validate(result.model_dump(mode="json"))
    assert rebound == result


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("missing", "source_missing"),
        ("escape", "path_escape"),
        ("intermediate_symlink", "source_symlink"),
        ("source_symlink", "source_symlink"),
        ("oversize", "oversize"),
        ("unparseable", "unparseable"),
        ("duplicate", "unparseable"),
        ("nan", "unparseable"),
        ("subject", "subject_mismatch"),
    ),
)
def test_invalid_or_incomplete_source_is_explicitly_incomplete(
    tmp_path: Path, case: str, expected: str
):
    body = b"not a contract"
    if case == "duplicate":
        body = b'{"openapi":"3.0.0","openapi":"3.0.1","paths":{}}\n'
    elif case == "nan":
        body = b'{"openapi":"3.0.0","paths":{"/x":{"x-value":NaN}}}\n'
    elif case not in ("unparseable",):
        body = b'{"openapi":"3.0.0","paths":{}}\n'
    root, head = _repository(tmp_path, body)
    store = ArtifactStore(tmp_path / "artifacts")
    collector = (
        ApiContractCollector(max_source_bytes=8)
        if case == "oversize"
        else ApiContractCollector()
    )
    source_path = None

    if case == "missing":
        (root / CONTRACT_SOURCE_PATH).unlink()
        _git(root, "commit", "-am", "remove")
        head = _git(root, "rev-parse", "HEAD")
    elif case == "escape":
        source_path = "../openapi.json"
    elif case == "intermediate_symlink":
        _git(root, "rm", "-q", "-r", "contracts")
        outside = root / "outside"
        outside.mkdir()
        (outside / "openapi.json").write_bytes(body)
        (root / "contracts").symlink_to("outside", target_is_directory=True)
        _git(root, "add", "contracts")
        _git(root, "commit", "-qm", "intermediate symlink")
        head = _git(root, "rev-parse", "HEAD")
    elif case == "source_symlink":
        _git(root, "rm", "-q", CONTRACT_SOURCE_PATH)
        (root / "outside.json").write_bytes(body)
        (root / "contracts").mkdir(exist_ok=True)
        (root / CONTRACT_SOURCE_PATH).symlink_to("../outside.json")
        _git(root, "add", CONTRACT_SOURCE_PATH, "outside.json")
        _git(root, "commit", "-qm", "source symlink")
        head = _git(root, "rev-parse", "HEAD")
    elif case == "subject":
        head = "0" * 40

    result = _collect(
        root,
        head,
        store,
        source_path=source_path,
        collector=collector,
    )
    assert result.snapshot.complete is False
    assert result.snapshot.status == "truncated"
    assert expected in result.snapshot.omissions
    assert result.evidence.status == "truncated"
    assert result.evidence.trust_level == "deterministic"


def test_tampered_artifact_digest_fails_closed(tmp_path, monkeypatch):
    root, head = _repository(tmp_path)
    store = ArtifactStore(tmp_path / "artifacts")
    original = store.put_bytes

    def tamper(data: bytes) -> str:
        original(data)
        return "sha256:" + "f" * 64

    monkeypatch.setattr(store, "put_bytes", tamper)
    with pytest.raises(ApiContractIntegrityError):
        _collect(root, head, store)


def test_repository_os_ancestor_symlink_fails_closed(tmp_path):
    root, head = _repository(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)

    result = _collect(
        alias / root.name,
        head,
        ArtifactStore(tmp_path / "artifacts"),
    )

    assert result.snapshot.status == "truncated"
    assert result.snapshot.complete is False
    assert result.snapshot.omissions == ("repository_unavailable",)
