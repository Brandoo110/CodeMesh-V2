"""GP-05A focused tests for the fixed OpenAI-compatible reviewer invoker."""

import asyncio
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from assurance.fixed_reviewer_invoker import (
    FixedOpenAICompatibleReviewerInvoker,
    FixedReviewerEndpoint,
)
from assurance.run_service import ReviewerRoute


def _route(**updates):
    values = {
        "provider": "openai-compatible",
        "model_ref": "reviewer-model",
        "timeout_seconds": 5,
        "token_budget": 64,
        "routing_rule": "single_general.v0:fixed",
    }
    values.update(updates)
    return ReviewerRoute.model_validate(values)


def _prompt():
    from tests.test_assurance_single_reviewer import _prompt as build_prompt

    return build_prompt()


def _completion(
    *,
    content="{}",
    model="reviewer-model",
    finish_reason="stop",
    choices=None,
    completion_tokens=None,
):
    if choices is None:
        choices = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ]
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": choices,
    }
    if completion_tokens is not None:
        payload["usage"] = {
            "prompt_tokens": 12,
            "completion_tokens": completion_tokens,
            "total_tokens": 12 + completion_tokens,
        }
    return payload


def _invoker(route, handler):
    seen = []

    async def wrapped(request):
        seen.append(request)
        return await handler(request)

    invoker = FixedOpenAICompatibleReviewerInvoker(
        FixedReviewerEndpoint(
            route=route,
            base_url="https://reviewer.example/v1",
            api_key=SecretStr("test-secret"),
        ),
        transport=httpx.MockTransport(wrapped),
    )
    return invoker, seen


def _json_handler(payload, *, status_code=200, headers=None):
    async def handler(request):
        return httpx.Response(status_code, json=payload, headers=headers)

    return handler


@pytest.mark.parametrize(
    "base_url",
    (
        "https://user:pass@reviewer.example/v1",
        "https://reviewer.example/v1?query=secret",
        "https://reviewer.example/v1#fragment",
        "http://reviewer.example/v1",
    ),
)
def test_endpoint_rejects_each_unsafe_url_form(base_url):
    with pytest.raises(ValueError):
        FixedReviewerEndpoint(
            route=_route(),
            base_url=base_url,
            api_key=SecretStr("test-secret"),
        )


def test_endpoint_rejects_blank_secret_and_nonpositive_budget():
    with pytest.raises(ValueError):
        FixedReviewerEndpoint(
            route=_route(),
            base_url="https://reviewer.example/v1",
            api_key=SecretStr(" "),
        )
    with pytest.raises(ValueError):
        FixedReviewerEndpoint(
            route=_route(token_budget=0),
            base_url="https://reviewer.example/v1",
            api_key=SecretStr("test-secret"),
        )


@pytest.mark.parametrize("keyword", ("client", "http_client"))
def test_public_client_bypass_is_not_supported(keyword):
    with pytest.raises(TypeError):
        FixedOpenAICompatibleReviewerInvoker(
            FixedReviewerEndpoint(
                route=_route(),
                base_url="https://reviewer.example/v1",
                api_key=SecretStr("test-secret"),
            ),
            **{keyword: object()},
        )


def test_happy_path_uses_real_sdk_once_with_exact_safe_payload(caplog):
    caplog.set_level(logging.DEBUG)
    route = _route()
    invoker, seen = _invoker(
        route,
        _json_handler(
            _completion(content=' {"raw": true} '),
            headers={"x-provider-secret": "test-secret"},
        ),
    )
    prompt = _prompt()

    result = asyncio.run(invoker.invoke(prompt, run_id="run-1", route=route))

    assert invoker._client.max_retries == 0
    assert result.status == "success"
    assert result.schema_status == "unverified"
    assert result.raw_response == b' {"raw": true} '
    assert result.usage_status == "unavailable"
    assert result.input_tokens is None
    assert result.output_tokens is None
    assert result.cost_usd is None
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url == "https://reviewer.example/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer test-secret"
    payload = json.loads(seen[0].content)
    assert payload == {
        "model": "reviewer-model",
        "messages": [{"role": "user", "content": prompt.prompt_text}],
        "temperature": 0,
        "n": 1,
        "stream": False,
        "max_tokens": 64,
    }
    assert not {
        "tools",
        "tool_choice",
        "fallback",
        "response_format",
    }.intersection(payload)
    assert "test-secret" not in caplog.text
    assert prompt.prompt_text not in caplog.text
    asyncio.run(invoker.aclose())


def test_deepseek_route_explicitly_disables_thinking():
    route = _route(
        provider="deepseek",
        model_ref="deepseek-v4-flash",
        token_budget=4096,
    )
    invoker, seen = _invoker(
        route,
        _json_handler(_completion(model="deepseek-v4-flash")),
    )

    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=route))

    assert result.status == "success"
    assert len(seen) == 1
    payload = json.loads(seen[0].content)
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 4096
    asyncio.run(invoker.aclose())


def test_forged_prompt_and_mismatched_route_are_rejected_before_transport():
    route = _route()
    invoker, seen = _invoker(route, _json_handler(_completion()))
    forged = _prompt().model_copy(update={"prompt_text": "forged"})

    with pytest.raises(ValueError):
        asyncio.run(invoker.invoke(forged, run_id="run-1", route=route))
    with pytest.raises(ValueError):
        asyncio.run(
            invoker.invoke(
                _prompt(),
                run_id="run-1",
                route=route.model_copy(update={"model_ref": "other-model"}),
            )
        )
    assert seen == []


