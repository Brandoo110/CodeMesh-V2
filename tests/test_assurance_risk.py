"""P3-01A/P3-01B risk contract atom focused tests."""

import ast
import hashlib
import inspect
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import assurance
from assurance import (
    EvidenceManifest,
    EvidenceManifestEntry,
    GitChange,
    GitSnapshot,
    IntakeDocument,
    IntakeNotice,
    IntakeSnapshot,
    RiskClassification,
    RiskClassificationInput,
    RiskClassificationResult,
    RiskClassifier,
    RiskDeclarations,
)
from assurance import manifest as manifest_module
from assurance import risk as risk_module

FIXED_TIME = datetime(2026, 8, 25, 8, 0, 0, tzinfo=timezone.utc)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest(letter: str) -> str:
    return "sha256:" + letter * 64


def _change(path, status="added", **overrides):
    values = {
        "schema_version": "v1",
        "path": path,
        "old_path": None,
        "status": status,
        "current_size": 1,
        "current_digest": _digest("b"),
        "binary": False,
        "large_file": False,
        "submodule": False,
    }
    values.update(overrides)
    return GitChange.model_validate(values)


def _doc(kind, path, **overrides):
    values = {
        "schema_version": "v1",
        "kind": kind,
        "path": path,
        "artifact_digest": _digest("a"),
        "byte_size": 1,
        "title": None,
        "owner": None,
        "version": None,
        "status": None,
        "acceptance_criteria": (),
        "metadata": (),
    }
    values.update(overrides)
    return IntakeDocument.model_validate(values)


