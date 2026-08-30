"""Live local HTTP entry point for the CodeMesh Assurance Run contract.

The domain service remains server-owned.  This module is intentionally a thin
client: it submits the caller intent to the loopback API, then performs an
authoritative CaseView readback before returning anything to a caller.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError


class AssuranceEntryError(RuntimeError):
    """Base error for a live local Assurance entry failure."""


class AssuranceTransportError(AssuranceEntryError):
    """The local Assurance HTTP service could not be reached."""


class AssuranceResponseError(AssuranceEntryError):
    """The local service returned a non-contract response."""


class AssuranceReadbackError(AssuranceEntryError):
    """The authoritative CaseView did not match the run response."""


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


def _validate_base_url(value: object) -> str:
    raw = _require_nonblank(value, "base_url").rstrip("/")
    try:
        parsed = httpx.URL(raw)
    except Exception as exc:  # pragma: no cover - defensive parser boundary
        raise ValueError("base_url must be a valid HTTP URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("base_url must be an HTTP URL")
    return raw


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
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        if type(timeout) not in (int, float) or timeout <= 0:
            raise ValueError("timeout must be positive")
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

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        json_payload: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method,
                endpoint,
                json=dict(json_payload) if json_payload is not None else None,
                headers=dict(headers or {}),
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
        try:
            return response.json()
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
        if payload.get("schema_version") != "v1" or payload.get("case_id") != case:
            raise AssuranceResponseError("assurance service returned an invalid CaseView")
        return dict(payload)

    def get_passport(self, case_id: str) -> dict[str, Any]:
        case = _require_nonblank(case_id, "case_id")
        response = self._request(
            "GET",
            f"/api/assurance/changes/{quote(case, safe='')}/passport",
            headers={"Accept": "application/json"},
        )
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise AssuranceResponseError("assurance service returned an invalid passport")
        if payload.get("case_id") != case:
            raise AssuranceResponseError("assurance service returned a mismatched passport")
        return dict(payload)

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
    "AssuranceEntryError",
    "AssuranceHttpClient",
    "AssuranceReadbackError",
    "AssuranceResponseError",
    "AssuranceRunReadback",
    "AssuranceTransportError",
]
