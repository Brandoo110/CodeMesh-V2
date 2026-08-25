"""Focused contract tests for assurance.author_receipts (V2-P2-05)."""

import ast
import builtins
import hashlib
import inspect
import json

import pytest
from pydantic import ValidationError

import assurance
from assurance import ArtifactNotFoundError, ArtifactStore, Evidence
from assurance import author_receipts as author_module
from assurance.author_receipts import (
    AuthorAgentReceipt,
    AuthorAgentReceiptArtifactError,
    AuthorAgentReceiptCost,
    AuthorAgentReceiptError,
    AuthorAgentReceiptNormalizer,
    AuthorAgentReceiptPayloadError,
    AuthorAgentReceiptResult,
    AuthorAgentReceiptSubjectMismatch,
    GenericAuthorReceiptEnvelope,
)


SUBJECT = "sha256:" + "0" * 64
OTHER_DIGEST = "sha256:" + "1" * 64
FIXED_TIME = "2026-08-25T08:00:00+00:00"
FIXED_TIME_LATER = "2026-08-25T08:05:00+00:00"
SECRET_MARKER = "S3CR3T-TOKEN-7f3a"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


RAW_BYTES = b"raw author receipt artifact bytes"
RAW_DIGEST = _sha256(RAW_BYTES)


def _payload(obj: dict) -> bytes:
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "store")


def _file_set(store: ArtifactStore) -> set[str]:
    if not store.root.exists():
        return set()
    return {
        path.relative_to(store.root).as_posix()
        for path in store.root.rglob("*")
        if path.is_file()
    }


def _cost(**overrides) -> dict:
    data = {"schema_version": "v1", "amount": 0.1, "currency": "CNY"}
    data.update(overrides)
    return data


def _generic(**overrides) -> dict:
    data = {
        "schema_version": "v1",
        "subject_digest": SUBJECT,
        "run_id": "g-run-1",
        "session_id": "sess-1",
        "provider_refs": ["provider-a"],
        "model_refs": ["model-b"],
        "tool_names": ["read_file"],
        "files_touched": ["src/a.py"],
        "command_claims": ["pytest -q"],
        "check_claims": ["checks passed"],
        "declared_intent": "implement bounded normalization",
        "declared_completion": "normalization implemented",
        "completion_status": "success",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost": _cost(),
        "started_at": FIXED_TIME,
        "completed_at": FIXED_TIME_LATER,
    }
    data.update(overrides)
    return data


def _v1_step(**overrides) -> dict:
    data = {
        "id": 1,
        "run_id": "run-001",
        "step_id": "step-1",
        "step_order": 1,
        "status": "done",
        "output": "step output",
        "error": None,
        "tool_calls": [
            {
                "name": "read_file",
                "args": {"path": "a.py"},
                "status": "ok",
                "ok": True,
                "result": "file content",
            }
        ],
        "file_diffs": [
            {"path": "src/a.py", "before": "old", "after": "new", "kind": "modified"}
        ],
        "model_used": "model-a",
        "cost_rmb": 0.5,
        "duration_ms": 1200,
        "started_at": "2026-08-25T08:00:05+00:00",
        "completed_at": "2026-08-25T08:01:00+00:00",
    }
    data.update(overrides)
    return data


def _v1_run(**overrides) -> dict:
    data = {
        "id": "run-001",
        "workflow_id": "wf-001",
        "status": "done",
        "started_at": FIXED_TIME,
        "completed_at": FIXED_TIME_LATER,
        "total_cost_rmb": 1.25,
        "error": None,
        "final_reply": "final answer",
        "step_results": [_v1_step()],
    }
    data.update(overrides)
    return data


def _v1_done_run() -> dict:
    return _v1_run(
        step_results=[
            _v1_step(),
            _v1_step(
                id=2,
                step_id="step-2",
                step_order=2,
                output="step two output",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "b.py"},
                        "status": "ok",
                        "ok": True,
                        "result": "more content",
                    },
                    {
                        "name": "write_file",
                        "args": {"path": "c.py", "content": "x"},
                        "status": "ok",
                        "ok": True,
                        "result": None,
                    },
                ],
                file_diffs=[
                    {
                        "path": "src/b.py",
                        "before": None,
                        "after": "new b",
                        "kind": "created",
                        "truncated": False,
                    }
                ],
                model_used="model-b",
                cost_rmb=0.75,
                duration_ms=900,
                started_at="2026-08-25T08:01:10+00:00",
                completed_at="2026-08-25T08:02:00+00:00",
            ),
        ]
    )


def _receipt_dict(**overrides) -> dict:
    data = {
        "schema_version": "v1",
        "receipt_id": "ar_" + "a" * 32,
        "source_kind": "generic",
        "source_schema": "generic_author_receipt.v1",
        "run_id": "g-run-1",
        "session_id": "sess-1",
        "subject_digest": SUBJECT,
        "provider_refs": ["provider-a"],
        "model_refs": ["model-b"],
        "tool_names": ["read_file"],
        "files_touched": ["src/a.py"],
        "command_claims": ["pytest -q"],
        "check_claims": ["checks passed"],
        "declared_intent": "implement bounded normalization",
        "declared_completion": "normalization implemented",
        "completion_status": "success",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost": _cost(),
        "started_at": FIXED_TIME,
        "completed_at": FIXED_TIME_LATER,
        "missing_fields": [],
        "raw_artifact_digest": RAW_DIGEST,
        "canonical_digest": _sha256(b"canonical"),
        "trust_level": "declared",
    }
    data.update(overrides)
    return data


def _canonical_body(receipt: AuthorAgentReceipt) -> bytes:
    data = receipt.model_dump(mode="json")
    data.pop("receipt_id")
    data.pop("canonical_digest")
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _normalize_generic(tmp_path, **overrides) -> AuthorAgentReceiptResult:
    store = _store(tmp_path)
    return AuthorAgentReceiptNormalizer.normalize_generic(
        _payload(_generic(**overrides)),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )


def _normalize_v1(tmp_path, run: dict | None = None) -> AuthorAgentReceiptResult:
    store = _store(tmp_path)
    return AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
        _payload(_v1_done_run() if run is None else run),
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )


NEW_PUBLIC_NAMES = {
    "AuthorAgentReceiptCost",
    "GenericAuthorReceiptEnvelope",
    "AuthorAgentReceipt",
    "AuthorAgentReceiptResult",
    "AuthorAgentReceiptNormalizer",
    "AuthorAgentReceiptError",
    "AuthorAgentReceiptPayloadError",
    "AuthorAgentReceiptSubjectMismatch",
    "AuthorAgentReceiptArtifactError",
}

ALL_MODELS = (
    AuthorAgentReceiptCost,
    GenericAuthorReceiptEnvelope,
    AuthorAgentReceipt,
    AuthorAgentReceiptResult,
)