def _git_snapshot(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    change = _change("a.txt")
    values = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "repository": "acme/service",
        "base_revision": "a" * 40,
        "head_revision": "b" * 40,
        "scope": "base_to_worktree",
        "worktree_dirty": False,
        "changes": (change,),
        "changed_files_total": 1,
        "diff_artifact_digest": _digest("d"),
        "diff_bytes": 0,
        "diff_truncated": False,
        "files_truncated": False,
        "ignored_files_lower_bound": 0,
        "ignored_scan_truncated": False,
        "large_file_paths": (),
        "submodule_paths": (),
        "omissions": (),
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return GitSnapshot.model_validate(values)


def _intake_snapshot(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    documents = overrides.get("documents", ())
    values = {
        "schema_version": "v1",
        "subject_digest": subject_digest,
        "documents": documents,
        "notices": (),
        "task_digest": None,
        "task_present": False,
        "policy_count": sum(
            document.kind == "policy" for document in documents
        ),
        "adr_count": sum(document.kind == "adr" for document in documents),
        "runbook_count": sum(
            document.kind == "runbook" for document in documents
        ),
        "manifest_artifact_digest": _digest("a"),
        "complete": True,
        "collected_at": FIXED_TIME,
    }
    values.update(overrides)
    return IntakeSnapshot.model_validate(values)


def _evidence_manifest(subject_digest=None, *, fresh_until=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    if fresh_until is None:
        entry_freshness = "unknown"
        has_unknown_freshness = True
        completeness_status = "has_gaps"
    else:
        entry_freshness = "fresh" if fresh_until >= FIXED_TIME else "stale"
        has_unknown_freshness = False
        completeness_status = "complete"
    entry = EvidenceManifestEntry.model_validate(
        {
            "schema_version": "v1",
            "evidence_id": "ev-1",
            "kind": "test_kind",
            "trust_level": "observed",
            "producer": "test_producer",
            "subject_digest": subject_digest,
            "artifact_digest": _digest("a"),
            "source_ref": "command_batch:" + _digest("a"),
            "status": "success",
            "collected_at": FIXED_TIME,
            "fresh_until": fresh_until,
            "freshness": entry_freshness,
            "redaction_status": "not_applicable",
        }
    )
    provisional_data = {
        "schema_version": "v1",
        "manifest_id": "em_" + "0" * 32,
        "subject_digest": subject_digest,
        "evaluated_at": FIXED_TIME,
        "entries": (entry,),
        "evidence_count": 1,
        "completeness_status": completeness_status,
        "has_incomplete_evidence": False,
        "has_stale_evidence": False,
        "has_unknown_freshness": has_unknown_freshness,
        "has_unredacted_content": False,
        "has_unassessed_redaction": False,
        "canonical_digest": _digest("0"),
        "artifact_digest": _digest("0"),
    }
    provisional_data.update(overrides)
    provisional = EvidenceManifest.model_construct(**provisional_data)
    body = manifest_module._canonical_body(provisional)
    digest = _sha256(body)
    manifest_id = "em_" + hashlib.sha256(
        (subject_digest + digest).encode("utf-8")
    ).hexdigest()[:32]
    values = {
        "schema_version": "v1",
        "manifest_id": manifest_id,
        "subject_digest": subject_digest,
        "evaluated_at": FIXED_TIME,
        "entries": (entry.model_dump(mode="json"),),
        "evidence_count": 1,
        "completeness_status": completeness_status,
        "has_incomplete_evidence": False,
        "has_stale_evidence": False,
        "has_unknown_freshness": has_unknown_freshness,
        "has_unredacted_content": False,
        "has_unassessed_redaction": False,
        "canonical_digest": digest,
        "artifact_digest": digest,
    }
    values.update(overrides)
    return EvidenceManifest.model_validate(values)


def _declarations_data(**overrides):
    values = {
        "changed_lines_total": 0,
        "external_side_effects": "none_declared",
        "provider_boundary": "within_declared_boundary",
    }
    values.update(overrides)
    return values


def _declarations(**overrides):
    return RiskDeclarations.model_validate(_declarations_data(**overrides))


def _input_data(subject_digest=None, **overrides):
    if subject_digest is None:
        subject_digest = _digest("c")
    values = {
        "snapshot": _git_snapshot(subject_digest),
        "intake": _intake_snapshot(subject_digest),
        "manifest": _evidence_manifest(
            subject_digest, fresh_until=FIXED_TIME
        ),
        "declarations": _declarations(),
    }
    values.update(overrides)
    return values


def _input(**overrides):
    return RiskClassificationInput.model_validate(_input_data(**overrides))


def _risk_data(**overrides):
    values = {
        "classification_id": "risk_" + "a" * 32,
        "subject_digest": _digest("b"),
        "rules_version": "risk.v0",
        "rules_digest": _digest("c"),
        "facts_digest": _digest("d"),
        "risk_level": "low",
        "reason_codes": (),
        "required_collectors": (),
        "required_reviewers": (),
        "required_human_role": None,
    }
    values.update(overrides)
    return values


def _canonical_bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _rules_table():
    authorization_segments = sorted(
        {
            "auth",
            "authentication",
            "authorization",
            "iam",
            "rbac",
            "permission",
            "permissions",
            "acl",
            "oauth",
            "oauth2",
        }
    )
    migration_segments = sorted(
        {"migration", "migrations", "alembic", "schema", "schemas"}
    )
    public_api_segments = sorted({"api", "routes", "openapi"})
    public_api_basenames = sorted(
        {"openapi.json", "openapi.yaml", "openapi.yml"}
    )
    dependency_basenames = sorted(
        {
            "pyproject.toml",
            "poetry.lock",
            "pdm.lock",
            "uv.lock",
            "pipfile",
            "pipfile.lock",
            "package.json",
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
            "composer.json",
            "composer.lock",
            "gemfile",
            "gemfile.lock",
            "cargo.toml",
            "cargo.lock",
            "go.mod",
            "go.sum",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "gradle.lockfile",
            "packages.lock.json",
        }
    )
    ci_iac_basenames = sorted(
        {
            "dockerfile",
            "compose.yml",
            "compose.yaml",
            "docker-compose.yml",
            "docker-compose.yaml",
        }
    )
    ci_iac_segments = sorted(
        {"terraform", "k8s", "kubernetes", "helm", "infra", "infrastructure"}
    )
    reason_codes = [
        "AUTHORIZATION_CHANGE",
        "MIGRATION_SCHEMA_CHANGE",
        "PUBLIC_API_CHANGE",
        "DEPENDENCY_LOCKFILE_CHANGE",
        "CI_IAC_CHANGE",
        "EXTERNAL_SIDE_EFFECTS_PRESENT",
        "EXTERNAL_SIDE_EFFECTS_UNKNOWN",
        "LARGE_CHANGE_FILE_COUNT",
        "LARGE_CHANGE_LINE_COUNT",
        "CHANGE_LINE_COUNT_UNKNOWN",
        "CROSS_MODULE_CHANGE",
        "POLICY_ADR_CHANGE",
        "EVIDENCE_GAPS",
        "INTAKE_INCOMPLETE",
        "PROVIDER_BOUNDARY_CROSSING",
        "PROVIDER_BOUNDARY_UNKNOWN",
    ]
    architecture_reasons = sorted(
        {
            "AUTHORIZATION_CHANGE",
            "MIGRATION_SCHEMA_CHANGE",
            "PUBLIC_API_CHANGE",
            "DEPENDENCY_LOCKFILE_CHANGE",
            "CROSS_MODULE_CHANGE",
            "POLICY_ADR_CHANGE",
            "PROVIDER_BOUNDARY_CROSSING",
        }
    )
    operability_reasons = sorted(
        {
            "MIGRATION_SCHEMA_CHANGE",
            "CI_IAC_CHANGE",
            "EXTERNAL_SIDE_EFFECTS_PRESENT",
            "EXTERNAL_SIDE_EFFECTS_UNKNOWN",
            "LARGE_CHANGE_FILE_COUNT",
            "LARGE_CHANGE_LINE_COUNT",
            "CHANGE_LINE_COUNT_UNKNOWN",
            "EVIDENCE_GAPS",
            "INTAKE_INCOMPLETE",
            "PROVIDER_BOUNDARY_UNKNOWN",
            "PROVIDER_BOUNDARY_CROSSING",
        }
    )
    return {
        "collector_rules": {
            "additional_order": [
                "authz_validation",
                "migration_validation",
                "api_contract",
                "dependency_audit",
                "ci_iac_validation",
                "side_effect_validation",
                "provider_boundary_attestation",
            ],
            "base_collectors": [
                "git_snapshot",
                "task_policy_adr",
                "deterministic_commands",
                "evidence_manifest",
            ],
            "reason_mappings": {
                "api_contract": ["PUBLIC_API_CHANGE"],
                "authz_validation": ["AUTHORIZATION_CHANGE"],
                "ci_iac_validation": ["CI_IAC_CHANGE"],
                "dependency_audit": ["DEPENDENCY_LOCKFILE_CHANGE"],
                "migration_validation": ["MIGRATION_SCHEMA_CHANGE"],
                "provider_boundary_attestation": [
                    "PROVIDER_BOUNDARY_CROSSING",
                    "PROVIDER_BOUNDARY_UNKNOWN",
                ],
                "side_effect_validation": [
                    "EXTERNAL_SIDE_EFFECTS_PRESENT",
                    "EXTERNAL_SIDE_EFFECTS_UNKNOWN",
                ],
            },
        },
        "operators": {"files": ">", "lines": ">", "modules": ">="},
        "path_rules": {
            "authorization_segments": authorization_segments,
            "ci_iac_basenames": ci_iac_basenames,
            "ci_iac_prefix_segments": [".github", "workflows"],
            "ci_iac_segments": ci_iac_segments,
            "dependency_basenames": dependency_basenames,
            "migration_basename_suffixes": [".sql"],
            "migration_segments": migration_segments,
            "public_api_basenames": public_api_basenames,
            "public_api_segments": public_api_segments,
            "requirements_basename_prefix": "requirements",
            "requirements_basename_suffix": ".txt",
        },
        "reason_codes": reason_codes,
        "reviewer_rules": {
            "architecture_reasons": architecture_reasons,
            "base_reviewers": ["intent"],
            "high_forces_all_reviewers": True,
            "operability_reasons": operability_reasons,
        },
        "risk_level_rules": {
            "high_signals": sorted(
                {
                    "authorization",
                    "migration_schema",
                    "public_api",
                    "dependency_lockfile",
                    "ci_iac",
                    "side_effects_present",
                    "policy_adr_changed",
                    "provider_crossing",
                    "evidence_gaps",
                    "intake_incomplete",
                    "files_high",
                    "lines_high",
                    "modules_high",
                }
            ),
            "medium_signals": sorted(
                {
                    "lines_unknown",
                    "side_effects_unknown",
                    "provider_unknown",
                    "files_medium",
                    "lines_medium",
                    "modules_medium",
                }
            ),
        },
        "rules_version": "risk.v0",
        "thresholds": {
            "changed_files_high": 20,
            "changed_files_medium": 5,
            "changed_lines_high": 1000,
            "changed_lines_medium": 200,
            "cross_module_high": 3,
            "cross_module_medium": 2,
        },
    }


def _rules_digest() -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(_rules_table())).hexdigest()


def _classification_id_for(data):
    body = {
        key: value for key, value in data.items() if key != "classification_id"
    }
    envelope = {
        "subject_digest": data["subject_digest"],
        "rules_digest": data["rules_digest"],
        "facts_digest": data["facts_digest"],
        "classification_body": body,
    }
    return "risk_" + hashlib.sha256(_canonical_bytes(envelope)).hexdigest()[:32]


def _risk(**overrides):
    data = _risk_data(**overrides)
    data.setdefault("schema_version", "v1")
    if "rules_digest" not in overrides:
        data["rules_digest"] = _rules_digest()
    if "classification_id" not in overrides:
        data["classification_id"] = _classification_id_for(data)
    return RiskClassification.model_validate(data)


def test_risk_contract_imports():
    for name in (
        "RiskDeclarations",
        "RiskClassificationInput",
        "RiskClassification",
        "RiskClassificationResult",
        "RiskClassifier",
    ):
        assert getattr(assurance, name) is getattr(risk_module, name)
        assert name in assurance.__all__
    assert {
        "GitSnapshot",
        "IntakeSnapshot",
        "EvidenceManifest",
        "PolicyDecision",
        "SQLiteAssuranceStore",
    } <= set(assurance.__all__)


def test_public_field_order():
    assert list(RiskDeclarations.model_fields) == [
        "schema_version",
        "changed_lines_total",
        "external_side_effects",
        "provider_boundary",
    ]
    assert list(RiskClassificationInput.model_fields) == [
        "schema_version",
        "snapshot",
        "intake",
        "manifest",
        "declarations",
    ]
    assert list(RiskClassification.model_fields) == [
        "schema_version",
        "classification_id",
        "subject_digest",
        "rules_version",
        "rules_digest",
        "facts_digest",
        "risk_level",
        "reason_codes",
        "required_collectors",
        "required_reviewers",
        "required_human_role",
    ]


def test_v1_only_extra_forbid_and_frozen():
    for data_factory, model in (
        (_declarations_data, RiskDeclarations),
        (_risk_data, RiskClassification),
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**data_factory(), "schema_version": "v2"})
        with pytest.raises(ValidationError):
            model.model_validate({**data_factory(), "unexpected_field": 1})
    with pytest.raises(ValidationError):
        RiskClassificationInput.model_validate(
            {**_input_data(), "schema_version": "v2"}
        )
    with pytest.raises(ValidationError):
        RiskClassificationInput.model_validate(
            {**_input_data(), "unexpected_field": 1}
        )

    declarations = _declarations()
    with pytest.raises(ValidationError):
        declarations.changed_lines_total = 1
    risk = _risk()
    with pytest.raises(ValidationError):
        risk.risk_level = "high"
    classification_input = _input()
    with pytest.raises(ValidationError):
        classification_input.declarations = _declarations()


def test_declarations_strict_count_and_enums():
    assert _declarations(changed_lines_total=0).changed_lines_total == 0
    assert _declarations(changed_lines_total=None).changed_lines_total is None
    assert _declarations(changed_lines_total=7).changed_lines_total == 7
    for value in (True, False, 1.5, "5", -1):
        with pytest.raises(ValidationError):
            _declarations(changed_lines_total=value)

    for value in ("none_declared", "present_declared", "unknown"):
        assert (
            _declarations(external_side_effects=value).external_side_effects
            == value
        )
    for value in ("none", "", "NONE_DECLARED", 1):
        with pytest.raises(ValidationError):
            _declarations(external_side_effects=value)

    for value in (
        "within_declared_boundary",
        "crosses_declared_boundary",
        "unknown",
    ):
        assert _declarations(provider_boundary=value).provider_boundary == value
    for value in ("within", "crosses", "", "WITHIN_DECLARED_BOUNDARY", 1):
        with pytest.raises(ValidationError):
            _declarations(provider_boundary=value)


def test_input_exact_nested_models_and_identity():
    data = _input_data()
    classification_input = RiskClassificationInput.model_validate(data)
    assert classification_input.schema_version == "v1"
    assert classification_input.snapshot is data["snapshot"]
    assert classification_input.intake is data["intake"]
    assert classification_input.manifest is data["manifest"]
    assert classification_input.declarations is data["declarations"]


def test_input_rejects_dict_list_scalar_and_subclass():
    for field_name in ("snapshot", "intake", "manifest", "declarations"):
        with pytest.raises(ValidationError):
            RiskClassificationInput.model_validate(
                {**_input_data(), field_name: {"schema_version": "v1"}}
            )
        with pytest.raises(ValidationError):
            RiskClassificationInput.model_validate(
                {**_input_data(), field_name: []}
            )
        with pytest.raises(ValidationError):
            RiskClassificationInput.model_validate(
                {**_input_data(), field_name: "not-a-model"}
            )

    class SubGitSnapshot(GitSnapshot):
        pass

    class SubIntakeSnapshot(IntakeSnapshot):
        pass

    class SubEvidenceManifest(EvidenceManifest):
        pass

    class SubRiskDeclarations(RiskDeclarations):
        pass

    subclass_values = {
        "snapshot": SubGitSnapshot.model_validate(
            _git_snapshot().model_dump()
        ),
        "intake": SubIntakeSnapshot.model_validate(
            _intake_snapshot().model_dump()
        ),
        "manifest": SubEvidenceManifest.model_validate(
            _evidence_manifest().model_dump()
        ),
        "declarations": SubRiskDeclarations.model_validate(
            _declarations().model_dump()
        ),
    }
    for field_name, subclass_value in subclass_values.items():
        with pytest.raises(ValidationError):
            RiskClassificationInput.model_validate(
                {**_input_data(), field_name: subclass_value}
            )


def test_input_subject_mismatch():
    other = _digest("e")
    for field_name, value in (
        ("snapshot", _git_snapshot(other)),
        ("intake", _intake_snapshot(other)),
        ("manifest", _evidence_manifest(other)),
    ):
        with pytest.raises(ValidationError):
            RiskClassificationInput.model_validate(
                {**_input_data(), field_name: value}
            )


def test_input_deterministic_serialization():
    classification_input = _input()
    first = classification_input.model_dump(mode="json")
    assert first == classification_input.model_dump(mode="json")
    assert list(first) == [
        "schema_version",
        "snapshot",
        "intake",
        "manifest",
        "declarations",
    ]
    assert json.loads(classification_input.model_dump_json()) == first


def test_risk_classification_id_and_digest_grammar():
    valid_id = _classification_id_for(
        {
            **_risk_data(),
            "schema_version": "v1",
            "rules_digest": _rules_digest(),
        }
    )
    assert _risk(classification_id=valid_id).classification_id == valid_id
    assert (
        len(valid_id) == 37
        and valid_id.startswith("risk_")
        and valid_id[5:].isalnum()
    )
    for bad in (
        "risk_" + "A" * 32,
        "risk_" + "a" * 31,
        "risk_" + "a" * 33,
        "risk_" + "g" * 32,
        "risk_" + "a" * 32 + "x",
        "risk_" + "a" * 32 + " ",
        "risk",
        "",
        True,
        123,
    ):
        with pytest.raises(ValidationError):
            _risk(classification_id=bad)

    for field_name in ("subject_digest", "rules_digest", "facts_digest"):
        for bad in (
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "sha256:" + "g" * 64,
            "sha256:" + "a" * 64 + "x",
            "sha256:" + "a" * 64 + " ",
            "sha512:" + "a" * 64,
            "SHA256:" + "a" * 64,
            "a" * 64,
            "",
            True,
            1,
        ):
            with pytest.raises(ValidationError):
                _risk(**{field_name: bad})


def test_risk_level_and_reviewer_enums():
    for level in ("low", "medium"):
        assert _risk(risk_level=level).risk_level == level
    assert _risk(
        risk_level="high", required_human_role="change_owner"
    ).risk_level == "high"
    for bad in ("critical", "LOW", "", 1):
        with pytest.raises(ValidationError):
            _risk(risk_level=bad)

    risk = _risk(
        risk_level="medium",
        required_reviewers=("intent", "architecture", "operability"),
    )
    assert risk.required_reviewers == (
        "intent",
        "architecture",
        "operability",
    )
    assert _risk(required_reviewers=()).required_reviewers == ()
    for bad in ("reviewer", "intent2", "", "INTENT"):
        with pytest.raises(ValidationError):
            _risk(required_reviewers=(bad,))


def test_tuple_list_rejection():
    for field_name in (
        "reason_codes",
        "required_collectors",
        "required_reviewers",
    ):
        with pytest.raises(ValidationError):
            _risk(**{field_name: ["a"]})
        with pytest.raises(ValidationError):
            RiskClassification.model_validate(
                {**_risk_data(), field_name: ["a"]}
            )
        with pytest.raises(ValidationError):
            _risk(**{field_name: "a"})
    with pytest.raises(ValidationError):
        _risk(required_reviewers=("a",))


def test_tuple_item_strictness_blank_unique_order():
    risk = _risk(reason_codes=("z", "a"))
    assert risk.reason_codes == ("z", "a")
    risk = _risk(required_collectors=("b", "a"))
    assert risk.required_collectors == ("b", "a")
    for field_name in ("reason_codes", "required_collectors"):
        for bad in (("a", 1), ("a", None), ("",), (" ",), ("a", "a")):
            with pytest.raises(ValidationError):
                _risk(**{field_name: bad})

    for valid in (
        ("intent",),
        ("architecture",),
        ("operability",),
        ("intent", "architecture"),
        ("architecture", "operability"),
        ("intent", "architecture", "operability"),
    ):
        assert _risk(required_reviewers=valid).required_reviewers == valid
    for bad in (
        ("architecture", "intent"),
        ("operability", "intent"),
        ("intent", "operability", "architecture"),
        ("intent", "intent"),
    ):
        with pytest.raises(ValidationError):
            _risk(required_reviewers=bad)


def test_deep_immutability():
    risk = _risk(
        reason_codes=("a",),
        required_collectors=("b",),
        required_reviewers=("intent",),
    )
    with pytest.raises(TypeError):
        risk.reason_codes[0] = "x"
    with pytest.raises(ValidationError):
        risk.reason_codes += ("c",)
    with pytest.raises(ValidationError):
        risk.required_collectors = ()
    with pytest.raises(ValidationError):
        risk.required_reviewers = ()


def test_cross_field_human_role():
    assert (
        _risk(
            risk_level="high", required_human_role="change_owner"
        ).required_human_role
        == "change_owner"
    )
    for level in ("low", "medium"):
        assert (
            _risk(
                risk_level=level, required_human_role=None
            ).required_human_role
            is None
        )
    with pytest.raises(ValidationError):
        _risk(risk_level="high", required_human_role=None)
    for level in ("low", "medium"):
        with pytest.raises(ValidationError):
            _risk(risk_level=level, required_human_role="change_owner")


def test_standalone_json_round_trip_and_stable_repeated_json():
    declarations = _declarations(changed_lines_total=3)
    restored_declarations = RiskDeclarations.model_validate(
        declarations.model_dump(mode="json")
    )
    assert restored_declarations == declarations
    assert declarations.model_dump_json() == restored_declarations.model_dump_json()
    assert json.loads(declarations.model_dump_json()) == (
        declarations.model_dump(mode="json")
    )

    risk = _risk(
        risk_level="high",
        required_human_role="change_owner",
        reason_codes=("a", "b"),
        required_collectors=("c",),
        required_reviewers=("intent", "architecture"),
    )
    restored_risk = RiskClassification.model_validate_json(
        risk.model_dump_json()
    )
    assert restored_risk == risk
    assert risk.model_dump_json() == restored_risk.model_dump_json()
    assert json.loads(risk.model_dump_json()) == risk.model_dump(mode="json")
    assert risk.model_dump(mode="json") == (
        RiskClassification.model_validate_json(
            risk.model_dump_json()
        ).model_dump(mode="json")
    )
    assert type(restored_risk.reason_codes) is tuple
    # Raw Python validation intentionally rejects the JSON-mode lists
    # produced by model_dump(mode="json"); JSON round-trip goes through
    # model_dump_json()/model_validate_json() with the exact-tuple rule intact.
    with pytest.raises(ValidationError):
        RiskClassification.model_validate(risk.model_dump(mode="json"))


def test_source_ast_audit_has_no_io_or_runtime_behavior():
    source = inspect.getsource(risk_module)
    tree = ast.parse(source)

    banned_modules = {
        "os",
        "pathlib",
        "sqlite3",
        "urllib",
        "http",
        "socket",
        "subprocess",
        "shutil",
        "io",
        "pickle",
        "shelve",
        "dbm",
        "tempfile",
        "requests",
        "httpx",
        "git",
        "asyncio",
        "threading",
        "multiprocessing",
        "sys",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned_modules)

    banned_calls = {
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "print",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "locals",
        "globals",
        "vars",
        "execfile",
    }
    banned_attributes = {
        "read",
        "write",
        "readline",
        "readlines",
        "writelines",
        "flush",
        "seek",
        "close",
        "connect",
        "send",
        "recv",
        "request",
        "urlopen",
        "execute",
        "commit",
        "rollback",
        "Popen",
        "run",
    }
    banned_names = {
        "Collector",
        "path",
        "file",
        "url",
        "http",
        "network",
        "sqlite",
        "storage",
        "shell",
        "subprocess",
        "eval",
        "exec",
        "open",
        "git",
        "io",
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in banned_calls
        ):
            raise AssertionError(f"banned call {node.func.id}")
        if isinstance(node, ast.Attribute) and node.attr in banned_attributes:
            raise AssertionError(f"banned attribute {node.attr}")
        if isinstance(node, ast.Name) and node.id in banned_names:
            raise AssertionError(f"banned name {node.id}")

    class_names = [
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    ]
    assert class_names == [
        "RiskDeclarations",
        "RiskClassificationInput",
        "RiskClassification",
        "RiskClassificationResult",
        "RiskClassifier",
    ]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            base_names = [
                base.id for base in node.bases if isinstance(base, ast.Name)
            ]
            if node.name == "RiskClassifier":
                assert base_names == []
            else:
                assert base_names == ["BaseModel"]


BASE_COLLECTORS = (
    "git_snapshot",
    "task_policy_adr",
    "deterministic_commands",
    "evidence_manifest",
)


REASON_BUILDERS = {
    "AUTHORIZATION_CHANGE": lambda: _input(
        snapshot=_git_snapshot(changes=(_change("auth/main.py"),))
    ),
    "MIGRATION_SCHEMA_CHANGE": lambda: _input(
        snapshot=_git_snapshot(changes=(_change("db/migrations/001.sql"),))
    ),
    "PUBLIC_API_CHANGE": lambda: _input(
        snapshot=_git_snapshot(changes=(_change("api/users.py"),))
    ),
    "DEPENDENCY_LOCKFILE_CHANGE": lambda: _input(
        snapshot=_git_snapshot(changes=(_change("pyproject.toml"),))
    ),
    "CI_IAC_CHANGE": lambda: _input(
        snapshot=_git_snapshot(
            changes=(_change(".github/workflows/ci.yml"),)
        )
    ),
    "EXTERNAL_SIDE_EFFECTS_PRESENT": lambda: _input(
        declarations=_declarations(external_side_effects="present_declared")
    ),
    "EXTERNAL_SIDE_EFFECTS_UNKNOWN": lambda: _input(
        declarations=_declarations(external_side_effects="unknown")
    ),
    "LARGE_CHANGE_FILE_COUNT": lambda: _input(
        snapshot=_git_snapshot(
            changes=(_change("a.txt"),), changed_files_total=6
        )
    ),
    "LARGE_CHANGE_LINE_COUNT": lambda: _input(
        declarations=_declarations(changed_lines_total=201)
    ),
    "CHANGE_LINE_COUNT_UNKNOWN": lambda: _input(
        declarations=_declarations(changed_lines_total=None)
    ),
    "CROSS_MODULE_CHANGE": lambda: _input(
        snapshot=_git_snapshot(
            changes=(_change("a/x.txt"), _change("b/y.txt")),
            changed_files_total=2,
        )
    ),
    "POLICY_ADR_CHANGE": lambda: _input(
        snapshot=_git_snapshot(changes=(_change("docs/policy.md"),)),
        intake=_intake_snapshot(
            documents=(_doc("policy", "docs/policy.md"),)
        ),
    ),
    "EVIDENCE_GAPS": lambda: _input(manifest=_evidence_manifest()),
    "INTAKE_INCOMPLETE": lambda: _input(
        intake=_intake_snapshot(
            complete=False,
            notices=(
                IntakeNotice.model_validate(
                    {
                        "schema_version": "v1",
                        "category": "missing_evidence",
                        "code": "task_spec_not_declared",
                    }
                ),
            ),
        )
    ),
    "PROVIDER_BOUNDARY_CROSSING": lambda: _input(
        declarations=_declarations(
            provider_boundary="crosses_declared_boundary"
        )
    ),
    "PROVIDER_BOUNDARY_UNKNOWN": lambda: _input(
        declarations=_declarations(provider_boundary="unknown")
    ),
}


def test_p3_01b_imports_and_exports():
    assert assurance.RiskClassificationResult is risk_module.RiskClassificationResult
    assert assurance.RiskClassifier is risk_module.RiskClassifier
    assert "RiskClassificationResult" in assurance.__all__
    assert "RiskClassifier" in assurance.__all__
    assert RiskClassificationResult is risk_module.RiskClassificationResult
    assert RiskClassifier is risk_module.RiskClassifier


def test_classifier_public_api_only_classify():
    public = sorted(
        name for name in vars(RiskClassifier) if not name.startswith("_")
    )
    assert public == ["classify"]
    assert callable(RiskClassifier.classify)


def test_result_public_field_order_config_v1():
    assert list(RiskClassificationResult.model_fields) == [
        "schema_version",
        "input",
        "classification",
    ]
    assert RiskClassificationResult.model_config["frozen"] is True
    assert RiskClassificationResult.model_config["extra"] == "forbid"
    value = _input()
    classification = RiskClassifier.classify(value).classification
    result = RiskClassificationResult.model_validate(
        {"input": value, "classification": classification}
    )
    assert result.schema_version == "v1"
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(
            {
                "schema_version": "v2",
                "input": value,
                "classification": classification,
            }
        )
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(
            {
                "input": value,
                "classification": classification,
                "unexpected": 1,
            }
        )
    with pytest.raises(ValidationError):
        result.input = _input()


def test_result_exact_nested_types_and_identity():
    value = _input()
    classification = RiskClassifier.classify(value).classification
    result = RiskClassificationResult.model_validate(
        {"input": value, "classification": classification}
    )
    assert result.input is value
    assert result.classification is classification
    assert result == RiskClassifier.classify(value)


def test_result_rejects_dict_list_scalar_and_subclass():
    value = _input()
    classification = RiskClassifier.classify(value).classification
    for data in (
        {
            "input": value.model_dump(mode="json"),
            "classification": classification,
        },
        {
            "input": value,
            "classification": classification.model_dump(mode="json"),
        },
        {"input": {}, "classification": {}},
        {"input": [], "classification": []},
        {"input": "x", "classification": "y"},
    ):
        with pytest.raises(ValidationError):
            RiskClassificationResult.model_validate(data)
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate("not a mapping")
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(None)

    class SubRiskClassificationInput(RiskClassificationInput):
        pass

    class SubRiskClassification(RiskClassification):
        pass

    sub_input = SubRiskClassificationInput.model_validate(
        {
            "snapshot": value.snapshot,
            "intake": value.intake,
            "manifest": value.manifest,
            "declarations": value.declarations,
        }
    )
    sub_classification = SubRiskClassification.model_validate(
        classification.model_dump()
    )
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(
            {"input": sub_input, "classification": classification}
        )
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(
            {"input": value, "classification": sub_classification}
        )


@pytest.mark.parametrize(
    "bad",
    ["not-an-input", 42, None, {"schema_version": "v1"}, [], b"bytes"],
)
def test_classifier_rejects_non_exact_input(bad):
    with pytest.raises(TypeError):
        RiskClassifier.classify(bad)


def test_classifier_rejects_subclass_input():
    value = _input()

    class SubRiskClassificationInput(RiskClassificationInput):
        pass

    sub_input = SubRiskClassificationInput.model_validate(
        {
            "snapshot": value.snapshot,
            "intake": value.intake,
            "manifest": value.manifest,
            "declarations": value.declarations,
        }
    )
    with pytest.raises(TypeError):
        RiskClassifier.classify(sub_input)


def test_low_baseline():
    value = _input()
    result = RiskClassifier.classify(value)
    assert result.input is value
    classification = result.classification
    assert classification.risk_level == "low"
    assert classification.reason_codes == ()
    assert classification.required_collectors == BASE_COLLECTORS
    assert classification.required_reviewers == ("intent",)
    assert classification.required_human_role is None


@pytest.mark.parametrize(
    ("total", "expected"),
    [(1, "low"), (5, "low"), (6, "medium"), (20, "medium"), (21, "high")],
)
def test_changed_files_boundaries(total, expected):
    value = _input(
        snapshot=_git_snapshot(
            changes=(_change("a.txt"),), changed_files_total=total
        )
    )
    classification = RiskClassifier.classify(value).classification
    assert classification.risk_level == expected
    if total > 5:
        assert "LARGE_CHANGE_FILE_COUNT" in classification.reason_codes
    else:
        assert "LARGE_CHANGE_FILE_COUNT" not in classification.reason_codes


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (0, "low"),
        (200, "low"),
        (201, "medium"),
        (1000, "medium"),
        (1001, "high"),
        (None, "medium"),
    ],
)
def test_changed_lines_boundaries(total, expected):
    value = _input(
        declarations=_declarations(changed_lines_total=total)
    )
    classification = RiskClassifier.classify(value).classification
    assert classification.risk_level == expected
    if total is None:
        assert "CHANGE_LINE_COUNT_UNKNOWN" in classification.reason_codes
    elif total > 200:
        assert "LARGE_CHANGE_LINE_COUNT" in classification.reason_codes
    else:
        assert "CHANGE_LINE_COUNT_UNKNOWN" not in classification.reason_codes
        assert "LARGE_CHANGE_LINE_COUNT" not in classification.reason_codes


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (("a/x.txt",), "low"),
        (("a/x.txt", "b/y.txt"), "medium"),
        (("a/x.txt", "b/y.txt", "c/z.txt"), "high"),
    ],
)
def test_cross_module_boundaries(paths, expected):
    changes = tuple(_change(path) for path in paths)
    value = _input(
        snapshot=_git_snapshot(
            changes=changes, changed_files_total=len(changes)
        )
    )
    classification = RiskClassifier.classify(value).classification
    assert classification.risk_level == expected
    if len(paths) >= 2:
        assert "CROSS_MODULE_CHANGE" in classification.reason_codes
    else:
        assert "CROSS_MODULE_CHANGE" not in classification.reason_codes


