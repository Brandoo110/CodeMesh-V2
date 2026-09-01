"""Live local HTTP entry point for the CodeMesh Assurance Run contract.

The domain service remains server-owned.  This module is intentionally a thin
client: it submits the caller intent to the loopback API, then performs an
authoritative CaseView readback before returning anything to a caller.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError

from .contracts import ExecutionReceipt
from .evidence_artifacts import ArtifactReference


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_JSON_BYTES = 256 * 1024
_MAX_ARTIFACT_INDEX_BYTES = 256 * 1024
_MAX_ARTIFACT_BYTES = 256 * 1024
_DEFAULT_RUN_CREATE_TIMEOUT_SECONDS = 240.0
_MAX_RUN_CREATE_TIMEOUT_SECONDS = 600.0


class AssuranceEntryError(RuntimeError):
    """Base error for a live local Assurance entry failure."""


class AssuranceTransportError(AssuranceEntryError):
    """The local Assurance HTTP service could not be reached."""


class AssuranceResponseError(AssuranceEntryError):
    """The local service returned a non-contract response."""


class AssuranceReadbackError(AssuranceEntryError):
    """The authoritative CaseView did not match the run response."""


@dataclass(frozen=True)
class AssuranceArtifactReadback:
    """One bounded, header- and digest-verified artifact response."""

    case_id: str
    evidence_id: str
    digest: str
    byte_size: int
    data: bytes


class _RunResponse(BaseModel):
    """Strict subset of the server-owned POST response contract."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: StrictStr
    run_id: StrictStr
    request_digest: StrictStr
    cached: StrictBool
    case_id: StrictStr
    case_view: dict[str, Any]


@dataclass(frozen=True)
class AssuranceRunReadback:
    """Run receipt plus the final authoritative CaseView."""

    run_id: str
    request_digest: str
    cached: bool
    case_id: str
    case_view: dict[str, Any]


def _require_nonblank(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be nonblank text")
    if "\x00" in value:
        raise ValueError(f"{field_name} contains NUL")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise AssuranceResponseError(f"assurance service returned an invalid {field_name}")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"unsupported JSON constant {value}")


