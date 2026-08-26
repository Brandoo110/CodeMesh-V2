"""Offline OTLP/HTTP JSON trace intents for CodeMesh assurance passports.

The exporter maps assurance identity and optional trace records to a
deterministic OTLP/HTTP JSON request.  It performs no network, provider,
subprocess, or credential I/O; a later transport may publish the returned
intent after an independent authorization step.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_HEX_SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_UINT64_MAX = (1 << 64) - 1
_TRUST_LEVELS = Literal[
    "declared", "observed", "deterministic", "inferred", "human_attested"
]


class OTLPTraceExportError(ValueError):
    """Base error for invalid offline OTLP trace export input."""


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects mutation after construction."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("OTLP request mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenDict(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _nonblank_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be exactly a string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    return value


def _digest(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be sha256:<64 lowercase hex>")
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise OTLPTraceExportError("OTLP payload is not canonical JSON") from exc


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_otlp_payload_digest(payload: Mapping[str, object]) -> str:
    """Return the digest of an OTLP JSON body using stable canonical encoding."""

    if not isinstance(payload, Mapping):
        raise OTLPTraceExportError("OTLP payload must be a mapping")
    return _sha256(_canonical_json_bytes(payload))


def canonical_otlp_request_digest(
    *,
    method: str,
    endpoint: str,
    headers: Mapping[str, str],
    body: Mapping[str, object],
) -> str:
    """Bind the complete offline HTTP intent, not only its OTLP body."""

    return _sha256(
        _canonical_json_bytes(
            {
                "method": method,
                "endpoint": endpoint,
                "headers": dict(headers),
                "body": body,
            }
        )
    )


class OTLPTraceRequestIntent(BaseModel):
    """An unsubmitted OTLP/HTTP JSON trace request."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

    method: Literal["POST"] = "POST"
    endpoint: str
    headers: _FrozenDict
    body: _FrozenDict

    @field_validator("endpoint", mode="before")
    @classmethod
    def validate_endpoint(cls, value: object) -> str:
        result = _nonblank_text(value, "endpoint")
        if (
            not result.startswith("/")
            or result.startswith("//")
            or any(character in result for character in "?#%\\\x00")
            or any(ord(character) < 32 or ord(character) == 127 for character in result)
            or any(part in {".", ".."} for part in result.split("/"))
        ):
            raise ValueError("endpoint must be a path-only OTLP endpoint")
        return result

    @field_validator("headers", mode="before")
    @classmethod
    def validate_headers(cls, value: object) -> _FrozenDict:
        if not isinstance(value, Mapping):
            raise ValueError("headers must be a mapping")
        headers = dict(value)
        if headers != {"Content-Type": "application/json"}:
            raise ValueError("headers must contain only Content-Type application/json")
        return _FrozenDict(headers)

    @field_validator("body", mode="before")
    @classmethod
    def validate_body(cls, value: object) -> _FrozenDict:
        if not isinstance(value, Mapping):
            raise ValueError("body must be an OTLP JSON mapping")
        frozen = _freeze_json(value)
        if not isinstance(frozen, _FrozenDict):  # pragma: no cover - defensive
            raise ValueError("body must be an OTLP JSON mapping")
        return frozen