@pytest.mark.parametrize("reason", list(REASON_BUILDERS))
def test_each_reason_independently(reason):
    value = REASON_BUILDERS[reason]()
    classification = RiskClassifier.classify(value).classification
    assert classification.reason_codes == (reason,)


def test_combined_reasons_exact_order():
    changes = (
        _change(".github/workflows/ci.yml"),
        _change("api/users.py"),
        _change("auth/main.py"),
        _change("db/migrations/001.sql"),
        _change("docs/policy.md"),
        _change("pyproject.toml"),
    )
    value = _input(
        snapshot=_git_snapshot(changes=changes, changed_files_total=6),
        intake=_intake_snapshot(
            documents=(_doc("policy", "docs/policy.md"),),
            complete=False,
            notices=(
                IntakeNotice.model_validate(
                    {
                        "schema_version": "v1",
                        "category": "missing_evidence",
                        "code": "task_spec_not_declared",
                    }
                ),
            ),
        ),
        manifest=_evidence_manifest(),
        declarations=_declarations(
            changed_lines_total=201,
            external_side_effects="present_declared",
            provider_boundary="crosses_declared_boundary",
        ),
    )
    classification = RiskClassifier.classify(value).classification
    assert classification.reason_codes == (
        "AUTHORIZATION_CHANGE",
        "MIGRATION_SCHEMA_CHANGE",
        "PUBLIC_API_CHANGE",
        "DEPENDENCY_LOCKFILE_CHANGE",
        "CI_IAC_CHANGE",
        "EXTERNAL_SIDE_EFFECTS_PRESENT",
        "LARGE_CHANGE_FILE_COUNT",
        "LARGE_CHANGE_LINE_COUNT",
        "CROSS_MODULE_CHANGE",
        "POLICY_ADR_CHANGE",
        "EVIDENCE_GAPS",
        "INTAKE_INCOMPLETE",
        "PROVIDER_BOUNDARY_CROSSING",
    )
    assert classification.risk_level == "high"
    assert classification.required_human_role == "change_owner"
    assert classification.required_reviewers == (
        "intent",
        "architecture",
        "operability",
    )
    assert classification.required_collectors == (
        "git_snapshot",
        "task_policy_adr",
        "deterministic_commands",
        "evidence_manifest",
        "authz_validation",
        "migration_validation",
        "api_contract",
        "dependency_audit",
        "ci_iac_validation",
        "side_effect_validation",
        "provider_boundary_attestation",
    )


