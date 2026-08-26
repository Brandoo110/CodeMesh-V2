"""Focused offline contract tests for the V2-P8-04 OTLP exporter."""

import ast
import hashlib
import inspect
import json

import pytest

from assurance.integrations.opentelemetry import (
    OTLPTraceExportError,
    OTLPTraceExportResult,
    OTLPTraceExporter,
    OTLPTraceReceipt,
    OTLPTraceRequestIntent,
    canonical_otlp_payload_digest,
)


SUBJECT = "sha256:" + "1" * 64
OBSERVED_AT = 1_787_644_800_000_000_000


def _passport(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "case_id": "case-017",
        "subject_digest": SUBJECT,
        "revision": 7,
        "gate": "ACCEPTED",
        "trust": "declared",
    }
    values.update(overrides)
    return values


def test_contract_models_are_frozen_and_forbid_extra_fields():
    for model in (
        OTLPTraceRequestIntent,
        OTLPTraceReceipt,
        OTLPTraceExportResult,
    ):
        assert model.model_config["frozen"] is True
        assert model.model_config["extra"] == "forbid"


def test_export_builds_default_otlp_http_json_intent_and_preserves_binding():
    result = OTLPTraceExporter.export(
        _passport(), observed_at_unix_nano=OBSERVED_AT
    )

    assert result.request.method == "POST"
    assert result.request.endpoint == "/v1/traces"
    assert result.request.headers == {"Content-Type": "application/json"}
    assert result.receipt.published is False
    assert result.receipt.case_id == "case-017"
    assert result.receipt.subject_digest == SUBJECT
    assert result.receipt.revision == 7
    assert result.receipt.gate == "ACCEPTED"
    assert result.receipt.trust == "declared"

    attributes = result.request.body["resourceSpans"][0]["resource"][
        "attributes"
    ]
    mapped = {
        item["key"]: next(iter(item["value"].values())) for item in attributes
    }
    assert mapped == {
        "codemesh.case_id": "case-017",
        "codemesh.subject_digest": SUBJECT,
        "codemesh.revision": "7",
        "codemesh.gate": "ACCEPTED",
        "codemesh.trust": "declared",
    }


def test_trace_data_is_mapped_deterministically_and_digest_is_recomputed():
    trace_data = [
        {
            "name": "assurance.review",
            "status": "success",
            "attributes": {"role": "architecture", "attempt": 1},
            "start_time_unix_nano": OBSERVED_AT,
            "end_time_unix_nano": OBSERVED_AT + 25,
        }
    ]
    first = OTLPTraceExporter.export(
        _passport(), trace_data=trace_data, observed_at_unix_nano=OBSERVED_AT
    )
    second = OTLPTraceExporter.export(
        json.loads(json.dumps(_passport())),
        trace_data=json.loads(json.dumps(trace_data)),
        observed_at_unix_nano=OBSERVED_AT,
    )

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    payload = json.dumps(
        first.request.body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert first.receipt.payload_digest == "sha256:" + hashlib.sha256(
        payload
    ).hexdigest()
    assert canonical_otlp_payload_digest(first.request.body) == (
        first.receipt.payload_digest
    )


@pytest.mark.parametrize("missing", ["case_id", "subject_digest", "revision", "gate", "trust"])
def test_required_assurance_binding_fields_are_required(missing):
    passport = _passport()
    del passport[missing]
    with pytest.raises(OTLPTraceExportError):
        OTLPTraceExporter.export(
            passport, observed_at_unix_nano=OBSERVED_AT
        )


def test_unknown_trust_and_noncanonical_values_fail_closed():
    with pytest.raises(OTLPTraceExportError):
        OTLPTraceExporter.export(
            _passport(trust="unknown"), observed_at_unix_nano=OBSERVED_AT
        )
    with pytest.raises(OTLPTraceExportError):
        OTLPTraceExporter.export(
            _passport(revision=True), observed_at_unix_nano=OBSERVED_AT
        )

    with pytest.raises(OTLPTraceExportError):
        OTLPTraceExporter.export(
            _passport(trust="declared", trust_level="observed"),
            observed_at_unix_nano=OBSERVED_AT,
        )


def test_custom_endpoint_is_path_only_and_network_is_never_called():
    result = OTLPTraceExporter.export(
        _passport(),
        endpoint="/collector/v1/traces",
        observed_at_unix_nano=OBSERVED_AT,
    )
    assert result.request.endpoint == "/collector/v1/traces"

    with pytest.raises(OTLPTraceExportError):
        OTLPTraceExporter.export(
            _passport(),
            endpoint="https://collector/v1/traces",
            observed_at_unix_nano=OBSERVED_AT,
        )


@pytest.mark.parametrize(
    "endpoint",
    ["//collector/v1/traces", "/../traces", "/%2f/traces", "/v1\\traces"],
)
def test_endpoint_cannot_escape_the_future_transport_base(endpoint):
    with pytest.raises(OTLPTraceExportError):
        OTLPTraceExporter.export(
            _passport(),
            endpoint=endpoint,
            observed_at_unix_nano=OBSERVED_AT,
        )


def test_receipt_and_payload_are_strictly_bound():
    result = OTLPTraceExporter.export(
        _passport(), observed_at_unix_nano=OBSERVED_AT
    )
    with pytest.raises(Exception):
        OTLPTraceExportResult(
            request=result.request,
            receipt=result.receipt.model_copy(update={"payload_digest": "sha256:" + "0" * 64}),
        )
    with pytest.raises(Exception):
        OTLPTraceRequestIntent.model_validate(
            {**result.request.model_dump(), "token": "secret"}
        )

    with pytest.raises(ValueError):
        OTLPTraceExportResult(
            request=result.request,
            receipt=result.receipt.model_copy(update={"case_id": "case-forged"}),
        )


def test_otlp_span_has_valid_ids_times_and_protojson_int64_values():
    result = OTLPTraceExporter.export(
        _passport(), observed_at_unix_nano=OBSERVED_AT
    )
    span = result.request.body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]

    assert span["traceId"] != "0" * 32
    assert span["spanId"] != "0" * 16
    assert span["startTimeUnixNano"] == str(OBSERVED_AT)
    assert span["endTimeUnixNano"] == str(OBSERVED_AT)
    assert span["status"]["code"] == 0


def test_otlp_request_is_deeply_immutable():
    result = OTLPTraceExporter.export(
        _passport(), observed_at_unix_nano=OBSERVED_AT
    )
    with pytest.raises(TypeError):
        result.request.headers["Authorization"] = "secret"
    with pytest.raises(TypeError):
        result.request.body["resourceSpans"] = []


@pytest.mark.parametrize(
    "trace_data",
    [
        [{"trace_id": "0" * 32}],
        [{"span_id": "0" * 16}],
        [{"start_time_unix_nano": OBSERVED_AT + 1, "end_time_unix_nano": OBSERVED_AT}],
        [{"status": "mystery"}],
    ],
)
def test_invalid_span_identity_time_or_status_fails_closed(trace_data):
    with pytest.raises(OTLPTraceExportError):
        OTLPTraceExporter.export(
            _passport(),
            trace_data=trace_data,
            observed_at_unix_nano=OBSERVED_AT,
        )


def test_no_network_client_imports_or_http_side_effects():
    import assurance.integrations.opentelemetry as opentelemetry

    tree = ast.parse(inspect.getsource(opentelemetry))
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