class OTLPTraceReceipt(BaseModel):
    """The passport binding and non-publication receipt for one OTLP intent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    case_id: str
    subject_digest: str
    revision: int
    gate: str
    trust: _TRUST_LEVELS
    payload_digest: str
    request_digest: str
    published: Literal[False] = False

    @field_validator("case_id", "gate", mode="before")
    @classmethod
    def validate_text(cls, value: object, info) -> str:
        return _nonblank_text(value, info.field_name)

    @field_validator(
        "subject_digest", "payload_digest", "request_digest", mode="before"
    )
    @classmethod
    def validate_digests(cls, value: object, info) -> str:
        return _digest(value, info.field_name)

    @field_validator("revision", mode="before")
    @classmethod
    def validate_revision(cls, value: object) -> int:
        if type(value) is not int or value < 0:
            raise ValueError("revision must be a non-negative integer")
        return value


class OTLPTraceExportResult(BaseModel):
    """An OTLP request intent and its immutable receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: OTLPTraceRequestIntent
    receipt: OTLPTraceReceipt

    @model_validator(mode="after")
    def validate_digest_binding(self) -> "OTLPTraceExportResult":
        expected = canonical_otlp_payload_digest(self.request.body)
        if self.receipt.payload_digest != expected:
            raise ValueError("receipt payload_digest must match canonical body")
        expected_request = canonical_otlp_request_digest(
            method=self.request.method,
            endpoint=self.request.endpoint,
            headers=self.request.headers,
            body=self.request.body,
        )
        if self.receipt.request_digest != expected_request:
            raise ValueError("receipt request_digest must match the complete intent")
        binding = _binding_from_otlp_body(self.request.body)
        receipt_binding = (
            self.receipt.case_id,
            self.receipt.subject_digest,
            self.receipt.revision,
            self.receipt.gate,
            self.receipt.trust,
        )
        if binding != receipt_binding:
            raise ValueError("receipt identity must match OTLP resource attributes")
        return self


def _passport_value(passport: Mapping[str, object], key: str) -> object:
    if key not in passport:
        raise OTLPTraceExportError(f"passport is missing required field {key}")
    return passport[key]


def _binding(passport: Mapping[str, object]) -> tuple[str, str, int, str, str]:
    try:
        case_id = _nonblank_text(_passport_value(passport, "case_id"), "case_id")
        subject_digest = _digest(
            _passport_value(passport, "subject_digest"), "subject_digest"
        )
        revision_value = _passport_value(passport, "revision")
        if type(revision_value) is not int or revision_value < 0:
            raise ValueError("revision must be a non-negative integer")
        gate = _nonblank_text(_passport_value(passport, "gate"), "gate")
        if (
            "trust" in passport
            and "trust_level" in passport
            and passport["trust"] != passport["trust_level"]
        ):
            raise ValueError("trust and trust_level must not conflict")
        trust_value = passport.get("trust", passport.get("trust_level"))
        if trust_value not in {
            "declared",
            "observed",
            "deterministic",
            "inferred",
            "human_attested",
        }:
            raise ValueError("trust must be a known assurance trust level")
        return case_id, subject_digest, revision_value, gate, trust_value
    except OTLPTraceExportError:
        raise
    except (TypeError, ValueError) as exc:
        raise OTLPTraceExportError("invalid assurance passport binding") from exc


def _otlp_value(value: object) -> dict[str, object]:
    if type(value) is bool:
        return {"boolValue": value}
    if type(value) is int:
        if value < -(1 << 63) or value > (1 << 63) - 1:
            raise OTLPTraceExportError("integer trace attributes must fit int64")
        return {"intValue": str(value)}
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            raise OTLPTraceExportError("trace attributes must be finite")
        return {"doubleValue": value}
    if type(value) is str:
        return {"stringValue": value}
    raise OTLPTraceExportError("trace attributes must be scalar JSON values")


def _attributes(values: Mapping[str, object]) -> list[dict[str, object]]:
    if not isinstance(values, Mapping):
        raise OTLPTraceExportError("trace attributes must be a mapping")
    result: list[dict[str, object]] = []
    for key in sorted(values):
        if type(key) is not str or not key.strip() or "\x00" in key:
            raise OTLPTraceExportError("trace attribute keys must be nonblank strings")
        result.append({"key": key, "value": _otlp_value(values[key])})
    return result