PATH_CASES = [
    (
        "AUTHORIZATION_CHANGE",
        [
            "auth/main.py",
            "authentication/login.py",
            "authorization/policy.py",
            "iam/roles.py",
            "rbac/roles.yaml",
            "permission/check.py",
            "permissions/grant.py",
            "acl/rules.yml",
            "oauth/token.py",
            "oauth2/client.py",
            "Auth/main.py",
            "AUTH/MAIN.PY",
        ],
    ),
    (
        "MIGRATION_SCHEMA_CHANGE",
        [
            "db/migration/001.sql",
            "db/migrations/001.sql",
            "alembic/versions/1.py",
            "schema/model.py",
            "schemas/user.py",
            "db/schema.sql",
            "schema.sql",
            "db/SCHEMA.SQL",
        ],
    ),
    (
        "PUBLIC_API_CHANGE",
        [
            "api/users.py",
            "routes/web.py",
            "openapi/spec.py",
            "openapi.json",
            "openapi.yaml",
            "openapi.yml",
            "OPENAPI.JSON",
        ],
    ),
    (
        "DEPENDENCY_LOCKFILE_CHANGE",
        [
            "pyproject.toml",
            "poetry.lock",
            "pdm.lock",
            "uv.lock",
            "pipfile",
            "pipfile.lock",
            "package.json",
            "package-lock.json",
            "npm-shrinkwrap.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "bun.lock",
            "bun.lockb",
            "composer.json",
            "composer.lock",
            "gemfile",
            "gemfile.lock",
            "cargo.toml",
            "cargo.lock",
            "go.mod",
            "go.sum",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "gradle.lockfile",
            "packages.lock.json",
            "requirements.txt",
            "requirements-dev.txt",
            "Requirements-Prod.TXT",
            "PIPFILE",
        ],
    ),
    (
        "CI_IAC_CHANGE",
        [
            ".github/workflows/ci.yml",
            ".github/workflows/deploy.yaml",
            "dockerfile",
            "Dockerfile",
            "compose.yml",
            "compose.yaml",
            "docker-compose.yml",
            "docker-compose.yaml",
            "terraform/main.tf",
            "k8s/deploy.yaml",
            "kubernetes/service.yaml",
            "helm/values.yaml",
            "infra/network.py",
            "infrastructure/modules/x.tf",
            ".GITHUB/WORKFLOWS/ci.yml",
        ],
    ),
]


