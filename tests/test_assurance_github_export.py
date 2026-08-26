"""Focused offline contract tests for the V2-P8-01A GitHub exporter."""

import ast
import hashlib
import inspect
import json

import pytest

from assurance.integrations.github import (
    GitHubAnnotation,
    GitHubAnnotationError,
    GitHubCheckPayload,
    GitHubCommentPayload,
    GitHubExportError,
    GitHubExportReceipt,
    GitHubExportResult,
    GitHubExporter,
    GitHubTarget,
    canonical_passport_digest,
)


SUBJECT = "sha256:" + "1" * 64


def _passport(*, gate: str = "ACCEPTED", state: str = "ACCEPTED") -> dict[str, object]:
    return {
        "schema": "codemesh.assurance.passport.v1",
        "case_id": "case-017",
        "subject_digest": SUBJECT,
        "state": state,
        "gate": gate,
        "revision": 7,
        "updated_at": "2026-08-26T12:00:00Z",
        "evidence": [
            {
                "evidence_id": "ev-1",
                "kind": "ci_run",
                "status": "success",
                "trust_level": "declared",
            }
        ],
        "findings": [
            {
                "finding_id": "finding-1",
                "severity": "low",
                "status": "open",
            }
        ],
        "policy_decisions": [
            {
                "decision_id": "decision-1",
                "outcome": "allow",
            }
        ],
        "human_decisions": [],
    }


def _target() -> GitHubTarget:
    return GitHubTarget(
        owner="acme",
        repo="widget",
        head_sha="a" * 40,
        pr_number=42,
    )


def test_contract_models_are_frozen_and_forbid_extra_fields():
    for model in (
        GitHubTarget,
        GitHubAnnotation,
        GitHubCheckPayload,
        GitHubCommentPayload,
        GitHubExportReceipt,
        GitHubExportResult,
    ):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_export_builds_offline_check_and_comment_endpoints():
    result = GitHubExporter.export(_passport(), _target())

    assert result.check.endpoint == "/repos/acme/widget/check-runs"
    assert result.comment.endpoint == "/repos/acme/widget/issues/42/comments"
    assert result.check.body["name"] == "CodeMesh Change Assurance"
    assert result.check.body["head_sha"] == _target().head_sha
    assert result.check.body["status"] == "completed"
    assert result.check.body["conclusion"] == "success"
    assert result.receipt.published is False
    assert (
        result.receipt.required_auth
        == "GitHub App checks:write / issue comment permission"
    )
    assert "token" not in json.dumps(result.model_dump(mode="json")).lower()


@pytest.mark.parametrize(
    ("gate", "expected"),
    [
        ("ACCEPTED", "success"),
        ("PASS", "success"),
        ("INVALIDATED", "stale"),
        ("STALE", "stale"),
        ("REJECTED", "failure"),
        ("BLOCKED", "failure"),
        ("UNKNOWN", "action_required"),
    ],
)
def test_gate_mapping_is_explicit_and_fail_closed(gate: str, expected: str):
    result = GitHubExporter.export(_passport(gate=gate), _target())

    assert result.check.body["conclusion"] == expected


def test_unknown_gate_never_maps_to_success():
    result = GitHubExporter.export(_passport(gate="new-provider-state"), _target())

    assert result.check.body["conclusion"] != "success"
    assert result.check.body["conclusion"] == "action_required"


def test_comment_has_stable_marker_and_assurance_summaries():
    result = GitHubExporter.export(_passport(), _target())
    body = result.comment.body["body"]

    assert "<!-- codemesh-change-assurance:case-017:sha256:" in body
    assert "Evidence" in body
    assert "Findings" in body
    assert "Decisions" in body
    assert "offline payload, not published" in body


def test_passport_digest_is_canonical_and_stable():
    first = _passport()
    second = dict(reversed(list(first.items())))
    assert canonical_passport_digest(first) == canonical_passport_digest(second)

    first_result = GitHubExporter.export(first, _target())
    second_result = GitHubExporter.export(second, _target())
    assert first_result.receipt.passport_digest == second_result.receipt.passport_digest
    assert first_result.model_dump(mode="json") == second_result.model_dump(mode="json")


def test_annotations_require_explicit_positions_and_allow_fifty():
    annotations = tuple(
        GitHubAnnotation(
            path="src/app.py",
            start_line=index + 1,
            end_line=index + 1,
            level="warning",
            message=f"finding {index}",
        )
        for index in range(50)
    )
    result = GitHubExporter.export(_passport(), _target(), annotations=annotations)
    output = result.check.body["output"]
    assert len(output["annotations"]) == 50
    assert output["annotations"][0]["path"] == "src/app.py"

    without_annotations = GitHubExporter.export(_passport(), _target())
    assert without_annotations.check.body["output"]["annotations"] == []


def test_more_than_fifty_annotations_is_rejected():
    annotations = tuple(
        GitHubAnnotation(
            path="src/app.py",
            start_line=index + 1,
            end_line=index + 1,
            level="notice",
            message="too many",
        )
        for index in range(51)
    )

    with pytest.raises(GitHubAnnotationError):
        GitHubExporter.export(_passport(), _target(), annotations=annotations)


@pytest.mark.parametrize(
    "missing",
    ["case_id", "subject_digest", "state", "gate", "revision"],
)
def test_required_passport_binding_fields_are_required(missing: str):
    passport = _passport()
    del passport[missing]

    with pytest.raises(GitHubExportError):
        GitHubExporter.export(passport, _target())


def test_no_network_client_imports_or_http_side_effects():
    import assurance.integrations.github as github

    tree = ast.parse(inspect.getsource(github))
    forbidden = {"httpx", "requests", "urllib", "socket", "aiohttp"}
    imported = {
        node.names[0].name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import) and node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported.isdisjoint(forbidden)


def test_export_is_byte_stable_for_same_passport_and_target():
    first = GitHubExporter.export(_passport(), _target())
    second = GitHubExporter.export(json.loads(json.dumps(_passport())), _target())

    first_bytes = json.dumps(
        first.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    second_bytes = json.dumps(
        second.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert hashlib.sha256(first_bytes).digest() == hashlib.sha256(second_bytes).digest()