MISSING_ORDER = (
    "source_subject_digest",
    "session_id",
    "provider_refs",
    "model_refs",
    "tool_names",
    "files_touched",
    "command_claims",
    "check_claims",
    "declared_intent",
    "declared_completion",
    "input_tokens",
    "output_tokens",
    "cost",
)


def test_public_api_exists():
    assert author_module.AuthorAgentReceiptCost is AuthorAgentReceiptCost
    assert (
        author_module.GenericAuthorReceiptEnvelope
        is GenericAuthorReceiptEnvelope
    )
    assert author_module.AuthorAgentReceipt is AuthorAgentReceipt
    assert author_module.AuthorAgentReceiptResult is AuthorAgentReceiptResult
    assert (
        author_module.AuthorAgentReceiptNormalizer
        is AuthorAgentReceiptNormalizer
    )
    assert author_module.AuthorAgentReceiptError is AuthorAgentReceiptError
    assert (
        author_module.AuthorAgentReceiptPayloadError
        is AuthorAgentReceiptPayloadError
    )
    assert (
        author_module.AuthorAgentReceiptSubjectMismatch
        is AuthorAgentReceiptSubjectMismatch
    )
    assert (
        author_module.AuthorAgentReceiptArtifactError
        is AuthorAgentReceiptArtifactError
    )


def test_package_exports_preserve_prior_names_and_add_new_api():
    assert set(assurance.__all__) >= NEW_PUBLIC_NAMES
    for name in NEW_PUBLIC_NAMES:
        assert getattr(assurance, name) is not None
    assert "Evidence" in assurance.__all__
    assert "ArtifactStore" in assurance.__all__
    assert "ExecutionReceipt" in assurance.__all__
    assert assurance.__all__ != list(NEW_PUBLIC_NAMES)


def test_error_hierarchy_is_simple():
    assert issubclass(AuthorAgentReceiptError, Exception)
    assert issubclass(AuthorAgentReceiptPayloadError, AuthorAgentReceiptError)
    assert issubclass(
        AuthorAgentReceiptSubjectMismatch, AuthorAgentReceiptError
    )
    assert issubclass(AuthorAgentReceiptArtifactError, AuthorAgentReceiptError)


def test_normalizer_has_only_two_public_entry_points():
    public_methods = sorted(
        name
        for name in vars(AuthorAgentReceiptNormalizer)
        if not name.startswith("_")
    )
    assert public_methods == ["normalize_codemesh_v1", "normalize_generic"]


def test_all_models_are_v1_frozen_extra_forbid():
    for model in ALL_MODELS:
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_cost_field_order_v1_only_and_strict_roundtrip():
    assert list(AuthorAgentReceiptCost.model_fields) == [
        "schema_version",
        "amount",
        "currency",
    ]
    model = AuthorAgentReceiptCost.model_validate(_cost(amount=5))
    assert model.amount == 5.0
    assert type(model.amount) is float
    restored = AuthorAgentReceiptCost.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        AuthorAgentReceiptCost.model_validate(_cost(schema_version="v2"))
    with pytest.raises(ValidationError):
        AuthorAgentReceiptCost.model_validate({**_cost(), "extra": 1})


def test_cost_strict_number_and_currency_boundaries():
    AuthorAgentReceiptCost.model_validate(_cost(amount=0))
    AuthorAgentReceiptCost.model_validate(_cost(amount=0.0))
    AuthorAgentReceiptCost.model_validate(_cost(currency="USD"))
    for overrides in (
        {"amount": True},
        {"amount": False},
        {"amount": "1.5"},
        {"amount": None},
        {"amount": -0.01},
        {"amount": float("nan")},
        {"amount": float("inf")},
        {"amount": float("-inf")},
        {"currency": "cny"},
        {"currency": "CN"},
        {"currency": "CNY1"},
        {"currency": "CN Y"},
        {"currency": ""},
        {"currency": 123},
    ):
        with pytest.raises(ValidationError):
            AuthorAgentReceiptCost.model_validate(_cost(**overrides))


def test_envelope_field_order_and_roundtrip():
    assert list(GenericAuthorReceiptEnvelope.model_fields) == [
        "schema_version",
        "subject_digest",
        "run_id",
        "session_id",
        "provider_refs",
        "model_refs",
        "tool_names",
        "files_touched",
        "command_claims",
        "check_claims",
        "declared_intent",
        "declared_completion",
        "completion_status",
        "input_tokens",
        "output_tokens",
        "cost",
        "started_at",
        "completed_at",
    ]
    model = GenericAuthorReceiptEnvelope.model_validate(_generic())
    restored = GenericAuthorReceiptEnvelope.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            _generic(schema_version="v2")
        )
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            {**_generic(), "missing_fields": []}
        )
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            {**_generic(), "receipt_id": "ar_x"}
        )
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            {**_generic(), "trust_level": "declared"}
        )
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            {**_generic(), "evidence": {}}
        )


def test_envelope_minimal_fields_and_missing_required():
    model = GenericAuthorReceiptEnvelope.model_validate(
        {
            "schema_version": "v1",
            "subject_digest": SUBJECT,
            "run_id": "g-min",
            "completion_status": "success",
            "started_at": FIXED_TIME,
            "completed_at": FIXED_TIME_LATER,
        }
    )
    assert model.session_id is None
    assert model.provider_refs == ()
    assert model.cost is None
    assert model.input_tokens is None
    for name in (
        "subject_digest",
        "run_id",
        "completion_status",
        "started_at",
        "completed_at",
    ):
        data = dict(_generic())
        del data[name]
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(data)


def test_envelope_tuples_are_deep_immutable_and_copy_safe():
    tool_names = ["read_file", "write_file"]
    model = GenericAuthorReceiptEnvelope.model_validate(
        _generic(tool_names=tool_names)
    )
    assert model.tool_names == ("read_file", "write_file")
    assert type(model.tool_names) is tuple
    tool_names.append("mutated")
    assert model.tool_names == ("read_file", "write_file")
    dumped = model.model_dump(mode="json")
    assert dumped["tool_names"] == ["read_file", "write_file"]
    restored = GenericAuthorReceiptEnvelope.model_validate(dumped)
    assert restored == model
    assert type(restored.tool_names) is tuple
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            _generic(tool_names=("read_file", "read_file"))
        )
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            _generic(provider_refs=("",))
        )
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            _generic(model_refs=(123,))
        )
    with pytest.raises(ValidationError):
        GenericAuthorReceiptEnvelope.model_validate(
            _generic(command_claims=("pytest passed", None))
        )