@pytest.mark.parametrize(("reason", "paths"), PATH_CASES)
def test_path_families_positive_and_case_variants(reason, paths):
    for path in paths:
        value = _input(snapshot=_git_snapshot(changes=(_change(path),)))
        classification = RiskClassifier.classify(value).classification
        assert reason in classification.reason_codes, path
        assert classification.risk_level == "high"


NEAR_MISS_CASES = [
    (
        "AUTHORIZATION_CHANGE",
        [
            "author.py",
            "authenticated.py",
            "authorization_backup/x.py",
            "auth_utils.py",
            "myauth/x.py",
            "oauth2_client.py",
        ],
    ),
    (
        "MIGRATION_SCHEMA_CHANGE",
        [
            "schematic.py",
            "migration_tools/x.py",
            "schema_backup/x.py",
            "data.sqlite3",
            "db/schema.sql.bak",
        ],
    ),
    (
        "PUBLIC_API_CHANGE",
        [
            "myapi.py",
            "routes_backup/x.py",
            "openapi_spec.py",
            "openapi.schema.json",
            "apiary.py",
        ],
    ),
    (
        "DEPENDENCY_LOCKFILE_CHANGE",
        [
            "myrequirements.txt",
            "requirements.lock",
            "package.json5",
            "yarn.lock.bak",
            "pyproject.toml.bak",
        ],
    ),
    (
        "CI_IAC_CHANGE",
        [
            "terraforming/x.py",
            "terraformfile",
            "k8s_utils/x.py",
            ".github/workflows.yml",
            ".github/actions/ci.yml",
            "foo/.github/workflows/ci.yml",
            "docker-compose.prod.yml",
            "composefile",
        ],
    ),
]


