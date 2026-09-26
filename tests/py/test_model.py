"""The SDK adapter, exercised through the real anthropic SDK with a mocked
HTTP transport: the request that would go out is inspected, and canned
server-sent event streams (or failures) come back. No network, no API key,
no cost."""

import json

import anthropic
import httpx2
import pytest

from small_print.model import (
    CONNECT_TIMEOUT_S,
    IDLE_TIMEOUT_S,
    ModelIncomplete,
    ModelRefused,
    ModelUnavailable,
    deadline_s,
)
from small_print.normalise import normalise
from small_print.prompt import MAX_TOKENS, request_params
from small_print.segment import segment
from small_print.verify import ModelOutputError

from conftest import BrokenStream, MemoryviewStream, message, mock_reader, run, sse_body, sse_response

FAKE_KEY = "test-key-for-mock-transport-only"
TEXT = normalise("You keep ownership of your files. We may terminate your account without notice.")
SEGS = segment(TEXT)
GOOD = {"clauses": [{"id": 1, "quote": "You keep ownership", "favours": "you", "reading": "Yours.", "confidence": "high"}]}


def reader_with(handler, clock=None):
    return mock_reader(handler, key=FAKE_KEY, clock=clock)


def ok(content, **kw):
    return lambda r: sse_response(message(content, **kw))


def read(reader, **kw):
    return run(reader.read(request_params(TEXT, SEGS, **kw)))


def test_request_is_streamed_with_the_expected_shape():
    reader, seen = reader_with(ok([
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": json.dumps(GOOD)},
    ]))
    assert read(reader) == {"data": GOOD, "model": "claude-opus-5-5"}

    (req,) = seen
    assert req.method == "POST" and req.url.path == "/v1/messages"
    assert req.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["x-api-key"] == FAKE_KEY
    body = json.loads(req.content)
    assert body["stream"] is True
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == MAX_TOKENS
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["fallbacks"] == "default"
    assert "thinking" not in body and "tool_choice" not in body
    assert "betas" not in body  # sent as a header, not in the body


def test_stream_delivered_in_memoryview_chunks_is_read():
    """Under Pyodide the fetch bridge yields memoryview chunks; the reader's
    transport turns them into bytes for the SDK's event decoder."""
    body = sse_body(message([{"type": "text", "text": json.dumps(GOOD)}]))
    reader, _ = reader_with(lambda r: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, stream=MemoryviewStream(body)))
    assert read(reader)["data"] == GOOD


def test_client_never_retries_and_has_bounded_timeouts():
    reader, _ = reader_with(ok([]))
    client = reader._client
    assert client.max_retries == 0
    assert client.timeout.connect == CONNECT_TIMEOUT_S
    assert client.timeout.read == IDLE_TIMEOUT_S
    # Sized for max_tokens: a full-length reply at the assumed floor rate fits.
    assert deadline_s(MAX_TOKENS) >= MAX_TOKENS / 50


def test_without_fallbacks_uses_the_plain_messages_endpoint():
    reader, seen = reader_with(ok([{"type": "text", "text": json.dumps(GOOD)}]))
    read(reader, fallbacks=False)
    (req,) = seen
    assert "anthropic-beta" not in req.headers
    assert "fallbacks" not in json.loads(req.content)


def test_the_model_that_answered_is_reported():
    """A refusal fallback: the stream carries a fallback block, and the
    answering model is the one after it."""
    reader, _ = reader_with(ok([
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
         "trigger": {"type": "refusal", "category": None}},
        {"type": "text", "text": json.dumps(GOOD)},
    ]))
    assert read(reader) == {"data": GOOD, "model": "claude-opus-5"}


def test_mid_stream_fallback_that_continues_the_partial_json():
    whole = json.dumps(GOOD)
    reader, _ = reader_with(ok([
        {"type": "text", "text": whole[:20]},
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
         "trigger": {"type": "refusal", "category": None}},
        {"type": "text", "text": whole[20:]},
    ]))
    assert read(reader)["data"] == GOOD


def test_refusal_raises_model_refused():
    reader, _ = reader_with(ok([], stop_reason="refusal",
                               stop_details={"type": "refusal", "category": None, "explanation": None}))
    with pytest.raises(ModelRefused):
        read(reader)