def test_envelope_text_bounds_and_blank_rules():
    GenericAuthorReceiptEnvelope.model_validate(_generic(run_id="r" * 256))
    GenericAuthorReceiptEnvelope.model_validate(
        _generic(session_id="s" * 256)
    )
    GenericAuthorReceiptEnvelope.model_validate(
        _generic(declared_intent="i" * 4096)
    )
    GenericAuthorReceiptEnvelope.model_validate(
        _generic(command_claims=("c" * 4096,))
    )
    for overrides in (
        {"run_id": ""},
        {"run_id": "   "},
        {"run_id": "a\x00b"},
        {"run_id": "r" * 257},
        {"session_id": ""},
        {"session_id": "\t"},
        {"session_id": "a\x00b"},
        {"session_id": "s" * 257},
        {"declared_intent": ""},
        {"declared_intent": " "},
        {"declared_intent": "a\x00b"},
        {"declared_intent": "i" * 4097},
        {"declared_completion": ""},
        {"declared_completion": "  "},
        {"declared_completion": "a\x00b"},
        {"declared_completion": "c" * 4097},
        {"command_claims": ["x\x00y"]},
        {"command_claims": [""]},
        {"command_claims": ["   "]},
        {"command_claims": ["c" * 4097]},
        {"check_claims": ["x\x00y"]},
        {"check_claims": [""]},
        {"check_claims": ["c" * 4097]},
    ):
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(
                _generic(**overrides)
            )


def test_envelope_tuple_count_and_shape_bounds():
    GenericAuthorReceiptEnvelope.model_validate(
        _generic(tool_names=tuple(f"t{i}" for i in range(64)))
    )
    GenericAuthorReceiptEnvelope.model_validate(
        _generic(command_claims=tuple(f"c{i}" for i in range(64)))
    )
    for name in (
        "provider_refs",
        "model_refs",
        "tool_names",
        "files_touched",
        "command_claims",
        "check_claims",
    ):
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(
                _generic(**{name: tuple(f"{name}-{i}" for i in range(65))})
            )
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(
                _generic(**{name: "not-a-list"})
            )
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(
                _generic(**{name: [None]})
            )


def test_envelope_files_touched_canonical_paths_only():
    GenericAuthorReceiptEnvelope.model_validate(
        _generic(files_touched=["src/a.py", "docs/guide.md", "a/b/c.txt"])
    )
    for path in (
        "/abs/path.py",
        "//unc/path.py",
        "C:/win/path.py",
        "C:\\win\\path.py",
        "../escape.py",
        "a/../b.py",
        ".",
        "./a.py",
        "a//b.py",
        "a/./b.py",
        "a/",
        "",
        "~",
        "~/home.py",
        "~user/x.py",
        "/Users/junjieli/x.py",
        "/home/user/x.py",
        "a\x00b.py",
        "a\\b.py",
        "a" * 257,
    ):
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(
                _generic(files_touched=[path])
            )


def test_envelope_token_cost_and_time_boundaries():
    GenericAuthorReceiptEnvelope.model_validate(_generic(input_tokens=0))
    for overrides in (
        {"input_tokens": -1},
        {"input_tokens": 1.5},
        {"input_tokens": True},
        {"input_tokens": "10"},
        {"output_tokens": -1},
        {"output_tokens": 1.5},
        {"output_tokens": True},
        {"output_tokens": "10"},
        {"cost": _cost(amount=-1)},
        {"cost": _cost(amount="1")},
        {"cost": _cost(currency="CN")},
        {"cost": "CNY"},
        {"started_at": "2026-08-25T08:00:00"},
        {"completed_at": "2026-08-25T08:05:00"},
        {"started_at": "not-a-date"},
        {"completed_at": FIXED_TIME, "started_at": FIXED_TIME_LATER},
        {"completion_status": "unknown"},
    ):
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(
                _generic(**overrides)
            )


def test_envelope_strict_primitives_and_digest_rejections():
    for overrides in (
        {"subject_digest": "sha256:XYZ"},
        {"subject_digest": SUBJECT.upper()},
        {"subject_digest": SUBJECT[:-1]},
        {"subject_digest": 1},
        {"run_id": 123},
        {"run_id": None},
        {"session_id": 123},
        {"completion_status": True},
        {"completion_status": 1},
        {"provider_refs": 1},
        {"model_refs": "model"},
        {"tool_names": {"name"}},
        {"files_touched": 1},
        {"command_claims": "pytest"},
        {"check_claims": 1},
        {"declared_intent": 1},
        {"declared_completion": []},
        {"cost": 5},
        {"cost": "CNY"},
        {"cost": {"schema_version": "v1", "amount": 1}},
        {"cost": {"schema_version": "v1", "amount": 1.0, "currency": "CNY", "extra": 1}},
        {"started_at": 123},
        {"completed_at": None},
    ):
        with pytest.raises(ValidationError):
            GenericAuthorReceiptEnvelope.model_validate(
                _generic(**overrides)
            )


def test_receipt_field_order_and_v1_only():
    assert list(AuthorAgentReceipt.model_fields) == [
        "schema_version",
        "receipt_id",
        "source_kind",
        "source_schema",
        "run_id",
        "session_id",
        "subject_digest",
        "provider_refs",
        "model_refs",
        "tool_names",
        "files_touched",
        "command_claims",
        "check_claims",
        "declared_intent",
        "declared_completion",
        "completion_status",
        "input_tokens",
        "output_tokens",
        "cost",
        "started_at",
        "completed_at",
        "missing_fields",
        "raw_artifact_digest",
        "canonical_digest",
        "trust_level",
    ]
    model = AuthorAgentReceipt.model_validate(_receipt_dict())
    restored = AuthorAgentReceipt.model_validate(
        model.model_dump(mode="json")
    )
    assert restored == model
    assert model.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        AuthorAgentReceipt.model_validate(
            _receipt_dict(schema_version="v2")
        )
    with pytest.raises(ValidationError):
        AuthorAgentReceipt.model_validate({**_receipt_dict(), "extra": 1})
    AuthorAgentReceipt.model_validate(
        _receipt_dict(source_kind="codemesh_v1")
    )


def test_receipt_missing_fields_literal_order_and_identity():
    model = AuthorAgentReceipt.model_validate(
        _receipt_dict(
            missing_fields=[
                "session_id",
                "provider_refs",
                "declared_intent",
            ]
        )
    )
    assert model.missing_fields == (
        "session_id",
        "provider_refs",
        "declared_intent",
    )
    for missing_fields in (
        ["unknown_field"],
        ["session_id", "session_id"],
        ["provider_refs", "session_id"],
        {"session_id"},
        "session_id",
        [123],
    ):
        with pytest.raises(ValidationError):
            AuthorAgentReceipt.model_validate(
                _receipt_dict(missing_fields=missing_fields)
            )
    for overrides in (
        {"receipt_id": "ar_" + "Z" * 32},
        {"receipt_id": "ar_" + "A" * 32},
        {"receipt_id": "ar_"},
        {"receipt_id": "ev_author_" + "a" * 32},
        {"raw_artifact_digest": "sha256:XYZ"},
        {"canonical_digest": OTHER_DIGEST.upper()},
        {"subject_digest": OTHER_DIGEST[:-1]},
        {"trust_level": "deterministic"},
        {"completed_at": FIXED_TIME, "started_at": FIXED_TIME_LATER},
    ):
        with pytest.raises(ValidationError):
            AuthorAgentReceipt.model_validate(
                _receipt_dict(**overrides)
            )