@pytest.mark.parametrize(("reason", "paths"), NEAR_MISS_CASES)
def test_path_families_near_miss_false_positives(reason, paths):
    for path in paths:
        value = _input(snapshot=_git_snapshot(changes=(_change(path),)))
        classification = RiskClassifier.classify(value).classification
        assert reason not in classification.reason_codes, path
        assert classification.risk_level == "low"


def test_policy_adr_exact_document_intersection():
    policy_path = "docs/policy.md"
    adr_path = "docs/adr/0001.md"
    intake = _intake_snapshot(
        documents=(
            _doc("policy", policy_path),
            _doc("adr", adr_path),
        )
    )
    cases = (
        (policy_path, True),
        (adr_path, True),
        ("docs/policy.md.bak", False),
        ("docs/other-policy.md", False),
        ("docs/POLICY.md", False),
        ("docs/adr/0001.md.bak", False),
    )
    for changed_path, expected in cases:
        value = _input(
            snapshot=_git_snapshot(changes=(_change(changed_path),)),
            intake=intake,
        )
        classification = RiskClassifier.classify(value).classification
        assert (
            "POLICY_ADR_CHANGE" in classification.reason_codes
        ) is expected, changed_path
    no_doc = _input(
        snapshot=_git_snapshot(changes=(_change("docs/policy.md"),))
    )
    classification = RiskClassifier.classify(no_doc).classification
    assert "POLICY_ADR_CHANGE" not in classification.reason_codes