@pytest.mark.parametrize("status_code", (429, 500))
def test_http_errors_are_provider_failure_with_one_request(status_code, caplog):
    caplog.set_level(logging.DEBUG)
    route = _route()
    invoker, seen = _invoker(
        route,
        _json_handler(
            {"error": {"message": "test-secret"}},
            status_code=status_code,
            headers={"x-provider-secret": "test-secret"},
        ),
    )

    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=route))

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROVIDER_FAILURE"
    assert result.error_message is None
    assert "test-secret" not in repr(result)
    assert "test-secret" not in caplog.text
    assert len(seen) == 1


@pytest.mark.parametrize(
    "payload, expected_status, expected_error",
    (
        (_completion(choices=[]), "failure", "REVIEWER_RESPONSE_MISSING"),
        (
            _completion(
                choices=[
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "a"},
                        "finish_reason": "stop",
                    },
                    {
                        "index": 1,
                        "message": {"role": "assistant", "content": "b"},
                        "finish_reason": "stop",
                    },
                ]
            ),
            "failure",
            "REVIEWER_RESPONSE_MISSING",
        ),
        (_completion(content=None), "failure", "REVIEWER_RESPONSE_MISSING"),
        (
            _completion(finish_reason="length"),
            "budget_exceeded",
            "REVIEWER_BUDGET_EXCEEDED",
        ),
        (
            _completion(
                choices=[
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "{}",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "tool",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "stop",
                    }
                ]
            ),
            "failure",
            "REVIEWER_PROVIDER_FAILURE",
        ),
        (
            _completion(
                choices=[
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "{}",
                            "function_call": {
                                "name": "legacy",
                                "arguments": "{}",
                            },
                        },
                        "finish_reason": "stop",
                    }
                ]
            ),
            "failure",
            "REVIEWER_PROVIDER_FAILURE",
        ),
        (_completion(model="other-model"), "failure", "REVIEWER_PROVIDER_FAILURE"),
    ),
)
def test_response_shapes_have_stable_failure_classification(
    payload, expected_status, expected_error
):
    route = _route()
    invoker, seen = _invoker(route, _json_handler(payload))

    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=route))

    assert result.status == expected_status
    assert result.error_code == expected_error
    assert result.raw_response is None
    assert result.error_message is None
    assert len(seen) == 1


def test_whitespace_content_is_missing_without_domain_raw_bytes():
    route = _route()
    invoker, seen = _invoker(route, _json_handler(_completion(content=" \n\t")))

    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=route))

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_RESPONSE_MISSING"
    assert result.raw_response is None
    assert len(seen) == 1


def test_output_token_budget_and_raw_byte_limit_are_strict():
    route = _route(token_budget=64)
    invoker, _ = _invoker(
        route,
        _json_handler(_completion(completion_tokens=65)),
    )
    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=route))
    assert result.status == "budget_exceeded"
    assert result.error_code == "REVIEWER_BUDGET_EXCEEDED"

    exact, seen_exact = _invoker(
        route,
        _json_handler(_completion(content="x" * (1024 * 1024))),
    )
    result = asyncio.run(exact.invoke(_prompt(), run_id="run-1", route=route))
    assert result.status == "success"
    assert len(result.raw_response) == 1024 * 1024
    assert len(seen_exact) == 1

    oversized, seen_oversized = _invoker(
        route,
        _json_handler(_completion(content="x" * (1024 * 1024 + 1))),
    )
    result = asyncio.run(oversized.invoke(_prompt(), run_id="run-1", route=route))
    assert result.status == "budget_exceeded"
    assert result.error_code == "REVIEWER_BUDGET_EXCEEDED"
    assert len(seen_oversized) == 1


def test_outer_wall_clock_timeout_is_one_request():
    route = _route(timeout_seconds=1)

    async def slow_handler(request):
        await asyncio.sleep(1.2)
        return httpx.Response(200, json=_completion())

    invoker, seen = _invoker(route, slow_handler)
    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=route))

    assert result.status == "timeout"
    assert result.error_code == "REVIEWER_TIMEOUT"
    assert len(seen) == 1


def test_cancelled_error_propagates_and_does_not_become_domain_failure():
    route = _route(timeout_seconds=5)
    started = asyncio.Event()

    async def hanging_handler(request):
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json=_completion())

    invoker, seen = _invoker(route, hanging_handler)

    async def run_and_cancel():
        task = asyncio.create_task(
            invoker.invoke(_prompt(), run_id="run-1", route=route)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())
    assert len(seen) == 1


def test_secret_never_appears_in_repr_exception_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    route = _route()
    secret = "test-secret"

    async def raising_handler(request):
        raise httpx.ConnectError(secret, request=request)

    invoker, seen = _invoker(route, raising_handler)
    endpoint_repr = repr(invoker._endpoint)
    invoker_repr = repr(invoker)
    result = asyncio.run(invoker.invoke(_prompt(), run_id="run-1", route=route))

    assert result.status == "failure"
    assert result.error_code == "REVIEWER_PROVIDER_FAILURE"
    assert len(seen) == 1
    assert secret not in endpoint_repr
    assert secret not in invoker_repr
    assert secret not in repr(result)
    assert secret not in caplog.text