def test_receipt_bounded_fields_match_envelope_rules():
    AuthorAgentReceipt.model_validate(
        _receipt_dict(run_id="r" * 256, files_touched=["a" * 256])
    )
    for overrides in (
        {"run_id": ""},
        {"run_id": "a\x00b"},
        {"run_id": "r" * 257},
        {"session_id": " "},
        {"session_id": "s" * 257},
        {"provider_refs": ["x\x00y"]},
        {"model_refs": [""]},
        {"tool_names": ["t" * 257]},
        {"files_touched": ["/abs"]},
        {"files_touched": ["../x"]},
        {"files_touched": ["~"]},
        {"files_touched": ["C:/x"]},
        {"command_claims": ["c" * 4097]},
        {"check_claims": [""]},
        {"declared_intent": "x\x00y"},
        {"declared_completion": "x" * 4097},
        {"input_tokens": True},
        {"output_tokens": -1},
    ):
        with pytest.raises(ValidationError):
            AuthorAgentReceipt.model_validate(
                _receipt_dict(**overrides)
            )


def test_result_field_order_and_roundtrip(tmp_path):
    result = _normalize_generic(tmp_path)
    assert list(AuthorAgentReceiptResult.model_fields) == [
        "schema_version",
        "receipt",
        "evidence",
    ]
    restored = AuthorAgentReceiptResult.model_validate(
        result.model_dump(mode="json")
    )
    assert restored == result
    assert result.model_dump_json() == restored.model_dump_json()
    with pytest.raises(ValidationError):
        AuthorAgentReceiptResult.model_validate(
            {**result.model_dump(mode="json"), "schema_version": "v2"}
        )
    with pytest.raises(ValidationError):
        AuthorAgentReceiptResult.model_validate(
            {
                **result.model_dump(mode="json"),
                "receipt": result.receipt.model_dump(mode="json"),
                "extra": 1,
            }
        )


def test_common_input_boundary_exact_bytes_size_and_store_type(tmp_path):
    store = _store(tmp_path)
    payload = _payload(_generic())
    for bad_payload in (
        bytearray(payload),
        memoryview(payload),
        str(payload),
        123,
        None,
        [payload],
    ):
        with pytest.raises(AuthorAgentReceiptPayloadError):
            AuthorAgentReceiptNormalizer.normalize_generic(
                bad_payload,
                expected_subject_digest=SUBJECT,
                artifact_store=store,
            )
    for bad_size in (b"", payload[:1] * 1_048_577):
        with pytest.raises(AuthorAgentReceiptPayloadError):
            AuthorAgentReceiptNormalizer.normalize_generic(
                bad_size,
                expected_subject_digest=SUBJECT,
                artifact_store=store,
            )
    for bad_digest in (
        "sha256:XYZ",
        "sha256:" + "0" * 63,
        "sha256:" + "0" * 65,
        "SHA256:" + "0" * 64,
        "sha256:" + "A" * 64,
        "",
        None,
        123,
    ):
        with pytest.raises(AuthorAgentReceiptPayloadError):
            AuthorAgentReceiptNormalizer.normalize_generic(
                payload,
                expected_subject_digest=bad_digest,
                artifact_store=store,
            )

    class StoreSubclass(ArtifactStore):
        pass

    for bad_store in (
        {},
        None,
        "store",
        StoreSubclass(tmp_path / "sub"),
    ):
        with pytest.raises(TypeError):
            AuthorAgentReceiptNormalizer.normalize_generic(
                payload,
                expected_subject_digest=SUBJECT,
                artifact_store=bad_store,
            )
    assert _file_set(store) == set()