def _binding_from_otlp_body(
    body: Mapping[str, object],
) -> tuple[str, str, int, str, str]:
    """Read the five CodeMesh identity fields from one OTLP resource."""

    try:
        resource_spans = body["resourceSpans"]
        if not isinstance(resource_spans, (list, tuple)) or len(resource_spans) != 1:
            raise ValueError("exactly one resourceSpans entry is required")
        resource_span = resource_spans[0]
        if not isinstance(resource_span, Mapping):
            raise ValueError("resourceSpans entry must be a mapping")
        resource = resource_span["resource"]
        if not isinstance(resource, Mapping):
            raise ValueError("resource must be a mapping")
        attributes = resource["attributes"]
        if not isinstance(attributes, (list, tuple)):
            raise ValueError("resource attributes must be a sequence")
        values: dict[str, object] = {}
        for item in attributes:
            if not isinstance(item, Mapping) or set(item) != {"key", "value"}:
                raise ValueError("resource attribute is malformed")
            key = item["key"]
            encoded = item["value"]
            if type(key) is not str or not isinstance(encoded, Mapping):
                raise ValueError("resource attribute is malformed")
            if key in values or len(encoded) != 1:
                raise ValueError("resource attribute is duplicated or ambiguous")
            values[key] = next(iter(encoded.values()))
        expected_keys = {
            "codemesh.case_id",
            "codemesh.subject_digest",
            "codemesh.revision",
            "codemesh.gate",
            "codemesh.trust",
        }
        if set(values) != expected_keys:
            raise ValueError("resource identity attributes must be exact")
        revision_text = values["codemesh.revision"]
        if type(revision_text) is not str or not revision_text.isdigit():
            raise ValueError("resource revision must be an int64 string")
        revision = int(revision_text)
        return (
            _nonblank_text(values["codemesh.case_id"], "case_id"),
            _digest(values["codemesh.subject_digest"], "subject_digest"),
            revision,
            _nonblank_text(values["codemesh.gate"], "gate"),
            _nonblank_text(values["codemesh.trust"], "trust"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("OTLP body does not contain an exact assurance binding") from exc


def _unix_nano(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0 or value > _UINT64_MAX:
        raise OTLPTraceExportError(
            f"{field_name} must be a positive uint64 nanosecond timestamp"
        )
    return value


def _trace_records(
    trace_data: Sequence[Mapping[str, object]] | Mapping[str, object] | None,
) -> tuple[Mapping[str, object], ...]:
    if trace_data is None:
        return ()
    if isinstance(trace_data, Mapping):
        if "spans" in trace_data:
            trace_data = trace_data["spans"]  # type: ignore[assignment]
        else:
            trace_data = (trace_data,)
    if isinstance(trace_data, (str, bytes, bytearray)) or not isinstance(
        trace_data, Sequence
    ):
        raise OTLPTraceExportError("trace_data must be a sequence or mapping")
    records: list[Mapping[str, object]] = []
    for item in trace_data:
        if not isinstance(item, Mapping):
            raise OTLPTraceExportError("trace_data must contain mappings")
        records.append(item)
    return tuple(records)


def _span(
    record: Mapping[str, object],
    *,
    trace_id: str,
    span_id: str,
    observed_at_unix_nano: int,
) -> dict[str, object]:
    name = _nonblank_text(record.get("name", "assurance.trace"), "trace name")
    provided_trace_id = record.get("trace_id", trace_id)
    provided_span_id = record.get("span_id", span_id)
    if (
        type(provided_trace_id) is not str
        or _HEX_TRACE_ID_RE.fullmatch(provided_trace_id) is None
        or int(provided_trace_id, 16) == 0
    ):
        raise OTLPTraceExportError("trace_id must be a non-zero 16-byte hex ID")
    if (
        type(provided_span_id) is not str
        or _HEX_SPAN_ID_RE.fullmatch(provided_span_id) is None
        or int(provided_span_id, 16) == 0
    ):
        raise OTLPTraceExportError("span_id must be a non-zero 8-byte hex ID")
    provided_trace_id = provided_trace_id.lower()
    provided_span_id = provided_span_id.lower()
    start_time = _unix_nano(
        record.get("start_time_unix_nano", observed_at_unix_nano),
        "start_time_unix_nano",
    )
    end_time = _unix_nano(
        record.get("end_time_unix_nano", observed_at_unix_nano),
        "end_time_unix_nano",
    )
    if end_time < start_time:
        raise OTLPTraceExportError(
            "end_time_unix_nano must be greater than or equal to start time"
        )
    attributes_value = record.get("attributes", {})
    attributes = _attributes(attributes_value)
    status_value = record.get("status", "unset")
    if type(status_value) is not str:
        raise OTLPTraceExportError("span status must be a known string")
    normalized_status = status_value.casefold()
    status_codes = {"unset": 0, "ok": 1, "success": 1, "error": 2}
    if normalized_status not in status_codes:
        raise OTLPTraceExportError("span status must be unset, ok, success, or error")
    status_code = status_codes[normalized_status]
    return {
        "traceId": provided_trace_id,
        "spanId": provided_span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_time),
        "endTimeUnixNano": str(end_time),
        "attributes": attributes,
        "status": {"code": status_code},
    }


class OTLPTraceExporter:
    """Map assurance passport/trace data to a deterministic OTLP JSON intent."""

    @staticmethod
    def export(
        passport: Mapping[str, object],
        *,
        trace_data: Sequence[Mapping[str, object]]
        | Mapping[str, object]
        | None = None,
        endpoint: str = "/v1/traces",
        observed_at_unix_nano: int,
    ) -> OTLPTraceExportResult:
        if not isinstance(passport, Mapping):
            raise OTLPTraceExportError("passport must be a mapping")
        observed_at = _unix_nano(
            observed_at_unix_nano, "observed_at_unix_nano"
        )
        case_id, subject_digest, revision, gate, trust = _binding(passport)
        records = _trace_records(
            trace_data if trace_data is not None else passport.get("traces")
        )
        binding_seed = f"{case_id}:{subject_digest}:{revision}:{gate}:{trust}".encode()
        trace_id = hashlib.sha256(binding_seed).hexdigest()[:32]
        spans: list[dict[str, object]] = []
        if not records:
            records = ({"name": "assurance.passport", "attributes": {}},)
        for index, record in enumerate(records):
            span_seed = binding_seed + b":" + str(index).encode()
            span_id = hashlib.sha256(span_seed).hexdigest()[:16]
            spans.append(
                _span(
                    record,
                    trace_id=trace_id,
                    span_id=span_id,
                    observed_at_unix_nano=observed_at,
                )
            )

        resource_attributes = _attributes(
            {
                "codemesh.case_id": case_id,
                "codemesh.subject_digest": subject_digest,
                "codemesh.revision": revision,
                "codemesh.gate": gate,
                "codemesh.trust": trust,
            }
        )
        body: dict[str, object] = {
            "resourceSpans": [
                {
                    "resource": {"attributes": resource_attributes},
                    "scopeSpans": [
                        {
                            "scope": {"name": "codemesh.assurance"},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }
        try:
            request = OTLPTraceRequestIntent(
                endpoint=endpoint,
                headers={"Content-Type": "application/json"},
                body=body,
            )
            receipt = OTLPTraceReceipt(
                case_id=case_id,
                subject_digest=subject_digest,
                revision=revision,
                gate=gate,
                trust=trust,
                payload_digest=canonical_otlp_payload_digest(body),
                request_digest=canonical_otlp_request_digest(
                    method=request.method,
                    endpoint=request.endpoint,
                    headers=request.headers,
                    body=request.body,
                ),
            )
            return OTLPTraceExportResult(request=request, receipt=receipt)
        except OTLPTraceExportError:
            raise
        except Exception as exc:
            raise OTLPTraceExportError("invalid OTLP request intent") from exc


def export(
    passport: Mapping[str, object],
    *,
    trace_data: Sequence[Mapping[str, object]]
    | Mapping[str, object]
    | None = None,
    endpoint: str = "/v1/traces",
    observed_at_unix_nano: int,
) -> OTLPTraceExportResult:
    """Convenience wrapper around :meth:`OTLPTraceExporter.export`."""

    return OTLPTraceExporter.export(
        passport,
        trace_data=trace_data,
        endpoint=endpoint,
        observed_at_unix_nano=observed_at_unix_nano,
    )


__all__ = [
    "OTLPTraceExportError",
    "OTLPTraceExportResult",
    "OTLPTraceExporter",
    "OTLPTraceReceipt",
    "OTLPTraceRequestIntent",
    "canonical_otlp_payload_digest",
    "canonical_otlp_request_digest",
    "export",
]