COLLECTOR_CASES = [
    (lambda: _input(), ()),
    (
        lambda: _input(snapshot=_git_snapshot(changes=(_change("auth/x"),))),
        ("authz_validation",),
    ),
    (
        lambda: _input(snapshot=_git_snapshot(changes=(_change("db/migrations/x.sql"),))),
        ("migration_validation",),
    ),
    (
        lambda: _input(snapshot=_git_snapshot(changes=(_change("api/x"),))),
        ("api_contract",),
    ),
    (
        lambda: _input(snapshot=_git_snapshot(changes=(_change("package.json"),))),
        ("dependency_audit",),
    ),
    (
        lambda: _input(snapshot=_git_snapshot(changes=(_change("terraform/x"),))),
        ("ci_iac_validation",),
    ),
    (
        lambda: _input(
            declarations=_declarations(external_side_effects="present_declared")
        ),
        ("side_effect_validation",),
    ),
    (
        lambda: _input(
            declarations=_declarations(external_side_effects="unknown")
        ),
        ("side_effect_validation",),
    ),
    (
        lambda: _input(
            declarations=_declarations(
                provider_boundary="crosses_declared_boundary"
            )
        ),
        ("provider_boundary_attestation",),
    ),
    (
        lambda: _input(
            declarations=_declarations(provider_boundary="unknown")
        ),
        ("provider_boundary_attestation",),
    ),
]


@pytest.mark.parametrize("builder", COLLECTOR_CASES, ids=lambda item: "case")
def test_collector_base_and_additional_order(builder):
    value, additional = builder[0](), builder[1]
    classification = RiskClassifier.classify(value).classification
    assert classification.required_collectors == BASE_COLLECTORS + additional


def test_reviewer_mapping_and_high_forces_all():
    cases = (
        (_input(), ("intent",)),
        (
            _input(snapshot=_git_snapshot(changes=(_change("auth/x"),))),
            ("intent", "architecture", "operability"),
        ),
        (
            _input(
                snapshot=_git_snapshot(
                    changes=(_change("a/x"), _change("b/y")),
                    changed_files_total=2,
                )
            ),
            ("intent", "architecture"),
        ),
        (
            _input(
                snapshot=_git_snapshot(
                    changes=(_change("a.txt"),), changed_files_total=6
                )
            ),
            ("intent", "operability"),
        ),
        (
            _input(declarations=_declarations(changed_lines_total=201)),
            ("intent", "operability"),
        ),
        (
            _input(declarations=_declarations(external_side_effects="unknown")),
            ("intent", "operability"),
        ),
        (
            _input(declarations=_declarations(provider_boundary="unknown")),
            ("intent", "operability"),
        ),
        (
            _input(snapshot=_git_snapshot(changes=(_change("db/migrations/x.sql"),))),
            ("intent", "architecture", "operability"),
        ),
        (
            _input(
                snapshot=_git_snapshot(
                    changes=(_change(".github/workflows/ci.yml"),)
                )
            ),
            ("intent", "architecture", "operability"),
        ),
        (
            _input(
                snapshot=_git_snapshot(
                    changes=(_change("a.txt"),), changed_files_total=21
                )
            ),
            ("intent", "architecture", "operability"),
        ),
    )
    for value, expected in cases:
        classification = RiskClassifier.classify(value).classification
        assert classification.required_reviewers == expected
        if classification.risk_level == "high":
            assert classification.required_human_role == "change_owner"
            assert classification.required_reviewers == (
                "intent",
                "architecture",
                "operability",
            )
        else:
            assert classification.required_human_role is None


def test_rules_table_immutable_and_digest_independently_recomputed():
    assert risk_module._RULES_DIGEST == _rules_digest()
    assert risk_module._RULES_DIGEST.startswith("sha256:")
    assert len(risk_module._RULES_DIGEST) == 7 + 64
    with pytest.raises(TypeError):
        risk_module._RULES_TABLE["rules_version"] = "risk.v1"
    assert risk_module._RULES_TABLE["rules_version"] == "risk.v0"


def test_collector_reasons_are_the_single_immutable_rule_authority():
    value = _input(
        snapshot=_git_snapshot(changes=(_change("auth/main.py"),))
    )
    original = risk_module._COLLECTOR_REASONS["authz_validation"]
    digest_before = risk_module._RULES_DIGEST
    before = RiskClassifier.classify(value)
    mutated = False
    try:
        with pytest.raises(TypeError):
            risk_module._COLLECTOR_REASONS["authz_validation"] = frozenset(
                {"NOT_A_REAL_REASON"}
            )
            mutated = True
    finally:
        if mutated:
            risk_module._COLLECTOR_REASONS["authz_validation"] = original
    after = RiskClassifier.classify(value)
    assert after == before
    assert after.classification.rules_digest == digest_before
    assert risk_module._RULES_DIGEST == digest_before
    assert (
        risk_module._RULES_TABLE["collector_rules"]["reason_mappings"]
        is risk_module._COLLECTOR_REASONS
    )


def test_facts_digest_independently_recomputed():
    value = _input()
    classification = RiskClassifier.classify(value).classification
    expected = "sha256:" + hashlib.sha256(
        _canonical_bytes(value.model_dump(mode="json"))
    ).hexdigest()
    assert classification.facts_digest == expected


def test_classification_id_independently_recomputed():
    classification = RiskClassifier.classify(_input()).classification
    data = classification.model_dump(mode="json")
    assert classification.classification_id == _classification_id_for(data)


def test_classification_rejects_arbitrary_rules_digest_and_id():
    valid = _risk()
    with pytest.raises(ValidationError):
        RiskClassification.model_validate(
            {**valid.model_dump(), "rules_digest": _digest("c")}
        )
    with pytest.raises(ValidationError):
        RiskClassification.model_validate(
            {
                **valid.model_dump(),
                "classification_id": "risk_" + "0" * 32,
            }
        )


