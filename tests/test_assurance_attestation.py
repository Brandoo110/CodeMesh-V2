"""Focused offline contract tests for the V2-P8-05 attestation atom."""

import ast
import base64
import hashlib
import inspect
import json

import pytest
from pydantic import ValidationError

from assurance.integrations.attestation import (
    ATTESTATION_PAYLOAD_TYPE,
    AttestationExportError,
    AttestationExportResult,
    AttestationReceipt,
    DSSEEnvelope,
    DSSESignature,
    InTotoAttestationExporter,
    InTotoStatement,
    InTotoSubject,
    canonical_statement_bytes,
    dsse_pae,
)


SUBJECT = "sha256:" + "a" * 64


class RecordingSigner:
    keyid = "test-signer"

    def __init__(self, signature: bytes = b"signature") -> None:
        self.signature = signature
        self.messages: list[bytes] = []

    def sign(self, message: bytes) -> bytes:
        self.messages.append(message)
        return self.signature


def _export(**overrides: object) -> AttestationExportResult:
    values: dict[str, object] = {
        "subject_digest": SUBJECT,
        "subject_name": "repo/app",
        "predicate": {"case_id": "case-001", "gate": "ACCEPTED"},
    }
    values.update(overrides)
    return InTotoAttestationExporter.export(**values)


def test_contract_models_are_frozen_and_forbid_extra_fields():
    for model in (
        InTotoSubject,
        InTotoStatement,
        DSSESignature,
        DSSEEnvelope,
        AttestationReceipt,
        AttestationExportResult,
    ):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        InTotoSubject.model_validate(
            {"name": "app", "digest": {"sha256": "a" * 64}, "extra": 1}
        )


def test_statement_uses_in_toto_v1_and_unprefixed_sha256_subject_digest():
    result = _export()
    statement = result.statement

    dumped = statement.model_dump(mode="json", by_alias=True)
    assert dumped["_type"] == "https://in-toto.io/Statement/v1"
    assert dumped["subject"] == [{"name": "repo/app", "digest": {"sha256": "a" * 64}}]
    assert dumped["predicateType"] == "https://codemesh.dev/assurance/v1"
    assert dumped["predicate"] == {"case_id": "case-001", "gate": "ACCEPTED"}
    assert "sha256:" not in json.dumps(dumped, sort_keys=True)


def test_statement_and_dsse_payload_are_canonical_and_byte_stable():
    first = _export(predicate={"z": 1, "a": "中文"})
    second = _export(
        predicate=json.loads(json.dumps({"a": "中文", "z": 1}, ensure_ascii=False))
    )

    expected = json.dumps(
        first.statement.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert canonical_statement_bytes(first.statement) == expected
    assert first.envelope.payload == second.envelope.payload
    assert first.receipt.payload_digest == (
        "sha256:" + hashlib.sha256(expected).hexdigest()
    )
    assert base64.b64decode(first.envelope.payload) == expected


def test_dsse_pae_uses_utf8_byte_lengths_exactly():
    assert dsse_pae("http://example.com/HelloWorld", b"hello world") == (
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    )
    assert dsse_pae("类型", "中文".encode("utf-8")) == (
        b"DSSEv1 6 \xe7\xb1\xbb\xe5\x9e\x8b 6 \xe4\xb8\xad\xe6\x96\x87"
    )


def test_without_signer_result_is_prepared_unsigned_and_never_verified():
    result = _export()

    assert result.envelope.payload_type == ATTESTATION_PAYLOAD_TYPE
    assert result.envelope.signatures == ()
    assert result.receipt.signing_status == "prepared"
    assert result.receipt.signature_present is False
    assert result.receipt.verified is False
    assert result.receipt.published is False


def test_injected_signer_receives_pae_and_signature_is_base64_encoded():
    signer = RecordingSigner(signature=b"signed bytes")
    result = _export(signer=signer)

    payload = base64.b64decode(result.envelope.payload)
    expected_pae = dsse_pae(result.envelope.payload_type, payload)
    assert signer.messages == [expected_pae]
    assert result.envelope.signatures == (
        DSSESignature(keyid="test-signer", sig=base64.b64encode(b"signed bytes").decode()),
    )
    assert result.receipt.signing_status == "signature_present"
    assert result.receipt.signature_present is True
    assert result.receipt.verified is False
    assert result.receipt.published is False
    assert result.receipt.pae_digest == "sha256:" + hashlib.sha256(expected_pae).hexdigest()


def test_signer_is_injected_but_verification_is_not_claimed():
    signer = RecordingSigner()
    result = _export(signer=signer)

    forged = result.receipt.model_copy(update={"verified": True})
    with pytest.raises(ValidationError):
        AttestationExportResult(
            statement=result.statement,
            envelope=result.envelope,
            receipt=forged,
        )


def test_invalid_subject_or_signer_output_fails_closed():
    with pytest.raises(AttestationExportError):
        _export(subject_digest="a" * 63)
    with pytest.raises(AttestationExportError):
        _export(subject_digest="sha256:" + "A" * 64)

    class EmptySigner:
        keyid = "empty"

        def sign(self, message: bytes) -> bytes:
            return b""

    with pytest.raises(AttestationExportError):
        _export(signer=EmptySigner())


def test_non_json_predicate_and_extra_envelope_fields_fail_closed():
    with pytest.raises(AttestationExportError):
        _export(predicate={"not_json": float("nan")})

    result = _export()
    with pytest.raises(ValidationError):
        DSSEEnvelope.model_validate(
            {**result.envelope.model_dump(by_alias=True), "unexpected": True}
        )


@pytest.mark.parametrize(
    "predicate_type",
    ["not a uri", "HTTPS://codemesh.dev/assurance/v1", "https://CodeMesh.dev/v1"],
)
def test_predicate_type_must_be_a_case_normalized_absolute_type_uri(predicate_type):
    with pytest.raises(AttestationExportError):
        _export(predicate_type=predicate_type)


def test_statement_nested_json_is_immutable_after_receipt_binding():
    result = _export()
    with pytest.raises(TypeError):
        result.statement.predicate["gate"] = "REJECTED"
    with pytest.raises(TypeError):
        result.statement.subject[0].digest["sha256"] = "b" * 64


def test_no_network_client_imports_or_http_side_effects():
    import assurance.integrations.attestation as attestation

    tree = ast.parse(inspect.getsource(attestation))
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
