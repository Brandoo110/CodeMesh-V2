"""One-shot, fixed-route OpenAI-compatible reviewer transport.

This module is intentionally a transport seam only.  It does not interpret the
reviewer's JSON response; the caller owns schema normalization and persistence.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping
from urllib.parse import urlsplit

import httpx
from openai import AsyncOpenAI
from pydantic import SecretStr

from .run_service import ReviewerInvocationResponse, ReviewerRoute
from .single_reviewer import SingleReviewerPrompt


_MAX_RESPONSE_BYTES = 1024 * 1024
_MISSING = object()


class _RedactingTransport(httpx.AsyncBaseTransport):
    """Keep provider diagnostics and credentials out of SDK-visible failures."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    @staticmethod
    def _safe_request(request: httpx.Request) -> httpx.Request:
        safe_url_text = str(request.url).split("?", 1)[0].split("#", 1)[0]
        safe_url = httpx.URL(safe_url_text)
        return httpx.Request(
            request.method,
            safe_url,
            headers={},
            content=b"",
        )

    @staticmethod
    def _safe_response(
        request: httpx.Request,
        status_code: int,
        content: bytes,
    ) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"content-type": "application/json"},
            content=content,
            request=_RedactingTransport._safe_request(request),
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        safe_request = self._safe_request(request)
        try:
            response = await self._inner.handle_async_request(request)
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise httpx.ReadTimeout(
                "reviewer transport timeout",
                request=safe_request,
            ) from None
        except Exception:
            raise httpx.ConnectError(
                "reviewer transport failure",
                request=safe_request,
            ) from None

        try:
            try:
                body = await response.aread()
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                raise httpx.ReadTimeout(
                    "reviewer transport timeout",
                    request=safe_request,
                ) from None
            except Exception:
                raise httpx.ConnectError(
                    "reviewer transport failure",
                    request=safe_request,
                ) from None
        finally:
            try:
                await response.aclose()
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException:
                raise httpx.ReadTimeout(
                    "reviewer transport timeout",
                    request=safe_request,
                ) from None
            except Exception:
                raise httpx.ConnectError(
                    "reviewer transport failure",
                    request=safe_request,
                ) from None

        if 200 <= response.status_code < 300:
            return self._safe_response(request, response.status_code, body)
        return self._safe_response(request, response.status_code, b"")

    async def aclose(self) -> None:
        try:
            await self._inner.aclose()
        except asyncio.CancelledError:
            raise
        except httpx.TimeoutException:
            raise RuntimeError("reviewer transport timeout") from None
        except Exception:
            raise RuntimeError("reviewer transport failure") from None


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    return getattr(value, name, _MISSING)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return True
    return type(error).__name__ in {"APITimeoutError", "ConnectTimeout"}


@dataclass(frozen=True)
class FixedReviewerEndpoint:
    """Construction-time endpoint and secret for one immutable reviewer route."""

    route: ReviewerRoute
    base_url: str
    api_key: SecretStr

    def __post_init__(self) -> None:
        if type(self.route) is not ReviewerRoute:
            raise TypeError("route must be an exact ReviewerRoute")
        route = self.route
        if (
            type(route.provider) is not str
            or not route.provider.strip()
            or type(route.model_ref) is not str
            or not route.model_ref.strip()
            or type(route.routing_rule) is not str
            or not route.routing_rule.strip()
        ):
            raise ValueError("reviewer route text fields are invalid")
        if (
            type(route.timeout_seconds) is not int
            or isinstance(route.timeout_seconds, bool)
            or route.timeout_seconds <= 0
        ):
            raise ValueError("reviewer route timeout_seconds must be a positive int")
        if type(route.tool_grants) is not tuple or route.tool_grants != ():
            raise ValueError("reviewer route must not grant tools")
        if (
            type(route.token_budget) is not int
            or isinstance(route.token_budget, bool)
            or route.token_budget <= 0
        ):
            raise ValueError("reviewer route token_budget must be a positive int")

        if type(self.base_url) is not str or not self.base_url.strip():
            raise ValueError("base_url must be a nonblank string")
        if any(char.isspace() or ord(char) < 0x20 for char in self.base_url):
            raise ValueError("base_url contains forbidden characters")
        try:
            parsed = urlsplit(self.base_url)
            hostname = parsed.hostname
            username = parsed.username
            password = parsed.password
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("base_url is invalid") from None
        if (
            parsed.scheme.lower() != "https"
            or hostname is None
            or username is not None
            or password is not None
            or parsed.query
            or parsed.fragment
            or "?" in self.base_url
            or "#" in self.base_url
        ):
            raise ValueError("base_url must be HTTPS without userinfo, query, or fragment")

        key = self.api_key
        if type(key) is str:
            key = SecretStr(key)
        elif type(key) is not SecretStr:
            raise TypeError("api_key must be a SecretStr")
        if not key.get_secret_value().strip():
            raise ValueError("api_key must be nonblank")
        object.__setattr__(self, "api_key", key)