def test_payload_exact_upper_bound_is_accepted(tmp_path):
    base = _v1_run(final_reply="")
    template = json.dumps(
        base,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    pad = 1_048_576 - len(template)
    assert pad > 0
    base["final_reply"] = "x" * pad
    payload = json.dumps(
        base,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert len(payload) == 1_048_576
    result = AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path),
    )
    assert result.receipt.declared_completion == "provided_by_author_agent"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xef\xbb\xbf{}",
        b'{"a":"\xff"}',
        b'{"a":"x\\u0000"}',
        b'{"a":1,"a":2}',
        b'{"a":{"b":1,"b":2}}',
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":-Infinity}',
        b"[]",
        b'"scalar"',
        b"123",
        b"null",
        b"{",
        b"{} {}",
    ],
)
def test_strict_json_parse_rejections(raw, tmp_path):
    store = _store(tmp_path)
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_generic(
            raw,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            raw,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == set()


def test_deeply_nested_unknown_json_escape_is_sanitized(tmp_path):
    store = _store(tmp_path)
    payload = (
        b'{"x":'
        + b"[" * 1100
        + b'"' + SECRET_MARKER.encode("ascii") + b'"'
        + b"]" * 1100
        + b"}"
    )
    assert len(payload) < 1024 * 1024
    with pytest.raises(AuthorAgentReceiptPayloadError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == "invalid author agent receipt payload"
    assert exc.value.__cause__ is None
    assert SECRET_MARKER not in str(exc.value)
    assert _file_set(store) == set()


def test_generic_minimal_normalization_and_evidence_truncated(tmp_path):
    result = AuthorAgentReceiptNormalizer.normalize_generic(
        _payload(
            {
                "schema_version": "v1",
                "subject_digest": SUBJECT,
                "run_id": "g-min",
                "completion_status": "success",
                "started_at": FIXED_TIME,
                "completed_at": FIXED_TIME_LATER,
            }
        ),
        expected_subject_digest=SUBJECT,
        artifact_store=_store(tmp_path),
    )
    assert result.receipt.source_kind == "generic"
    assert result.receipt.source_schema == "generic_author_receipt.v1"
    assert result.receipt.run_id == "g-min"
    assert result.receipt.missing_fields == tuple(
        name for name in MISSING_ORDER if name != "source_subject_digest"
    )
    assert result.evidence.kind == "author_agent_receipt"
    assert result.evidence.producer == "normalizer.author_agent.generic"
    assert result.evidence.status == "truncated"
    assert result.evidence.trust_level == "declared"


def test_generic_full_normalization_is_declared_success(tmp_path):
    result = _normalize_generic(tmp_path)
    receipt = result.receipt
    assert receipt.session_id == "sess-1"
    assert receipt.provider_refs == ("provider-a",)
    assert receipt.model_refs == ("model-b",)
    assert receipt.tool_names == ("read_file",)
    assert receipt.files_touched == ("src/a.py",)
    assert receipt.command_claims == ("pytest -q",)
    assert receipt.check_claims == ("checks passed",)
    assert receipt.declared_intent == "implement bounded normalization"
    assert receipt.declared_completion == "normalization implemented"
    assert receipt.completion_status == "success"
    assert receipt.input_tokens == 100
    assert receipt.output_tokens == 50
    assert receipt.cost.amount == 0.1
    assert receipt.cost.currency == "CNY"
    assert receipt.missing_fields == ()
    assert result.evidence.status == "success"
    assert result.evidence.trust_level == "declared"
    assert result.evidence.artifact_digest == receipt.raw_artifact_digest
    assert (
        result.evidence.source_ref
        == f"author_agent_receipt:{receipt.raw_artifact_digest}"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("failure", "failure"),
        ("cancelled", "cancelled"),
        ("truncated", "truncated"),
    ],
)
def test_generic_status_precedence_is_frozen(status, expected, tmp_path):
    result = _normalize_generic(tmp_path, completion_status=status)
    assert result.receipt.completion_status == status
    assert result.evidence.status == expected
    assert result.evidence.trust_level == "declared"


def test_generic_subject_mismatch_is_sanitized_and_no_growth(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(AuthorAgentReceiptSubjectMismatch) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            _payload(_generic(subject_digest=OTHER_DIGEST)),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == "author agent receipt subject digest mismatch"
    assert exc.value.__cause__ is None
    assert _file_set(store) == set()


def test_generic_claims_never_elevate_trust_or_kind(tmp_path):
    result = _normalize_generic(
        tmp_path,
        command_claims=["pytest passed", "deterministic run"],
        check_claims=["signed verification passed"],
        declared_intent="pytest passed deterministically and signed",
        declared_completion="deterministic signed success",
    )
    assert result.receipt.trust_level == "declared"
    assert result.evidence.trust_level == "declared"
    assert result.evidence.kind == "author_agent_receipt"
    assert result.evidence.status == "success"
    assert result.evidence.producer == "normalizer.author_agent.generic"


def test_v1_done_normalization_extraction_order_and_cny(tmp_path):
    result = _normalize_v1(tmp_path, _v1_done_run())
    receipt = result.receipt
    assert receipt.source_kind == "codemesh_v1"
    assert receipt.source_schema == "codemesh_v1.RunDetail"
    assert receipt.run_id == "run-001"
    assert receipt.session_id is None
    assert receipt.provider_refs == ()
    assert receipt.model_refs == ("model-a", "model-b")
    assert receipt.tool_names == ("read_file", "write_file")
    assert receipt.files_touched == ("src/a.py", "src/b.py")
    assert receipt.command_claims == ()
    assert receipt.check_claims == ()
    assert receipt.declared_intent is None
    assert receipt.declared_completion == "provided_by_author_agent"
    assert receipt.completion_status == "success"
    assert receipt.input_tokens is None
    assert receipt.output_tokens is None
    assert receipt.cost.amount == 1.25
    assert receipt.cost.currency == "CNY"
    assert receipt.missing_fields == (
        "source_subject_digest",
        "session_id",
        "provider_refs",
        "command_claims",
        "check_claims",
        "declared_intent",
        "input_tokens",
        "output_tokens",
    )
    assert result.evidence.status == "truncated"
    assert result.evidence.producer == "normalizer.author_agent.codemesh_v1"
    assert result.evidence.artifact_digest == receipt.raw_artifact_digest


def test_v1_error_and_cancelled_statuses(tmp_path):
    error_run = _v1_run(
        status="error",
        error="run failed",
        final_reply=None,
        total_cost_rmb=0.4,
        step_results=[
            _v1_step(
                status="error",
                output=None,
                error="step failed",
                cost_rmb=0.4,
                model_used="model-a",
            )
        ],
    )
    error_result = _normalize_v1(tmp_path, error_run)
    assert error_result.receipt.completion_status == "failure"
    assert error_result.receipt.declared_completion is None
    assert error_result.evidence.status == "failure"

    cancelled_run = _v1_run(
        status="cancelled",
        total_cost_rmb=0.1,
        final_reply="partial reply",
        step_results=[
            _v1_step(
                status="cancelled",
                output=None,
                error=None,
                cost_rmb=0.1,
                model_used="model-a",
            )
        ],
    )
    cancelled_result = _normalize_v1(tmp_path, cancelled_run)
    assert cancelled_result.receipt.completion_status == "cancelled"
    assert cancelled_result.evidence.status == "cancelled"
    assert (
        cancelled_result.receipt.declared_completion
        == "provided_by_author_agent"
    )


def test_v1_missing_source_facts_append_fixed_missing_fields(tmp_path):
    run = _v1_run(
        total_cost_rmb=None,
        final_reply=None,
        step_results=[
            _v1_step(
                tool_calls=None,
                file_diffs=None,
                model_used=None,
                cost_rmb=None,
                duration_ms=None,
            )
        ],
    )
    result = _normalize_v1(tmp_path, run)
    assert result.receipt.missing_fields == (
        "source_subject_digest",
        "session_id",
        "provider_refs",
        "model_refs",
        "tool_names",
        "files_touched",
        "command_claims",
        "check_claims",
        "declared_intent",
        "declared_completion",
        "input_tokens",
        "output_tokens",
        "cost",
    )
    assert result.receipt.cost is None
    assert result.evidence.status == "truncated"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"id": "r" * 257},
        {"id": 123},
        {"workflow_id": ""},
        {"workflow_id": "   "},
        {"workflow_id": "w" * 257},
        {"status": "running"},
        {"status": "unknown"},
        {"status": 123},
        {"started_at": "2026-08-25T08:00:00"},
        {"completed_at": "2026-08-25T08:05:00"},
        {"completed_at": None},
        {"completed_at": FIXED_TIME, "started_at": FIXED_TIME_LATER},
        {"total_cost_rmb": -0.01},
        {"total_cost_rmb": float("inf")},
        {"total_cost_rmb": "1.25"},
        {"total_cost_rmb": True},
        {"error": 123},
        {"final_reply": 123},
        {"step_results": "not-a-list"},
        {"step_results": [{}]},
        {"unexpected": 1},
    ],
)
def test_v1_run_boundaries(overrides, tmp_path):
    store = _store(tmp_path)
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            _payload(_v1_run(**overrides)),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == set()


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": 0},
        {"id": -1},
        {"id": 1.5},
        {"id": True},
        {"run_id": "other-run"},
        {"run_id": ""},
        {"step_id": ""},
        {"step_id": "   "},
        {"step_id": 123},
        {"step_order": 0},
        {"step_order": -1},
        {"step_order": 1.5},
        {"step_order": True},
        {"status": "running"},
        {"status": "unknown"},
        {"output": 123},
        {"error": 123},
        {"model_used": ""},
        {"model_used": "m" * 257},
        {"cost_rmb": -0.01},
        {"cost_rmb": float("nan")},
        {"cost_rmb": "0.5"},
        {"cost_rmb": True},
        {"duration_ms": -1},
        {"duration_ms": 1.5},
        {"duration_ms": True},
        {"started_at": "2026-08-25T08:00:05"},
        {"completed_at": "2026-08-25T08:01:00"},
        {"completed_at": None},
        {"started_at": None},
        {
            "started_at": "2026-08-25T08:00:01",
            "completed_at": "2026-08-25T08:00:00+00:00",
        },
        {
            "started_at": "2026-08-25T07:59:59+00:00",
            "completed_at": "2026-08-25T08:01:00+00:00",
        },
        {
            "started_at": "2026-08-25T08:00:05+00:00",
            "completed_at": "2026-08-25T08:05:01+00:00",
        },
        {"tool_calls": "not-a-list"},
        {"tool_calls": [{"name": ""}]},
        {"tool_calls": [{"name": "x" * 257}]},
        {"tool_calls": [{"name": "x", "unexpected": 1}]},
        {"tool_calls": [{"name": "x", "status": "running"}]},
        {"tool_calls": [{"name": "x", "ok": 1}]},
        {"tool_calls": [{"name": "x", "ok": "true"}]},
        {"file_diffs": "not-a-list"},
        {"file_diffs": [{"path": "/abs", "kind": "modified"}]},
        {"file_diffs": [{"path": "../x", "kind": "modified"}]},
        {"file_diffs": [{"path": "a.py", "kind": "unknown"}]},
        {"file_diffs": [{"path": "a.py", "kind": "modified", "before": 123}]},
        {"file_diffs": [{"path": "a.py", "kind": "modified", "truncated": 1}]},
        {"file_diffs": [{"path": "a.py", "kind": "modified", "extra": 1}]},
        {"file_diffs": [{}]},
    ],
)
def test_v1_step_boundaries(overrides, tmp_path):
    store = _store(tmp_path)
    run = _v1_run(step_results=[_v1_step(**overrides)])
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            _payload(run),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == set()