def _validate_base_url(value: object) -> str:
    raw = _require_nonblank(value, "base_url").rstrip("/")
    try:
        parsed = httpx.URL(raw)
    except Exception as exc:  # pragma: no cover - defensive parser boundary
        raise ValueError("base_url must be a valid HTTP URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("base_url must be an HTTP URL")
    return raw


def _validate_run_create_timeout(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("run_create_timeout must be finite")
    if value <= 0 or value > _MAX_RUN_CREATE_TIMEOUT_SECONDS:
        raise ValueError(
            "run_create_timeout must be > 0 and <= "
            f"{_MAX_RUN_CREATE_TIMEOUT_SECONDS:g} seconds"
        )
    return float(value)


def _drop_freshness_timestamp(value: object, *, in_freshness: bool = False) -> object:
    """Ignore only the live probe timestamp while comparing CaseViews."""

    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            if in_freshness and key == "checked_at":
                continue
            result[key] = _drop_freshness_timestamp(
                item, in_freshness=(key == "freshness")
            )
        return result
    if isinstance(value, list):
        return [
            _drop_freshness_timestamp(item, in_freshness=in_freshness)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            _drop_freshness_timestamp(item, in_freshness=in_freshness)
            for item in value
        )
    return value


def _case_views_match(post_view: Mapping[str, Any], readback: Mapping[str, Any]) -> bool:
    """Compare all server facts except volatile freshness ``checked_at``."""

    post_normalised = _drop_freshness_timestamp(copy.deepcopy(dict(post_view)))
    readback_normalised = _drop_freshness_timestamp(copy.deepcopy(dict(readback)))
    return post_normalised == readback_normalised


class AssuranceHttpClient:
    """Small synchronous client for the loopback Assurance API."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8010",
        *,
        timeout: float = 20.0,
        run_create_timeout: float = _DEFAULT_RUN_CREATE_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("timeout must be positive")
        self._run_create_timeout = _validate_run_create_timeout(run_create_timeout)
        parsed = httpx.URL(self.base_url)
        loopback_hosts = {"127.0.0.1", "localhost", "::1"}
        # Production use is local-first.  Tests may inject a MockTransport and
        # therefore do not need a resolvable loopback hostname.
        if transport is None and parsed.host not in loopback_hosts:
            raise ValueError("assurance API URL must use a loopback host")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=float(timeout),
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AssuranceHttpClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @staticmethod
    def _bounded_content(
        response: httpx.Response,
        *,
        max_bytes: int,
        expected_media_type: str | None = None,
    ) -> bytes:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        declared = response.headers.get("Content-Length")
        declared_size: int | None = None
        if declared is not None:
            if re.fullmatch(r"[0-9]+", declared.strip()) is None:
                raise AssuranceResponseError("assurance service returned invalid response headers")
            declared_size = int(declared)
            if declared_size > max_bytes:
                raise AssuranceResponseError("assurance service response exceeded size limit")
        content = response.content
        if len(content) > max_bytes:
            raise AssuranceResponseError("assurance service response exceeded size limit")
        if declared_size is not None and declared_size != len(content):
            raise AssuranceResponseError("assurance service response size did not match headers")
        if expected_media_type is not None:
            content_type = response.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != expected_media_type:
                raise AssuranceResponseError("assurance service returned an invalid content type")
        return content

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        request_options: dict[str, object] = {
            "json": dict(json_payload) if json_payload is not None else None,
            "headers": dict(headers or {}),
        }
        if timeout is not None:
            request_options["timeout"] = timeout
        try:
            response = self._client.request(
                method,
                endpoint,
                **request_options,
            )
        except httpx.RequestError as exc:
            raise AssuranceTransportError("assurance service is unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise AssuranceResponseError(
                f"assurance service rejected the request (HTTP {response.status_code})"
            )
        return response

    @staticmethod
    def _json(response: httpx.Response) -> object:
        data = AssuranceHttpClient._bounded_content(
            response,
            max_bytes=_MAX_JSON_BYTES,
            expected_media_type="application/json",
        )
        try:
            return json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, TypeError) as exc:
            raise AssuranceResponseError("assurance service returned invalid JSON") from exc

    def run(
        self,
        request: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> AssuranceRunReadback:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        key = _require_nonblank(idempotency_key, "idempotency_key")
        if len(key.encode("utf-8")) > 256:
            raise ValueError("idempotency_key is too long")
        response = self._request(
            "POST",
            "/api/assurance/runs",
            json_payload=request,
            headers={"Idempotency-Key": key},
            timeout=self._run_create_timeout,
        )
        payload = self._json(response)
        try:
            parsed = _RunResponse.model_validate(payload)
        except ValidationError as exc:
            raise AssuranceResponseError("assurance service returned an invalid run response") from exc
        if parsed.schema_version != "v1":
            raise AssuranceResponseError("assurance service returned an unsupported schema")
        if not parsed.run_id.strip() or not parsed.case_id.strip():
            raise AssuranceResponseError("assurance service returned incomplete run facts")
        if not parsed.request_digest.strip():
            raise AssuranceResponseError("assurance service returned an incomplete request digest")
        return AssuranceRunReadback(
            run_id=parsed.run_id,
            request_digest=parsed.request_digest,
            cached=parsed.cached,
            case_id=parsed.case_id,
            case_view=dict(parsed.case_view),
        )

    def get_case(self, case_id: str) -> dict[str, Any]:
        case = _require_nonblank(case_id, "case_id")
        response = self._request(
            "GET",
            f"/api/assurance/changes/{quote(case, safe='')}",
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise AssuranceResponseError("assurance service returned an invalid CaseView")
        if (
            payload.get("schema_version") != "v1"
            or payload.get("case_id") != case
            or _SHA256_RE.fullmatch(str(payload.get("subject_digest"))) is None
        ):
            raise AssuranceResponseError("assurance service returned an invalid CaseView")
        return dict(payload)

    def get_receipt(self, case_id: str) -> dict[str, Any]:
        case = _require_nonblank(case_id, "case_id")
        response = self._request(
            "GET",
            f"/api/assurance/changes/{quote(case, safe='')}/receipt",
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise AssuranceResponseError("assurance service returned an invalid receipt")
        try:
            ExecutionReceipt.model_validate(payload)
        except ValidationError as exc:
            raise AssuranceResponseError("assurance service returned an invalid receipt") from exc
        if _SHA256_RE.fullmatch(str(payload.get("subject_digest"))) is None:
            raise AssuranceResponseError("assurance service returned an invalid receipt")
        return dict(payload)

    def get_passport(self, case_id: str) -> dict[str, Any]:
        case = _require_nonblank(case_id, "case_id")
        response = self._request(
            "GET",
            f"/api/assurance/changes/{quote(case, safe='')}/passport?format=json",
            headers={"Accept": "application/json"},
        )
        payload = self._json(response)
        if (
            not isinstance(payload, dict)
            or payload.get("schema") != "codemesh.assurance.passport.v1"
            or payload.get("case_id") != case
            or _SHA256_RE.fullmatch(str(payload.get("subject_digest"))) is None
            or not isinstance(payload.get("evidence"), list)
        ):
            raise AssuranceResponseError("assurance service returned an invalid passport")
        return dict(payload)

    def get_passport_markdown(self, case_id: str) -> str:
        case = _require_nonblank(case_id, "case_id")
        response = self._request(
            "GET",
            f"/api/assurance/changes/{quote(case, safe='')}/passport?format=markdown",
            headers={"Accept": "text/markdown"},
        )
        data = self._bounded_content(
            response,
            max_bytes=_MAX_JSON_BYTES,
            expected_media_type="text/markdown",
        )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssuranceResponseError(
                "assurance service returned invalid Passport Markdown"
            ) from exc

    def list_artifacts(self, case_id: str, evidence_id: str) -> dict[str, Any]:
        case = _require_nonblank(case_id, "case_id")
        evidence = _require_nonblank(evidence_id, "evidence_id")
        response = self._request(
            "GET",
            f"/api/assurance/changes/{quote(case, safe='')}/evidence/{quote(evidence, safe='')}/artifacts",
        )
        data = self._bounded_content(
            response,
            max_bytes=_MAX_ARTIFACT_INDEX_BYTES,
            expected_media_type="application/json",
        )
        try:
            payload = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            raise AssuranceResponseError(
                "assurance service returned an invalid artifact index"
            ) from exc
        if not isinstance(payload, dict):
            raise AssuranceResponseError("assurance service returned an invalid artifact index")
        if (
            payload.get("schema_version") != "v1"
            or payload.get("case_id") != case
            or payload.get("evidence_id") != evidence
            or type(payload.get("evidence_kind")) is not str
            or not payload["evidence_kind"].strip()
            or not isinstance(payload.get("artifacts"), list)
            or not payload["artifacts"]
            or len(payload["artifacts"]) > 256
        ):
            raise AssuranceResponseError("assurance service returned an invalid artifact index")
        normalized: list[dict[str, Any]] = []
        try:
            for item in payload["artifacts"]:
                normalized.append(
                    ArtifactReference.model_validate(item).model_dump(mode="json")
                )
        except (TypeError, ValueError, ValidationError) as exc:
            raise AssuranceResponseError(
                "assurance service returned an invalid artifact index"
            ) from exc
        return {**payload, "artifacts": normalized}

    def read_artifact(
        self, case_id: str, evidence_id: str, digest: str
    ) -> AssuranceArtifactReadback:
        case = _require_nonblank(case_id, "case_id")
        evidence = _require_nonblank(evidence_id, "evidence_id")
        requested_digest = _require_sha256(digest, "artifact digest")
        response = self._request(
            "GET",
            f"/api/assurance/changes/{quote(case, safe='')}/evidence/{quote(evidence, safe='')}/artifacts/{quote(requested_digest, safe='')}",
            headers={"Accept": "text/plain"},
        )
        data = self._bounded_content(
            response,
            max_bytes=_MAX_ARTIFACT_BYTES,
            expected_media_type="text/plain",
        )
        if response.headers.get("X-Artifact-Digest") != requested_digest:
            raise AssuranceResponseError("artifact digest header did not match request")
        size_header = response.headers.get("X-Artifact-Size")
        if size_header is None or re.fullmatch(r"[0-9]+", size_header.strip()) is None:
            raise AssuranceResponseError("artifact size header was invalid")
        byte_size = int(size_header)
        if byte_size > _MAX_ARTIFACT_BYTES or byte_size != len(data):
            raise AssuranceResponseError("artifact size did not match response bytes")
        actual_digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if actual_digest != requested_digest:
            raise AssuranceResponseError("artifact bytes did not match requested digest")
        return AssuranceArtifactReadback(
            case_id=case,
            evidence_id=evidence,
            digest=requested_digest,
            byte_size=byte_size,
            data=data,
        )

    def run_and_readback(
        self,
        request: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> AssuranceRunReadback:
        submitted = self.run(request, idempotency_key=idempotency_key)
        authoritative = self.get_case(submitted.case_id)
        if authoritative.get("case_id") != submitted.case_id:
            raise AssuranceReadbackError("authoritative CaseView case_id did not match the run")
        if not _case_views_match(submitted.case_view, authoritative):
            raise AssuranceReadbackError(
                "authoritative CaseView did not match the run response"
            )
        return AssuranceRunReadback(
            run_id=submitted.run_id,
            request_digest=submitted.request_digest,
            cached=submitted.cached,
            case_id=submitted.case_id,
            case_view=authoritative,
        )


__all__ = [
    "AssuranceArtifactReadback",
    "AssuranceEntryError",
    "AssuranceHttpClient",
    "AssuranceReadbackError",
    "AssuranceResponseError",
    "AssuranceRunReadback",
    "AssuranceTransportError",
]
