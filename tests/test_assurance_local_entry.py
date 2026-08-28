from pathlib import Path

import pytest

from assurance.local_entry import LocalAssuranceEntry


def test_entry_wires_read_only_local_dependencies(monkeypatch, tmp_path):
    calls = {}

    class FakeChecker:
        def __init__(self, *, workspace_root):
            calls["checker"] = workspace_root

    class FakeRepository:
        def __init__(self, database, *, freshness_checker, live_required):
            calls["repository"] = (database, freshness_checker, live_required)

        def get_change(self, case_id):
            return {
                "case_id": case_id,
                "gate": "ACCEPTED",
                "freshness": {"status": "FRESH"},
                "allowed_actions": [],
            }

        def get_passport(self, case_id):
            return {"canonical": {"case_id": case_id}, "markdown": "passport"}

    class FakeArtifacts:
        def __init__(self, root):
            calls["artifacts"] = root

    monkeypatch.setattr("assurance.local_entry.LiveFreshnessChecker", FakeChecker)
    monkeypatch.setattr("assurance.local_entry.AssuranceWebRepository", FakeRepository)
    monkeypatch.setattr("assurance.local_entry.ArtifactStore", FakeArtifacts)

    database = tmp_path / "assurance.sqlite"
    database.touch()
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    entry = LocalAssuranceEntry(database, artifact_root, workspace_root)

    assert calls["checker"] == workspace_root
    assert calls["artifacts"] == artifact_root
    assert calls["repository"][0] == database
    assert calls["repository"][1].__class__ is FakeChecker
    assert calls["repository"][2] is True
    assert entry.gate("case-1")["gate"] == "ACCEPTED"
    assert entry.passport("case-1", format="markdown") == "passport"

    with entry:
        assert entry.gate("case-1")["case_id"] == "case-1"
    with pytest.raises(RuntimeError):
        entry.gate("case-1")


def test_entry_rejects_missing_persisted_paths(tmp_path):
    with pytest.raises(ValueError):
        LocalAssuranceEntry(
            tmp_path / "missing.sqlite",
            tmp_path / "artifacts",
            tmp_path,
        )