def test_v1_step_id_order_run_id_and_status_cross_rules(tmp_path):
    store = _store(tmp_path)
    invalid_runs = [
        _v1_run(step_results=[_v1_step(id=1), _v1_step(id=1, step_order=2)]),
        _v1_run(
            step_results=[
                _v1_step(),
                _v1_step(id=2, step_order=3),
            ]
        ),
        _v1_run(
            step_results=[
                _v1_step(),
                _v1_step(id=2, step_order=1),
            ]
        ),
        _v1_run(
            step_results=[
                _v1_step(
                    status="error",
                    error="step boom",
                )
            ]
        ),
        _v1_run(
            status="error",
            error=None,
            step_results=[_v1_step()],
        ),
        _v1_run(
            status="error",
            error="   ",
            step_results=[_v1_step()],
        ),
        _v1_run(
            total_cost_rmb=0.4,
            step_results=[
                _v1_step(cost_rmb=0.5),
            ],
        ),
        _v1_run(
            total_cost_rmb=None,
            step_results=[
                _v1_step(cost_rmb=0.5),
            ],
        ),
    ]
    for run in invalid_runs:
        with pytest.raises(AuthorAgentReceiptPayloadError):
            AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
                _payload(run),
                expected_subject_digest=SUBJECT,
                artifact_store=store,
            )
    assert _file_set(store) == set()


def test_v1_duplicate_step_id_fails_closed(tmp_path):
    store = _store(tmp_path)
    run = _v1_run(
        step_results=[
            _v1_step(),
            _v1_step(id=2, step_order=2, step_id="step-1"),
        ]
    )
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            _payload(run),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == set()


def test_v1_array_bounds(tmp_path):
    store = _store(tmp_path)
    too_many_steps = _v1_run(
        step_results=[
            _v1_step(
                id=index + 1,
                step_order=index + 1,
                started_at="2026-08-25T08:00:05+00:00",
                completed_at="2026-08-25T08:00:06+00:00",
                model_used=None,
                cost_rmb=None,
                duration_ms=None,
                tool_calls=None,
                file_diffs=None,
            )
            for index in range(257)
        ]
    )
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            _payload(too_many_steps),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    too_many_calls = _v1_run(
        step_results=[
            _v1_step(
                tool_calls=[
                    {"name": f"tool-{index}"} for index in range(129)
                ]
            )
        ]
    )
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            _payload(too_many_calls),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    too_many_diffs = _v1_run(
        step_results=[
            _v1_step(
                file_diffs=[
                    {"path": f"f{index}.py", "kind": "modified"}
                    for index in range(257)
                ]
            )
        ]
    )
    with pytest.raises(AuthorAgentReceiptPayloadError):
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            _payload(too_many_diffs),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == set()


@pytest.mark.parametrize("field", ["args", "result"])
def test_v1_tool_call_nested_json_validator_escape_is_sanitized(field, tmp_path):
    store = _store(tmp_path)
    run = _v1_done_run()
    marker = f"{SECRET_MARKER}-{field}"
    value: object = marker
    for _ in range(550):
        value = [value]
    run["step_results"][0]["tool_calls"][0][field] = value
    payload = _payload(run)
    assert len(payload) < 1024 * 1024
    with pytest.raises(AuthorAgentReceiptPayloadError) as exc:
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == "invalid author agent receipt payload"
    assert exc.value.__cause__ is None
    assert marker not in str(exc.value)
    assert _file_set(store) == set()