def test_cut_off_reply_is_an_output_error():
    reader, _ = reader_with(ok([{"type": "text", "text": '{"clauses": [{"id": 1'}], stop_reason="max_tokens"))
    with pytest.raises(ModelOutputError):
        read(reader)


def test_non_json_text_is_an_output_error():
    reader, _ = reader_with(ok([{"type": "text", "text": "Sure! Here you go"}]))
    with pytest.raises(ModelOutputError):
        read(reader)


def test_reply_without_text_is_an_output_error():
    reader, _ = reader_with(ok([{"type": "thinking", "thinking": "", "signature": "s"}]))
    with pytest.raises(ModelOutputError):
        read(reader)


@pytest.mark.parametrize("status,kind", [
    (429, "busy"), (529, "busy"), (401, "config"), (403, "config"), (404, "config"), (400, "config"),
    (500, "upstream"), (503, "upstream"),
])
def test_http_error_before_the_stream_opens_is_unavailable_and_not_retried(status, kind):
    reader, seen = reader_with(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "x"}}))
    with pytest.raises(ModelUnavailable) as err:
        read(reader)
    assert err.value.kind == kind
    assert len(seen) == 1  # no automatic retry


def test_connection_refused_is_unavailable():
    def refused(request):
        raise httpx2.ConnectError("connection refused", request=request)

    reader, seen = reader_with(refused)
    with pytest.raises(ModelUnavailable) as err:
        read(reader)
    assert err.value.kind == "network" and len(seen) == 1


def test_timeout_waiting_for_the_stream_is_incomplete_and_not_retried():
    def slow(request):
        raise httpx2.ReadTimeout("no response headers in time", request=request)

    reader, seen = reader_with(slow)
    with pytest.raises(ModelIncomplete) as err:
        read(reader)
    assert err.value.kind == "timeout"
    assert len(seen) == 1  # the SDK would otherwise retry a timeout


def test_connection_lost_after_sending_is_incomplete():
    def dropped(request):
        raise httpx2.RemoteProtocolError("server disconnected", request=request)

    reader, _ = reader_with(dropped)
    with pytest.raises(ModelIncomplete) as err:
        read(reader)
    assert err.value.kind == "interrupted"


@pytest.mark.parametrize("exc,kind", [
    (httpx2.ReadError("connection reset"), "interrupted"),
    (httpx2.ReadTimeout("stream went silent"), "timeout"),
])
def test_stream_that_breaks_after_opening_is_incomplete_with_no_retry(exc, kind):
    head = sse_response(message([{"type": "text", "text": json.dumps(GOOD)}])).content[:300]
    reader, seen = reader_with(lambda r: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, stream=BrokenStream(head, exc)))
    with pytest.raises(ModelIncomplete) as err:
        read(reader)
    assert err.value.kind == kind
    assert len(seen) == 1


def test_error_event_inside_an_open_stream_is_incomplete():
    start = {"type": "message_start", "message": {**message([]), "stop_reason": None}}
    body = (b"event: message_start\ndata: " + json.dumps(start).encode() + b"\n\n"
            b'event: error\ndata: {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}\n\n')
    reader, seen = reader_with(lambda r: httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body))
    with pytest.raises(ModelIncomplete) as err:
        read(reader)
    assert err.value.kind == "interrupted" and len(seen) == 1


def test_a_stream_past_its_overall_deadline_is_stopped_as_a_timeout():
    now = [0.0]

    def clock():
        now[0] += 100.0  # every look at the clock is 100 s later
        return now[0]

    long_text = json.dumps(GOOD) * 40  # many deltas
    reader, seen = reader_with(ok([{"type": "text", "text": long_text}]), clock=clock)
    with pytest.raises(ModelIncomplete) as err:
        read(reader)
    assert err.value.kind == "timeout" and len(seen) == 1


def test_errors_never_carry_the_key():
    reader, _ = reader_with(lambda r: httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}))
    with pytest.raises(ModelUnavailable) as err:
        read(reader)
    assert FAKE_KEY not in repr(err.value) and FAKE_KEY not in str(err.value)
    assert err.value.__cause__ is None  # raised "from None": no SDK error chained in
