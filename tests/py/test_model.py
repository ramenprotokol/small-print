"""The SDK adapter, exercised through the real anthropic SDK with a mocked
HTTP transport: the request that would go out is inspected, and canned
responses come back. No network, no API key, no cost."""

import json

import anthropic
import httpx2
import pytest

from small_print.model import AnthropicReader, ModelRefused, ModelUnavailable
from small_print.normalise import normalise
from small_print.prompt import request_params
from small_print.segment import segment
from small_print.verify import ModelOutputError

from conftest import run

FAKE_KEY = "test-key-for-mock-transport-only"
TEXT = normalise("You keep ownership of your files. We may terminate your account without notice.")
SEGS = segment(TEXT)


def message(content, stop_reason="end_turn", **extra):
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10}, **extra,
    }


def reader_with(handler):
    seen = []

    def wrapped(request: httpx2.Request):
        seen.append(request)
        return handler(request)

    client = anthropic.AsyncAnthropic(
        api_key=FAKE_KEY,
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wrapped)),
    )
    return AnthropicReader(FAKE_KEY, client=client), seen


GOOD = {"clauses": [{"id": 1, "quote": "You keep ownership", "favours": "you", "reading": "Yours.", "confidence": "high"}]}


def test_request_shape_on_the_wire():
    reader, seen = reader_with(lambda r: httpx2.Response(200, json=message([
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": json.dumps(GOOD)},
    ])))
    out = run(reader.read(request_params(TEXT, SEGS)))
    assert out == {"data": GOOD, "model": "claude-opus-5-5"}

    (req,) = seen
    assert req.method == "POST" and req.url.path == "/v1/messages"
    assert req.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["x-api-key"] == FAKE_KEY
    body = json.loads(req.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "thinking" not in body and "tool_choice" not in body
    assert "betas" not in body  # sent as a header, not in the body


def test_without_fallbacks_uses_the_plain_messages_endpoint():
    reader, seen = reader_with(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": json.dumps(GOOD)}])))
    run(reader.read(request_params(TEXT, SEGS, fallbacks=False)))
    (req,) = seen
    assert "anthropic-beta" not in req.headers
    assert "fallbacks" not in json.loads(req.content)


def test_refusal_raises_model_refused():
    reader, _ = reader_with(lambda r: httpx2.Response(200, json=message(
        [], stop_reason="refusal", stop_details={"type": "refusal", "category": None, "explanation": None})))
    with pytest.raises(ModelRefused):
        run(reader.read(request_params(TEXT, SEGS)))


def test_cut_off_reply_is_an_output_error():
    reader, _ = reader_with(lambda r: httpx2.Response(200, json=message(
        [{"type": "text", "text": '{"clauses": [{"id": 1'}], stop_reason="max_tokens")))
    with pytest.raises(ModelOutputError):
        run(reader.read(request_params(TEXT, SEGS)))


def test_non_json_text_is_an_output_error():
    reader, _ = reader_with(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": "Sure! Here you go"}])))
    with pytest.raises(ModelOutputError):
        run(reader.read(request_params(TEXT, SEGS)))


def test_reply_without_text_is_an_output_error():
    reader, _ = reader_with(lambda r: httpx2.Response(200, json=message([{"type": "thinking", "thinking": "", "signature": "s"}])))
    with pytest.raises(ModelOutputError):
        run(reader.read(request_params(TEXT, SEGS)))


@pytest.mark.parametrize("status,kind", [(429, "busy"), (401, "config"), (403, "config"), (400, "config"), (500, "upstream"), (529, "upstream")])
def test_http_errors_map_to_unavailable(status, kind):
    reader, _ = reader_with(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "x"}}))
    with pytest.raises(ModelUnavailable) as err:
        run(reader.read(request_params(TEXT, SEGS)))
    assert err.value.kind == kind


def test_network_failure_maps_to_unavailable():
    def boom(request):
        raise httpx2.ConnectError("no route", request=request)

    reader, _ = reader_with(boom)
    with pytest.raises(ModelUnavailable) as err:
        run(reader.read(request_params(TEXT, SEGS)))
    assert err.value.kind == "network"


def test_errors_never_carry_the_key():
    reader, _ = reader_with(lambda r: httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}))
    with pytest.raises(ModelUnavailable) as err:
        run(reader.read(request_params(TEXT, SEGS)))
    assert FAKE_KEY not in repr(err.value) and FAKE_KEY not in str(err.value)
    assert err.value.__cause__ is None  # raised "from None": no SDK error chained in
