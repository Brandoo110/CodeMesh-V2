import json
from datetime import datetime, timezone

import pytest

from assurance import AcceptanceBinding, AcceptanceCase
from web.assurance_demo import NEW_CASE_ID, OLD_CASE_ID, seed_assurance_demo
from web.assurance_store import AssuranceWebRepository


def test_seed_builds_two_cases_with_visible_risks_and_terminal_states(tmp_path):
    db_path = tmp_path / "assurance.sqlite"

    summary = seed_assurance_demo(db_path)

    assert summary["case_count"] == 2
    assert summary["evidence_level"] == "deterministic_offline_demo"
    assert summary["external_services"] is False
    assert [item["case_id"] for item in summary["cases"]] == [OLD_CASE_ID, NEW_CASE_ID]

    old_case, new_case = summary["cases"]
    assert old_case["state"] == "INVALIDATED"
    assert old_case["gate"] == "INVALIDATED"
    assert old_case["digest_freshness"] is True
    assert old_case["evidence_count"] == 3
    assert old_case["finding_count"] == 8
    assert old_case["receipt_count"] == 1
    assert old_case["policy_outcomes"] == ["BLOCKED"]
    assert old_case["receipt_roles"] == ["intent", "architecture", "operability"]
    assert old_case["timeline_labels"] == ["collect", "review", "conflict", "invalidate"]
    for risk in (
        "provider boundary breach",
        "hardcoded fallback",
        "side-effect retry non-idempotent",
        "no cost cap",
        "no fallback trace",
        "no kill switch",
        "no owner/ADR",
        "scope creep",
    ):
        assert risk in old_case["finding_claims"]

    assert new_case["state"] == "ACCEPTED"
    assert new_case["gate"] == "ACCEPTED"
    assert new_case["digest_freshness"] is True
    assert new_case["passport_available"] is True
    assert new_case["policy_outcomes"] == ["NEEDS_HUMAN"]
    assert new_case["receipt_roles"] == ["intent", "architecture", "operability"]
    assert new_case["human_decisions"] == ["approve"]
    assert new_case["timeline_labels"] == ["collect", "review", "accept"]
    assert old_case["subject_digest"] != new_case["subject_digest"]

    repository = AssuranceWebRepository(db_path)
    repository.initialize()
    expected_evidence = {
        "provider boundary breach": ("diff", "/diff"),
        "hardcoded fallback": ("diff", "/diff"),
        "side-effect retry non-idempotent": ("diff", "/diff"),
        "no cost cap": ("diff", "/diff"),
        "no fallback trace": ("author_agent_receipt", "/author-receipt"),
        "no kill switch": ("diff", "/diff"),
        "no owner/ADR": ("author_agent_receipt", "/author-receipt"),
        "scope creep": ("diff", "/diff"),
    }
    for case_id in (OLD_CASE_ID, NEW_CASE_ID):
        projection = repository.get_change(case_id)
        evidence_by_id = {item["evidence_id"]: item for item in projection["evidence"]}
        for finding in projection["findings"]:
            expected_kind, expected_suffix = expected_evidence[finding["claim"]]
            evidence = evidence_by_id[finding["evidence_refs"][0]]
            assert evidence["kind"] == expected_kind
            assert evidence["source_ref"].endswith(expected_suffix)


def test_seed_is_idempotent_and_deterministic_across_fresh_databases(tmp_path):
    first_db = tmp_path / "first.sqlite"
    second_db = tmp_path / "second.sqlite"

    first = seed_assurance_demo(first_db)
    replay = seed_assurance_demo(first_db)
    other = seed_assurance_demo(second_db)

    assert replay == first
    assert json.dumps(first, sort_keys=True, separators=(",", ":")) == json.dumps(
        other, sort_keys=True, separators=(",", ":")
    )
    repository = AssuranceWebRepository(first_db)
    repository.initialize()
    assert len(repository.list_changes()) == 2


def test_existing_fixed_case_with_different_content_fails_closed(tmp_path):
    db_path = tmp_path / "conflict.sqlite"
    repository = AssuranceWebRepository(db_path)
    repository.initialize()
    digest = "sha256:" + "f" * 64
    now = datetime(2026, 8, 25, 0, 0, tzinfo=timezone.utc)
    repository.create_change(
        AcceptanceCase(
            case_id=OLD_CASE_ID,
            subject_digest=digest,
            state="DRAFT",
            created_at=now,
            updated_at=now,
        ),
        AcceptanceBinding(
            subject_digest=digest,
            policy_version="wrong-policy",
            rubric_version="wrong-rubric",
        ),
        {"author": "not-the-demo"},
        "conflict:create",
        {"case_id": OLD_CASE_ID, "subject_digest": digest},
    )

    with pytest.raises(ValueError):
        seed_assurance_demo(db_path)


def test_all_persisted_resources_are_digest_bound_and_passport_is_available(tmp_path):
    db_path = tmp_path / "resources.sqlite"
    summary = seed_assurance_demo(db_path)
    repository = AssuranceWebRepository(db_path)
    repository.initialize()

    for item in summary["cases"]:
        projection = repository.get_change(item["case_id"])
        digest = projection["case"]["subject_digest"]
        assert projection["digest_freshness"] is True
        assert all(evidence["subject_digest"] == digest for evidence in projection["evidence"])
        assert all(finding["subject_digest"] == digest for finding in projection["findings"])
        assert projection["receipt"]["subject_digest"] == digest
        assert all(decision["subject_digest"] == digest for decision in projection["decisions"])
        assert all(finding["evidence_status"] == "backed" for finding in projection["findings"])

    passport = repository.get_passport(NEW_CASE_ID)
    assert passport["canonical"]["state"] == "ACCEPTED"
    assert passport["canonical"]["gate"] == "ACCEPTED"