class FixedOpenAICompatibleReviewerInvoker:
    """Invoke one frozen OpenAI-compatible endpoint exactly once per call."""

    def __init__(
        self,
        endpoint: FixedReviewerEndpoint,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if type(endpoint) is not FixedReviewerEndpoint:
            raise TypeError("endpoint must be an exact FixedReviewerEndpoint")

        self._endpoint = endpoint
        self._closed = False
        inner_transport = transport
        if inner_transport is None:
            inner_transport = httpx.AsyncHTTPTransport(retries=0)
        self._transport = _RedactingTransport(inner_transport)
        http_client = httpx.AsyncClient(
            transport=self._transport,
            trust_env=False,
        )
        self._client = AsyncOpenAI(
            api_key=endpoint.api_key.get_secret_value(),
            base_url=endpoint.base_url,
            timeout=endpoint.route.timeout_seconds,
            max_retries=0,
            http_client=http_client,
        )

    async def invoke(
        self,
        prompt: SingleReviewerPrompt,
        *,
        run_id: str,
        route: ReviewerRoute,
    ) -> ReviewerInvocationResponse:
        """Send exactly one user prompt and return transport facts only."""

        if self._closed:
            raise RuntimeError("reviewer invoker is closed")
        if type(route) is not ReviewerRoute or route != self._endpoint.route:
            raise ValueError("reviewer route does not match the fixed endpoint")
        if type(run_id) is not str or not run_id.strip():
            raise ValueError("run_id must be a nonblank string")
        if type(prompt) is not SingleReviewerPrompt:
            raise TypeError("prompt must be an exact SingleReviewerPrompt")
        try:
            checked_prompt = SingleReviewerPrompt.model_validate_json(
                prompt.model_dump_json()
            )
        except Exception:
            raise ValueError("prompt failed deterministic JSON round-trip validation") from None

        route = self._endpoint.route
        started = _now()
        try:
            provider_options = {}
            if route.provider == "deepseek":
                provider_options["extra_body"] = {
                    "thinking": {"type": "disabled"}
                }
            response = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=route.model_ref,
                    messages=[
                        {"role": "user", "content": checked_prompt.prompt_text}
                    ],
                    temperature=0,
                    n=1,
                    stream=False,
                    max_tokens=route.token_budget,
                    **provider_options,
                ),
                timeout=route.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            completed = _now()
            if _is_timeout(error):
                return self._failure(
                    route,
                    started,
                    completed,
                    status="timeout",
                    error_code="REVIEWER_TIMEOUT",
                )
            return self._failure(
                route,
                started,
                completed,
                status="failure",
                error_code="REVIEWER_PROVIDER_FAILURE",
            )

        completed = _now()
        return self._decode_response(route, started, completed, response)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._client, "close", None)
        if close is None:
            close = getattr(self._client, "aclose", None)
        if close is None:
            return
        result = close()
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _failure(
        route: ReviewerRoute,
        started: datetime,
        completed: datetime,
        *,
        status: str,
        error_code: str,
    ) -> ReviewerInvocationResponse:
        return ReviewerInvocationResponse(
            status=status,
            provider=route.provider,
            model_ref=route.model_ref,
            started_at=started,
            completed_at=completed,
            schema_status="not_produced",
            error_code=error_code,
        )

    @classmethod
    def _decode_response(
        cls,
        route: ReviewerRoute,
        started: datetime,
        completed: datetime,
        response: object,
    ) -> ReviewerInvocationResponse:
        """Classify a provider response without retaining provider diagnostics."""

        try:
            model = _field(response, "model")
            choices = _field(response, "choices")
            if type(model) is not str or model != route.model_ref:
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_PROVIDER_FAILURE",
                )
            if type(choices) not in (list, tuple) or len(choices) != 1:
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_RESPONSE_MISSING",
                )
            choice = choices[0]
            finish_reason = _field(choice, "finish_reason")
            if finish_reason == "length":
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="budget_exceeded",
                    error_code="REVIEWER_BUDGET_EXCEEDED",
                )
            if finish_reason != "stop":
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_PROVIDER_FAILURE",
                )
            message = _field(choice, "message")
            if message is _MISSING or message is None:
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_RESPONSE_MISSING",
                )
            tool_calls = _field(message, "tool_calls")
            if tool_calls not in (_MISSING, None, [], ()):
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_PROVIDER_FAILURE",
                )
            function_call = _field(message, "function_call")
            if function_call not in (_MISSING, None):
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_PROVIDER_FAILURE",
                )
            content = _field(message, "content")
            if (
                content is _MISSING
                or content is None
                or content == ""
                or (type(content) is str and not content.strip())
            ):
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_RESPONSE_MISSING",
                )
            if type(content) is not str:
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="failure",
                    error_code="REVIEWER_PROVIDER_FAILURE",
                )
            raw = content.encode("utf-8")
            if len(raw) > _MAX_RESPONSE_BYTES:
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="budget_exceeded",
                    error_code="REVIEWER_BUDGET_EXCEEDED",
                )
            usage = _field(response, "usage")
            output_tokens = _field(usage, "completion_tokens")
            if (
                type(output_tokens) is int
                and not isinstance(output_tokens, bool)
                and output_tokens > route.token_budget
            ):
                return cls._failure(
                    route,
                    started,
                    completed,
                    status="budget_exceeded",
                    error_code="REVIEWER_BUDGET_EXCEEDED",
                )
            return ReviewerInvocationResponse(
                status="success",
                provider=route.provider,
                model_ref=route.model_ref,
                started_at=started,
                completed_at=completed,
                raw_response=raw,
                schema_status="unverified",
                usage_status="unavailable",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return cls._failure(
                route,
                started,
                completed,
                status="failure",
                error_code="REVIEWER_PROVIDER_FAILURE",
            )


__all__ = (
    "FixedReviewerEndpoint",
    "FixedOpenAICompatibleReviewerInvoker",
)