@pytest.mark.parametrize(
    ("field", "mutator"),
    [
        (
            "subject_digest",
            lambda data: data.update(subject_digest=_digest("e")),
        ),
        (
            "rules_digest",
            lambda data: data.update(rules_digest=_digest("e")),
        ),
        (
            "facts_digest",
            lambda data: data.update(facts_digest=_digest("e")),
        ),
        (
            "rules_version",
            lambda data: data.update(rules_version="risk.v9"),
        ),
        (
            "risk_level",
            lambda data: data.update(
                risk_level="high", required_human_role="change_owner"
            ),
        ),
        (
            "reason_codes",
            lambda data: data.update(reason_codes=("AUTHORIZATION_CHANGE",)),
        ),
        (
            "required_collectors",
            lambda data: data.update(
                required_collectors=("authz_validation",)
            ),
        ),
        (
            "required_reviewers",
            lambda data: data.update(
                required_reviewers=("intent", "architecture")
            ),
        ),
        (
            "required_human_role",
            lambda data: data.update(required_human_role="change_owner"),
        ),
    ],
)
def test_result_forgery_rejection_every_classification_field(field, mutator):
    value = _input()
    classification = RiskClassifier.classify(value).classification
    data = classification.model_dump()
    mutator(data)
    forged = RiskClassification.model_construct(
        **{**data, "classification_id": _classification_id_for(data)}
    )
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(
            {"input": value, "classification": forged}
        )


def test_result_rejects_synchronized_id_forgery():
    value = _input()
    classification = RiskClassifier.classify(value).classification
    data = classification.model_dump()
    data["subject_digest"] = _digest("e")
    data["facts_digest"] = _digest("f")
    data["risk_level"] = "high"
    data["required_human_role"] = "change_owner"
    data["reason_codes"] = ("AUTHORIZATION_CHANGE",)
    data["required_collectors"] = BASE_COLLECTORS + ("authz_validation",)
    data["required_reviewers"] = (
        "intent",
        "architecture",
        "operability",
    )
    data["classification_id"] = _classification_id_for(data)
    forged = RiskClassification.model_construct(**data)
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(
            {"input": value, "classification": forged}
        )


def test_result_rejects_stale_id_forgery():
    value = _input()
    classification = RiskClassifier.classify(value).classification
    forged = RiskClassification.model_construct(
        subject_digest=_digest("e"),
        rules_digest=classification.rules_digest,
        facts_digest=classification.facts_digest,
        classification_id=classification.classification_id,
        rules_version=classification.rules_version,
        risk_level=classification.risk_level,
        reason_codes=classification.reason_codes,
        required_collectors=classification.required_collectors,
        required_reviewers=classification.required_reviewers,
        required_human_role=classification.required_human_role,
    )
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(
            {"input": value, "classification": forged}
        )


def test_repeat_input_equality_and_deterministic_json():
    value = _input()
    first = RiskClassifier.classify(value)
    second = RiskClassifier.classify(value)
    assert first == second
    assert first is not second
    assert first.model_dump_json() == second.model_dump_json()
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    rebuilt = RiskClassificationInput.model_validate(
        {
            "snapshot": value.snapshot,
            "intake": value.intake,
            "manifest": value.manifest,
            "declarations": value.declarations,
        }
    )
    third = RiskClassifier.classify(rebuilt)
    assert third == first
    assert third.model_dump_json() == first.model_dump_json()


def test_stable_output_under_semantically_equivalent_input_order():
    changes = (
        _change("alpha/x.txt"),
        _change("beta/y.txt"),
        _change("gamma/z.txt"),
    )
    ordered = _git_snapshot(changes=changes, changed_files_total=3)
    reversed_snapshot = GitSnapshot.model_construct(
        schema_version="v1",
        subject_digest=ordered.subject_digest,
        repository=ordered.repository,
        base_revision=ordered.base_revision,
        head_revision=ordered.head_revision,
        scope="base_to_worktree",
        worktree_dirty=False,
        changes=tuple(reversed(changes)),
        changed_files_total=3,
        diff_artifact_digest=ordered.diff_artifact_digest,
        diff_bytes=0,
        diff_truncated=False,
        files_truncated=False,
        ignored_files_lower_bound=0,
        ignored_scan_truncated=False,
        large_file_paths=(),
        submodule_paths=(),
        omissions=(),
        complete=True,
        collected_at=FIXED_TIME,
    )
    value_a = _input(snapshot=ordered)
    value_b = RiskClassificationInput.model_construct(
        schema_version="v1",
        snapshot=reversed_snapshot,
        intake=value_a.intake,
        manifest=value_a.manifest,
        declarations=value_a.declarations,
    )
    result_a = RiskClassifier.classify(value_a)
    result_b = RiskClassifier.classify(value_b)
    for field in (
        "subject_digest",
        "rules_digest",
        "risk_level",
        "reason_codes",
        "required_collectors",
        "required_reviewers",
        "required_human_role",
    ):
        assert getattr(result_b.classification, field) == getattr(
            result_a.classification, field
        )


def test_result_no_dict_json_revalidation_path():
    value = _input()
    classification = RiskClassifier.classify(value).classification
    result = RiskClassificationResult.model_validate(
        {"input": value, "classification": classification}
    )
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate(result.model_dump(mode="json"))
    with pytest.raises(ValidationError):
        RiskClassificationResult.model_validate_json(result.model_dump_json())
    restored = RiskClassification.model_validate_json(
        classification.model_dump_json()
    )
    assert restored == classification
    assert restored.model_dump_json() == classification.model_dump_json()


def test_p3_01b_source_ast_audit_pure_classifier():
    source = inspect.getsource(risk_module)
    tree = ast.parse(source)
    banned_modules = {
        "os",
        "pathlib",
        "sqlite3",
        "urllib",
        "http",
        "socket",
        "subprocess",
        "shutil",
        "io",
        "pickle",
        "shelve",
        "dbm",
        "tempfile",
        "requests",
        "httpx",
        "git",
        "asyncio",
        "threading",
        "multiprocessing",
        "sys",
        "time",
        "random",
        "datetime",
        "collections",
        "dataclasses",
        "functools",
    }
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned_modules)
    assert {"hashlib", "json"} <= imported

    banned_calls = {
        "open",
        "eval",
        "exec",
        "compile",
        "input",
        "print",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "locals",
        "globals",
        "vars",
        "execfile",
    }
    banned_attributes = {
        "read",
        "write",
        "readline",
        "readlines",
        "writelines",
        "flush",
        "seek",
        "close",
        "connect",
        "send",
        "recv",
        "request",
        "urlopen",
        "execute",
        "commit",
        "rollback",
        "Popen",
        "run",
        "exists",
        "get_bytes",
        "put_bytes",
        "verify",
    }
    banned_names = {
        "Collector",
        "ArtifactStore",
        "SQLiteAssuranceStore",
        "PolicyGate",
        "Reviewer",
        "router",
        "orchestration",
        "storage",
        "network",
        "shell",
        "subprocess",
        "eval",
        "exec",
        "open",
        "git",
        "io",
        "path",
        "file",
        "time",
        "random",
        "datetime",
        "os",
        "sys",
        "requests",
        "httpx",
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in banned_calls
        ):
            raise AssertionError(f"banned call {node.func.id}")
        if isinstance(node, ast.Attribute) and node.attr in banned_attributes:
            raise AssertionError(f"banned attribute {node.attr}")
        if isinstance(node, ast.Name) and node.id in banned_names:
            raise AssertionError(f"banned name {node.id}")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            raise AssertionError("mutable global/nonlocal state is forbidden")
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            for target in targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    raise AssertionError(
                        f"module-level mutable public binding {target.id}"
                    )
