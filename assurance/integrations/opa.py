"""Offline OPA Data API intent and fail-closed decision normalization.

The adapter builds a path-only ``POST /v1/data/{path}`` request intent.  It
does not contain an HTTP client and never contacts OPA.  A caller may later
feed a provider response into the normalizer, but the local CodeMesh policy
outcome remains the authoritative lower bound: an OPA ``allow`` can never
clear a local block, reject, stale result, or human-required outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from assurance.contracts import PolicyDecision


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPA_STATUSES = Literal[
    "ALLOW",
    "DENY",
    "UNDEFINED",
    "ERROR",
    "UNAVAILABLE",
]
_POLICY_OUTCOMES = Literal[
    "STALE", "BLOCKED", "NEEDS_HUMAN", "PASS", "PASS_WITH_WAIVER"
]


class OPAIntentError(ValueError):
    """Raised when an OPA intent or normalized decision is unsafe."""


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects post-digest mutation."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("OPA intent mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON object keys must be strings")
        return _FrozenDict(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def canonical_opa_json_bytes(value: object) -> bytes:
    """Serialize a JSON value deterministically, rejecting NaN/non-JSON data."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OPAIntentError("value is not canonical JSON") from exc


def _sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _nonblank(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a nonblank string")
    return value


def _validate_data_path(value: object) -> str:
    path = _nonblank(value, "path")
    if (
        path.startswith("/")
        or path.endswith("/")
        or "\\" in path
        or "%" in path
        or "?" in path
        or "#" in path
        or "://" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("path must be a relative OPA data path")
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("path must not contain empty or dot segments")
    if any(not segment.strip() for segment in segments):
        raise ValueError("path segments must not be blank")
    return path


class OPADataAPIIntent(BaseModel):
    """An offline intent for OPA's Data API POST endpoint."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", arbitrary_types_allowed=True
    )

    schema_version: Literal["v1"] = "v1"
    method: Literal["POST"] = "POST"
    path: str
    input_document: Any
    required: StrictBool = False

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, value: object) -> str:
        return _validate_data_path(value)

    @field_validator("input_document", mode="before")
    @classmethod
    def _validate_input_document(cls, value: object) -> object:
        # The OPA input document can be any JSON value, not only an object.
        canonical_opa_json_bytes(value)
        return _freeze_json(value)

    @property
    def endpoint(self) -> str:
        return f"/v1/data/{self.path}"

    @property
    def body(self) -> dict[str, Any]:
        return {"input": self.input_document}

    @property
    def body_digest(self) -> str:
        return _sha256_digest(canonical_opa_json_bytes(self.body))

    @property
    def intent_digest(self) -> str:
        return _sha256_digest(
            canonical_opa_json_bytes(
                {
                    "schema_version": self.schema_version,
                    "method": self.method,
                    "endpoint": self.endpoint,
                    "body": self.body,
                    "required": self.required,
                }
            )
        )


class OPADecision(BaseModel):
    """Normalized provider decision with explicit fail-closed states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    status: _OPA_STATUSES
    allow: StrictBool | None = None
    reason_code: str = Field(min_length=1)

    @field_validator("reason_code", mode="before")
    @classmethod
    def _validate_reason(cls, value: object) -> str:
        return _nonblank(value, "reason_code")

    @model_validator(mode="after")
    def _validate_allow_binding(self) -> "OPADecision":
        if self.status == "ALLOW" and self.allow is not True:
            raise ValueError("ALLOW requires allow=True")
        if self.status == "DENY" and self.allow is not False:
            raise ValueError("DENY requires allow=False")
        if self.status in {"UNDEFINED", "ERROR", "UNAVAILABLE"}:
            if self.allow is not None:
                raise ValueError("non-decision statuses must not claim allow")
        return self


class OPAEvaluationReceipt(BaseModel):
    """The local-authority outcome after offline OPA normalization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["v1"] = "v1"
    local_decision_id: str = Field(min_length=1)
    local_outcome: _POLICY_OUTCOMES
    final_outcome: _POLICY_OUTCOMES
    opa_status: _OPA_STATUSES
    required: StrictBool
    provider_reachable: StrictBool
    decision_available: StrictBool
    authoritative: Literal["local"] = "local"
    intent_digest: str
    local_decision_digest: str
    decision_digest: str

    @field_validator("local_decision_id", mode="before")
    @classmethod
    def _validate_local_decision_id(cls, value: object) -> str:
        return _nonblank(value, "local_decision_id")

    @field_validator(
        "intent_digest", "local_decision_digest", "decision_digest", mode="before"
    )
    @classmethod
    def _validate_digests(cls, value: object) -> str:
        if type(value) is not str or _SHA256_DIGEST_RE.fullmatch(value) is None:
            raise ValueError("digest must be sha256:<64 lowercase hex>")
        return value

    @model_validator(mode="after")
    def _validate_availability(self) -> "OPAEvaluationReceipt":
        if self.provider_reachable is not (self.opa_status != "UNAVAILABLE"):
            raise ValueError("provider_reachable must match the OPA status")
        if self.decision_available is not (self.opa_status in {"ALLOW", "DENY"}):
            raise ValueError("decision_available must match the OPA status")
        return self


class OPAEvaluationResult(BaseModel):
    """Intent, normalized decision, and an anti-forgery bound receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    intent: OPADataAPIIntent
    local_decision: PolicyDecision
    decision: OPADecision
    receipt: OPAEvaluationReceipt

    @model_validator(mode="after")
    def _bind_receipt(self) -> "OPAEvaluationResult":
        if self.receipt.intent_digest != self.intent.intent_digest:
            raise ValueError("receipt intent_digest does not match intent")
        if self.receipt.required is not self.intent.required:
            raise ValueError("receipt required mode does not match intent")
        expected_local_digest = _sha256_digest(
            canonical_opa_json_bytes(self.local_decision.model_dump(mode="json"))
        )
        if self.receipt.local_decision_digest != expected_local_digest:
            raise ValueError("receipt local_decision_digest does not match decision")
        if self.receipt.local_decision_id != self.local_decision.decision_id:
            raise ValueError("receipt local_decision_id does not match decision")
        if self.receipt.local_outcome != self.local_decision.outcome:
            raise ValueError("receipt local_outcome does not match local decision")
        if self.receipt.opa_status != self.decision.status:
            raise ValueError("receipt opa_status does not match OPA decision")
        expected_decision_digest = _sha256_digest(
            canonical_opa_json_bytes(self.decision.model_dump(mode="json"))
        )
        if self.receipt.decision_digest != expected_decision_digest:
            raise ValueError("receipt decision_digest does not match decision")
        expected_final = _final_outcome(
            self.receipt.local_outcome,
            self.decision.status,
            required=self.receipt.required,
        )
        if self.receipt.final_outcome != expected_final:
            raise ValueError("receipt final_outcome is not fail-closed")
        return self


def _final_outcome(local_outcome: str, opa_status: str, *, required: bool) -> str:
    # The local decision is a lower bound. OPA allow has no upgrade power.
    if opa_status in {"UNAVAILABLE", "UNDEFINED", "ERROR"}:
        return "BLOCKED" if required else local_outcome
    if opa_status == "DENY":
        if local_outcome in {"BLOCKED", "REJECTED", "STALE"}:
            return local_outcome
        return "BLOCKED"
    return local_outcome


def _normalize_decision(
    response: Mapping[str, Any] | None,
    *,
    provider_reachable: bool,
) -> OPADecision:
    if type(provider_reachable) is not bool:
        raise OPAIntentError("provider_reachable must be a boolean")
    if not provider_reachable:
        return OPADecision(status="UNAVAILABLE", reason_code="OPA_UNAVAILABLE")
    if response is None:
        return OPADecision(status="UNDEFINED", reason_code="OPA_UNDEFINED")
    if not isinstance(response, Mapping):
        return OPADecision(status="ERROR", reason_code="OPA_INVALID_RESPONSE")
    if "error" in response:
        return OPADecision(status="ERROR", reason_code="OPA_ERROR")
    if "result" not in response or response.get("result") is None:
        return OPADecision(status="UNDEFINED", reason_code="OPA_UNDEFINED")

    value = response["result"]
    if type(value) is bool:
        return OPADecision(
            status="ALLOW" if value else "DENY",
            allow=value,
            reason_code="OPA_ALLOW" if value else "OPA_DENY",
        )
    return OPADecision(status="ERROR", reason_code="OPA_INVALID_RESULT")


class OPADataAdapter:
    """Build offline Data API intents and normalize responses fail-closed."""

    @staticmethod
    def build_intent(
        path: str,
        input_document: Any,
        *,
        required: bool = False,
    ) -> OPADataAPIIntent:
        try:
            return OPADataAPIIntent(
                path=path,
                input_document=input_document,
                required=required,
            )
        except OPAIntentError:
            raise
        except Exception as exc:
            raise OPAIntentError("invalid OPA Data API intent") from exc

    @staticmethod
    def normalize_decision(
        response: Mapping[str, Any] | None,
        *,
        provider_reachable: bool = True,
    ) -> OPADecision:
        try:
            return _normalize_decision(
                response, provider_reachable=provider_reachable
            )
        except OPAIntentError:
            raise
        except Exception as exc:
            raise OPAIntentError("OPA decision normalization failed") from exc

    @staticmethod
    def evaluate(
        local_decision: PolicyDecision,
        intent: OPADataAPIIntent,
        response: Mapping[str, Any] | None,
        *,
        provider_reachable: bool = True,
    ) -> OPAEvaluationResult:
        if type(local_decision) is not PolicyDecision:
            raise OPAIntentError("local_decision must be an exact PolicyDecision")
        if type(intent) is not OPADataAPIIntent:
            raise OPAIntentError("intent must be an exact OPADataAPIIntent")
        if (
            not isinstance(intent.input_document, Mapping)
            or intent.input_document.get("subject_digest")
            != local_decision.subject_digest
        ):
            raise OPAIntentError(
                "intent input subject_digest must match local decision"
            )
        decision = OPADataAdapter.normalize_decision(
            response,
            provider_reachable=provider_reachable,
        )
        local_digest = _sha256_digest(
            canonical_opa_json_bytes(local_decision.model_dump(mode="json"))
        )
        receipt = OPAEvaluationReceipt(
            local_decision_id=local_decision.decision_id,
            local_outcome=local_decision.outcome,
            final_outcome=_final_outcome(
                local_decision.outcome,
                decision.status,
                required=intent.required,
            ),
            opa_status=decision.status,
            required=intent.required,
            provider_reachable=decision.status != "UNAVAILABLE",
            decision_available=decision.status in {"ALLOW", "DENY"},
            intent_digest=intent.intent_digest,
            local_decision_digest=local_digest,
            decision_digest=_sha256_digest(
                canonical_opa_json_bytes(decision.model_dump(mode="json"))
            ),
        )
        return OPAEvaluationResult(
            intent=intent,
            local_decision=local_decision,
            decision=decision,
            receipt=receipt,
        )


__all__ = (
    "OPADataAPIIntent",
    "OPADataAdapter",
    "OPADecision",
    "OPAEvaluationReceipt",
    "OPAEvaluationResult",
    "OPAIntentError",
    "canonical_opa_json_bytes",
)