def test_v1_secret_content_never_appears_in_result_or_errors(tmp_path):
    secret = SECRET_MARKER
    run = _v1_done_run()
    run["final_reply"] = f"final {secret}"
    run["error"] = f"run {secret}"
    steps = run["step_results"]
    steps[0]["output"] = f"output {secret}"
    steps[0]["error"] = f"step {secret}"
    steps[0]["tool_calls"][0]["args"] = {"path": secret}
    steps[0]["tool_calls"][0]["result"] = f"result {secret}"
    steps[0]["file_diffs"][0]["before"] = f"before {secret}"
    steps[0]["file_diffs"][0]["after"] = f"after {secret}"
    result = _normalize_v1(tmp_path, run)
    dumped = result.model_dump_json()
    assert secret not in dumped
    assert secret not in result.receipt.model_dump_json()
    assert secret not in result.evidence.model_dump_json()
    assert secret not in json.dumps(result.model_dump(mode="json"))

    store = ArtifactStore(tmp_path / "bad-store")
    bad = dict(run)
    bad["unexpected"] = f"leak {secret}"
    with pytest.raises(AuthorAgentReceiptPayloadError) as exc:
        AuthorAgentReceiptNormalizer.normalize_codemesh_v1(
            _payload(bad),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert secret not in str(exc.value)
    assert exc.value.__cause__ is None
    assert _file_set(store) == set()


def test_generic_secret_claims_never_elevate_or_leak(tmp_path):
    result = _normalize_generic(
        tmp_path,
        declared_intent=SECRET_MARKER,
        declared_completion=f"done {SECRET_MARKER}",
        command_claims=[f"echo {SECRET_MARKER}"],
        check_claims=[SECRET_MARKER],
    )
    assert result.receipt.trust_level == "declared"
    assert result.evidence.trust_level == "declared"
    assert result.evidence.kind == "author_agent_receipt"
    assert result.receipt.declared_intent == SECRET_MARKER


@pytest.mark.parametrize(
    "payload",
    [
        b'{"a":1,"a":2}',
        b'{"a":NaN}',
        b"[]",
        _payload(_generic(schema_version="v2")),
        _payload(_generic(completion_status="unknown")),
        _payload(_generic(subject_digest=OTHER_DIGEST)),
    ],
)
def test_failures_before_put_never_grow_store(payload, tmp_path):
    store = _store(tmp_path)
    with pytest.raises(AuthorAgentReceiptError):
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert _file_set(store) == set()


def test_canonical_helper_failure_is_sanitized_and_no_growth(
    tmp_path, monkeypatch
):
    store = _store(tmp_path)

    def boom(_receipt):
        raise RuntimeError("canonical marker boom")

    monkeypatch.setattr(author_module, "_canonical_receipt_body", boom)
    with pytest.raises(AuthorAgentReceiptPayloadError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            _payload(_generic()),
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == "invalid author agent receipt payload"
    assert exc.value.__cause__ is None
    assert _file_set(store) == set()


def test_artifact_put_verify_get_failures_are_sanitized(tmp_path, monkeypatch):
    store = _store(tmp_path)
    payload = _payload(_generic())
    expected = "author agent receipt artifact persistence failed"

    monkeypatch.setattr(
        store,
        "put_bytes",
        lambda data: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(AuthorAgentReceiptArtifactError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == expected
    assert exc.value.__cause__ is None
    assert _file_set(store) == set()

    real_put = store.put_bytes
    monkeypatch.setattr(store, "put_bytes", lambda data: OTHER_DIGEST)
    with pytest.raises(AuthorAgentReceiptArtifactError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == expected
    assert exc.value.__cause__ is None

    monkeypatch.setattr(store, "put_bytes", real_put)
    monkeypatch.setattr(store, "verify", lambda digest: False)
    with pytest.raises(AuthorAgentReceiptArtifactError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == expected
    assert exc.value.__cause__ is None

    monkeypatch.setattr(
        store,
        "verify",
        lambda digest: (_ for _ in ()).throw(OSError("verify boom")),
    )
    with pytest.raises(AuthorAgentReceiptArtifactError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == expected
    assert exc.value.__cause__ is None

    monkeypatch.setattr(store, "verify", lambda digest: True)
    monkeypatch.setattr(
        store,
        "get_bytes",
        lambda digest: b"corrupted bytes",
    )
    with pytest.raises(AuthorAgentReceiptArtifactError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == expected
    assert exc.value.__cause__ is None

    monkeypatch.setattr(
        store,
        "get_bytes",
        lambda digest: (_ for _ in ()).throw(OSError("get boom")),
    )
    with pytest.raises(AuthorAgentReceiptArtifactError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == expected
    assert exc.value.__cause__ is None

    monkeypatch.setattr(
        store,
        "get_bytes",
        lambda digest: (_ for _ in ()).throw(
            ArtifactNotFoundError("missing")
        ),
    )
    with pytest.raises(AuthorAgentReceiptArtifactError) as exc:
        AuthorAgentReceiptNormalizer.normalize_generic(
            payload,
            expected_subject_digest=SUBJECT,
            artifact_store=store,
        )
    assert str(exc.value) == expected
    assert exc.value.__cause__ is None


def _valid_result_dict(tmp_path) -> dict:
    return _normalize_generic(tmp_path).model_dump(mode="json")


def _rebound_v1_result(
    receipt_data: dict, *, recompute_missing_fields: bool = True
) -> dict:
    receipt = AuthorAgentReceipt.model_validate(receipt_data)
    if recompute_missing_fields:
        receipt = receipt.model_copy(
            update={
                "missing_fields": author_module._compute_missing_fields(
                    receipt
                )
            }
        )
    receipt = receipt.model_copy(
        update={"canonical_digest": author_module._canonical_digest(receipt)}
    )
    receipt = receipt.model_copy(
        update={"receipt_id": author_module._receipt_id(receipt)}
    )
    evidence = Evidence(
        evidence_id=author_module._evidence_id(receipt),
        subject_digest=receipt.subject_digest,
        kind="author_agent_receipt",
        producer=author_module._producer_for(receipt.source_kind),
        artifact_digest=receipt.raw_artifact_digest,
        source_ref=f"author_agent_receipt:{receipt.raw_artifact_digest}",
        trace_id=None,
        status=author_module._evidence_status(receipt),
        trust_level="declared",
        collected_at=receipt.completed_at,
    )
    return {
        "schema_version": "v1",
        "receipt": receipt.model_dump(mode="json"),
        "evidence": evidence.model_dump(mode="json"),
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["receipt"].update(raw_artifact_digest=OTHER_DIGEST),
        lambda data: data["receipt"].update(canonical_digest=OTHER_DIGEST),
        lambda data: data["receipt"].update(
            receipt_id="ar_" + "f" * 32
        ),
        lambda data: data["receipt"].update(missing_fields=["session_id"]),
        lambda data: data["receipt"].update(
            missing_fields=["session_id", "provider_refs"]
        ),
        lambda data: data["receipt"].update(subject_digest=OTHER_DIGEST),
        lambda data: data["receipt"].update(
            source_schema="codemesh_v1.RunDetail"
        ),
        lambda data: data["evidence"].update(
            evidence_id="ev_author_" + "f" * 32
        ),
        lambda data: data["evidence"].update(
            producer="normalizer.author_agent.other"
        ),
        lambda data: data["evidence"].update(kind="other_kind"),
        lambda data: data["evidence"].update(status="failure"),
        lambda data: data["evidence"].update(
            source_ref="author_agent_receipt:" + OTHER_DIGEST
        ),
        lambda data: data["evidence"].update(artifact_digest=OTHER_DIGEST),
        lambda data: data["evidence"].update(trust_level="observed"),
        lambda data: data["evidence"].update(
            collected_at="2026-08-25T09:00:00+00:00"
        ),
        lambda data: data["evidence"].update(subject_digest=OTHER_DIGEST),
        lambda data: data["evidence"].update(trace_id="forged-trace"),
    ],
)
def test_result_rejects_forged_cross_bindings(tmp_path, mutate):
    data = _valid_result_dict(tmp_path)
    mutate(data)
    with pytest.raises(ValidationError):
        AuthorAgentReceiptResult.model_validate(data)


def test_result_rejects_forged_v1_cross_bindings(tmp_path):
    data = _normalize_v1(tmp_path, _v1_done_run()).model_dump(mode="json")
    for mutate in (
        lambda d: d["receipt"].update(
            missing_fields=(
                "source_subject_digest",
                "session_id",
                "provider_refs",
                "model_refs",
                "command_claims",
                "check_claims",
                "declared_intent",
                "input_tokens",
                "output_tokens",
            )
        ),
        lambda d: d["receipt"].update(source_kind="generic"),
        lambda d: d["receipt"].update(
            source_schema="generic_author_receipt.v1"
        ),
        lambda d: d["evidence"].update(
            producer="normalizer.author_agent.generic"
        ),
    ):
        forged = json.loads(json.dumps(data))
        mutate(forged)
        with pytest.raises(ValidationError):
            AuthorAgentReceiptResult.model_validate(forged)


@pytest.mark.parametrize(
    "forge",
    [
        {"session_id": "forged-session"},
        {"provider_refs": ["forged-provider"]},
        {"command_claims": ["forged-command"]},
        {"check_claims": ["forged-check"]},
        {"declared_intent": "forged-intent"},
        {"input_tokens": 1},
        {"output_tokens": 1},
    ],
)
def test_result_rejects_rebound_forged_v1_source_facts(tmp_path, forge):
    data = _normalize_v1(tmp_path, _v1_done_run()).model_dump(mode="json")
    data["receipt"].update(forge)
    forged = _rebound_v1_result(data["receipt"])
    with pytest.raises(ValidationError):
        AuthorAgentReceiptResult.model_validate(forged)


def test_result_rejects_v1_missing_fields_without_source_subject_digest(
    tmp_path,
):
    data = _normalize_v1(tmp_path, _v1_done_run()).model_dump(mode="json")
    assert "source_subject_digest" in data["receipt"]["missing_fields"]
    data["receipt"]["missing_fields"] = [
        name
        for name in data["receipt"]["missing_fields"]
        if name != "source_subject_digest"
    ]
    forged = _rebound_v1_result(
        data["receipt"], recompute_missing_fields=False
    )
    with pytest.raises(ValidationError):
        AuthorAgentReceiptResult.model_validate(forged)


def test_canonical_receipt_id_evidence_id_formulas(tmp_path):
    result = _normalize_generic(tmp_path)
    receipt = result.receipt
    body = _canonical_body(receipt)
    assert receipt.canonical_digest == _sha256(body)
    assert receipt.receipt_id == "ar_" + hashlib.sha256(
        (
            receipt.source_kind
            + receipt.subject_digest
            + receipt.raw_artifact_digest
            + receipt.canonical_digest
        ).encode("ascii")
    ).hexdigest()[:32]
    assert result.evidence.evidence_id == "ev_author_" + hashlib.sha256(
        (
            receipt.receipt_id
            + receipt.raw_artifact_digest
            + receipt.canonical_digest
        ).encode("ascii")
    ).hexdigest()[:32]


def test_deterministic_idempotence_and_exact_raw_artifact(tmp_path):
    store = _store(tmp_path)
    payload = _payload(_generic())
    first = AuthorAgentReceiptNormalizer.normalize_generic(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    second = AuthorAgentReceiptNormalizer.normalize_generic(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=store,
    )
    assert second == first
    assert second.model_dump_json() == first.model_dump_json()
    digest = first.receipt.raw_artifact_digest
    assert store.get_bytes(digest) == payload
    assert store.verify(digest) is True
    assert digest == _sha256(payload)
    assert _file_set(store) == {"sha256/" + digest[7:][:2] + "/" + digest[7:][2:]}

    other_store = _store(tmp_path)
    third = AuthorAgentReceiptNormalizer.normalize_generic(
        payload,
        expected_subject_digest=SUBJECT,
        artifact_store=other_store,
    )
    assert third == first


def test_payload_content_is_never_executed(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("payload content must never be executed")

    monkeypatch.setattr(builtins, "eval", forbidden)
    monkeypatch.setattr(builtins, "exec", forbidden)
    run = _v1_done_run()
    run["final_reply"] = "__import__('os').system('echo pwned')"
    run["step_results"][0]["tool_calls"][0]["args"] = {
        "command": "eval('pwned')",
        "marker": SECRET_MARKER,
    }
    run["step_results"][0]["tool_calls"][0]["result"] = "exec('pwned')"
    result = _normalize_v1(tmp_path, run)
    assert result.evidence.trust_level == "declared"
    assert SECRET_MARKER not in result.model_dump_json()


def test_source_audit_no_forbidden_imports_io_or_execution():
    source = inspect.getsource(author_module)
    tree = ast.parse(source)
    imported_roots = set()
    forbidden_imports = {
        "sqlite3",
        "subprocess",
        "socket",
        "httpx",
        "openai",
        "anthropic",
        "os",
        "sys",
        "pathlib",
        "urllib",
        "requests",
        "shlex",
        "pty",
        "signal",
        "tempfile",
        "pickle",
        "ctypes",
        "importlib",
        "glob",
        "shutil",
        "git",
        "torch",
        "transformers",
    }
    forbidden_calls = {
        "eval",
        "exec",
        "compile",
        "open",
        "input",
        "__import__",
        "system",
        "popen",
        "glob",
        "urlopen",
        "request",
    }
    forbidden_methods = {
        "read_bytes",
        "write_bytes",
        "read_text",
        "write_text",
        "unlink",
        "mkdir",
        "rmdir",
        "system",
        "popen",
        "spawn",
        "connect",
        "urlopen",
        "request",
    }
    forbidden_names = {
        "os",
        "sys",
        "subprocess",
        "socket",
        "httpx",
        "openai",
        "anthropic",
        "pathlib",
        "requests",
        "sqlite3",
        "Path",
        "eval",
        "exec",
        "compile",
        "__import__",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(
                alias.name.split(".")[0] for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in forbidden_calls, func.id
            elif isinstance(func, ast.Attribute):
                assert func.attr not in forbidden_methods, func.attr
        if isinstance(node, ast.Attribute):
            assert node.attr not in forbidden_methods, node.attr
        if isinstance(node, ast.Name):
            assert node.id not in forbidden_names, node.id
    assert imported_roots.isdisjoint(forbidden_imports)
    assert "http://" not in source
    assert "https://" not in source
