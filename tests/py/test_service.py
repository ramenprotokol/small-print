"""Handlers end to end, with an in-memory cache, the real budget ledger over
SQLite and a scripted model. No network."""

import json

import httpx2
import pytest

from small_print.model import ModelIncomplete, ModelRefused, ModelUnavailable, worst_case_s
from small_print.normalise import normalise
from small_print.prompt import MAX_TOKENS, MODEL
from small_print.segment import clauses, segment
from small_print.service import (
    MAX_AI_CHARS,
    MAX_BODY_BYTES,
    MAX_CHARS,
    MAX_CLAUSES_PER_CALL,
    Config,
    Deps,
    Request,
    collect_body,
    handle,
)
from small_print.verify import ModelOutputError

from conftest import (
    BrokenStream,
    LedgerBudget,
    MemoryCache,
    ScriptedReader,
    demo_analysis,
    demo_text,
    message,
    mock_reader,
    run,
    sse_body,
)

DAY = "2026-09-26"
IP = "203.0.113.7"
SECRET = "unit-test-key-must-never-leak"


def echo_model(bad_quotes=0):
    """Quotes the first six words of each clause, like a well-behaved model;
    the first ``bad_quotes`` entries get a paraphrased quote instead."""

    def respond(params):
        prompt = params["messages"][0]["content"]
        out = []
        for line in prompt.split("\n"):
            if line.startswith("[") and "] " in line:
                cid = int(line[1 : line.index("]")])
                body = line[line.index("] ") + 2 :]
                quote = " ".join(body.split()[:6])
                if len(out) < bad_quotes:
                    quote = "Paraphrased words that are not there"
                out.append({"id": cid, "quote": quote, "favours": "neutral", "reading": "Plain reading.", "confidence": "medium"})
        return {"clauses": out}

    return respond


def make(reader=None, has_key=True, budget=40, per_visitor=8, cache=None, ledger=None, logs=None, origins=()):
    cfg = Config(has_key=has_key, daily_budget=budget, per_visitor=per_visitor, allowed_origins=origins)
    return Deps(
        config=cfg,
        cache=cache or MemoryCache(),
        budget=ledger or LedgerBudget(),
        reader=reader,
        today=DAY,
        client=IP,
        log=(logs.append if logs is not None else (lambda line: None)),
    )


def post(text, deps, raw=None, ctype="application/json"):
    body = raw if raw is not None else json.dumps({"text": text}).encode()
    return run(handle(Request("POST", "/api/analyze", {"content-type": ctype}, body), deps))


TEXT = demo_text("brothbike")
NORM = normalise(TEXT)


def test_analyze_with_model_returns_verified_readings_with_quotes_and_offsets():
    reader = ScriptedReader(echo_model())
    reply = post(TEXT, make(reader))
    assert reply.status == 200
    p = reply.payload
    assert p["mode"] == "live" and p["notice"] is None
    assert p["text"] == NORM
    a = p["analysis"]
    assert a["source"] == "model" and a["model"] == MODEL
    n = len(clauses(segment(NORM)))
    assert a["received"] == n and a["verified"] == n and a["dropped"] == 0
    for r in a["readings"]:
        assert NORM[r["quote_start"] : r["quote_end"]] == r["quote"]
    assert p["budget"]["used"] == 1 and p["budget"]["remaining"] == 39
    assert len(reader.calls) == 1


def test_unverifiable_quotes_are_dropped_and_counted():
    reply = post(TEXT, make(ScriptedReader(echo_model(bad_quotes=3))))
    a = reply.payload["analysis"]
    assert a["dropped"] == 3
    assert a["dropped_reasons"] == {"not_found": 3}
    assert a["verified"] == a["received"] - 3
    # Every surviving quote is still verbatim.
    for r in a["readings"]:
        assert NORM[r["quote_start"] : r["quote_end"]] == r["quote"]


def test_second_request_for_same_text_is_served_from_cache_without_budget():
    reader = ScriptedReader(echo_model())
    deps = make(reader)
    first = post(TEXT, deps)
    # Different raw whitespace, same normalised text: same cache entry.
    second = post(TEXT.replace("\n", "\r\n") + "\n\n\n", deps)
    assert second.payload["analysis"]["source"] == "cache"
    assert second.payload["analysis"]["readings"] == first.payload["analysis"]["readings"]
    assert len(reader.calls) == 1
    assert second.payload["budget"]["used"] == 1


def test_cache_stores_offsets_and_readings_but_never_the_text():
    cache = MemoryCache()
    post(TEXT, make(ScriptedReader(echo_model()), cache=cache))
    (stored,) = cache.rows.values()
    assert "quote" not in json.loads(stored)["readings"][0]
    # No clause text is in the stored row (six-word quotes would show up).
    for s in clauses(segment(NORM)):
        assert NORM[s.start : s.end] not in stored


def test_demo_mode_without_key_segments_but_never_calls_a_model():
    reply = post(TEXT, make(reader=None, has_key=False))
    p = reply.payload
    assert reply.status == 200
    assert p["mode"] == "demo" and p["analysis"] is None
    assert p["notice"]["kind"] == "demo_mode"
    assert len(p["segments"]) > 0 and p["budget"] is None


def test_demo_mode_still_serves_cached_analyses():
    cache = MemoryCache()
    post(TEXT, make(ScriptedReader(echo_model()), cache=cache))
    reply = post(TEXT, make(reader=None, has_key=False, cache=cache))
    assert reply.payload["analysis"]["source"] == "cache"


def test_budget_spent_means_no_model_call_and_a_clear_notice():
    ledger = LedgerBudget()
    reader = ScriptedReader(echo_model())
    deps = make(reader, budget=1, ledger=ledger)
    post(TEXT, deps)
    other = demo_text("pacewren")
    reply = post(other, deps)
    assert reply.payload["notice"]["kind"] == "budget_spent"
    assert reply.payload["analysis"] is None
    assert len(reader.calls) == 1
    assert reply.payload["budget"]["remaining"] == 0


def test_per_visitor_limit_notice():
    reader = ScriptedReader(echo_model())
    deps = make(reader, budget=10, per_visitor=1)
    post(TEXT, deps)
    reply = post(demo_text("pacewren"), deps)
    assert reply.payload["notice"]["kind"] == "visitor_limit"
    assert len(reader.calls) == 1


def test_model_unavailable_refunds_the_budget():
    ledger = LedgerBudget()
    reply = post(TEXT, make(ScriptedReader(lambda p: ModelUnavailable("network")), ledger=ledger))
    assert reply.payload["notice"]["kind"] == "model_error"
    assert ledger.refunds == 1
    assert reply.payload["budget"]["used"] == 0
    assert ledger.ledger.status(DAY, 40)["used"] == 0


def test_timeout_through_the_real_reader_consumes_budget_and_says_so():
    """The SDK stream times out (mocked transport): the call may have been
    billed, so the budget unit stays spent and there is no retry."""
    def slow(request):
        raise httpx2.ReadTimeout("no response in time", request=request)

    reader, seen = mock_reader(slow)
    ledger = LedgerBudget()
    logs = []
    reply = post(TEXT, make(reader, ledger=ledger, logs=logs))
    p = reply.payload
    assert p["notice"]["kind"] == "model_timeout"
    assert "counted against today's budget" in p["notice"]["message"]
    assert p["budget"]["used"] == 1 and ledger.refunds == 0
    assert ledger.ledger.status(DAY, 40)["used"] == 1
    assert len(seen) == 1
    assert json.loads(logs[-1])["error"] == "timeout"


def test_pre_generation_5xx_through_the_real_reader_is_refunded():
    reader, seen = mock_reader(lambda r: httpx2.Response(503, json={"type": "error", "error": {"type": "api_error", "message": "x"}}))
    ledger = LedgerBudget()
    reply = post(TEXT, make(reader, ledger=ledger))
    assert reply.payload["notice"]["kind"] == "model_error"
    assert "Nothing was taken" in reply.payload["notice"]["message"]
    assert reply.payload["budget"]["used"] == 0 and ledger.refunds == 1
    assert len(seen) == 1


def test_stream_that_breaks_after_it_started_is_counted_and_not_retried():
    head = sse_body(message([{"type": "text", "text": '{"clauses": []}'}]))[:250]
    reader, seen = mock_reader(lambda r: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, stream=BrokenStream(head, httpx2.ReadError("reset"))))
    ledger = LedgerBudget()
    reply = post(TEXT, make(reader, ledger=ledger))
    assert reply.payload["notice"]["kind"] == "model_interrupted"
    assert reply.payload["budget"]["used"] == 1 and ledger.refunds == 0
    assert len(seen) == 1


def test_full_path_through_the_real_reader_and_a_streamed_reply():
    def answer(request):
        body = json.loads(request.content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               content=sse_body(message([{"type": "text", "text": json.dumps(echo_model()(body))}])))

    reader, seen = mock_reader(answer)
    reply = post(TEXT, make(reader))
    a = reply.payload["analysis"]
    assert a["source"] == "model" and a["verified"] == a["received"] == len(clauses(segment(NORM)))
    assert len(seen) == 1


@pytest.mark.parametrize("exc,notice,error", [
    (ModelIncomplete("timeout"), "model_timeout", "timeout"),
    (ModelIncomplete("interrupted"), "model_interrupted", "interrupted"),
    (ModelUnavailable("config"), "model_error", "config"),
    (ModelUnavailable("network"), "model_error", "network"),
    (ModelUnavailable("busy"), "model_busy", "busy"),
    (ModelRefused(), "refused", "rejected"),
    (ModelOutputError("cut off"), "bad_output", "bad_output"),
])
def test_failures_log_their_kind_and_only_pre_generation_ones_refund(exc, notice, error):
    ledger = LedgerBudget()
    logs = []
    reply = post(TEXT, make(ScriptedReader(lambda p: exc), ledger=ledger, logs=logs))
    assert reply.payload["notice"]["kind"] == notice
    assert json.loads(logs[-1])["error"] == error
    assert ledger.refunds == (1 if isinstance(exc, ModelUnavailable) else 0)


def test_rate_limited_model_is_reported_as_busy():
    reply = post(TEXT, make(ScriptedReader(lambda p: ModelUnavailable("busy"))))
    assert reply.payload["notice"]["kind"] == "model_busy"


def test_refusal_and_bad_output_are_reported_and_not_cached():
    cache = MemoryCache()
    r1 = post(TEXT, make(ScriptedReader(lambda p: ModelRefused()), cache=cache))
    assert r1.payload["notice"]["kind"] == "refused"
    r2 = post(TEXT, make(ScriptedReader(lambda p: ModelOutputError("cut off")), cache=cache))
    assert r2.payload["notice"]["kind"] == "bad_output"
    r3 = post(TEXT, make(ScriptedReader(lambda p: {"clauses": "nope"}), cache=cache))
    assert r3.payload["notice"]["kind"] == "bad_output"
    assert cache.puts == 0


def test_all_quotes_dropped_is_explained_and_not_cached():
    cache = MemoryCache()
    n = len(clauses(segment(NORM)))
    reply = post(TEXT, make(ScriptedReader(echo_model(bad_quotes=n)), cache=cache))
    assert reply.payload["notice"]["kind"] == "none_verified"
    assert reply.payload["analysis"]["verified"] == 0
    assert reply.payload["analysis"]["dropped"] == n
    assert cache.puts == 0


def test_long_part_is_not_sent_to_the_model():
    assert MAX_AI_CHARS == 10_000  # the page's part size
    reader = ScriptedReader(echo_model())
    ledger = LedgerBudget()
    long_text = ("You agree to these terms for the service. " * 500)[: MAX_AI_CHARS + 100]
    reply = post(long_text, make(reader, ledger=ledger))
    assert reply.payload["notice"]["kind"] == "too_long"
    assert reader.calls == []
    assert ledger.ledger.status(DAY, 40)["used"] == 0


def test_part_with_too_many_clauses_is_not_sent_to_the_model():
    reader = ScriptedReader(echo_model())
    ledger = LedgerBudget()
    many = " ".join(f"Rule {i} applies here." for i in range(MAX_CLAUSES_PER_CALL + 5))
    assert len(normalise(many)) <= MAX_AI_CHARS
    reply = post(many, make(reader, ledger=ledger))
    assert reply.payload["notice"]["kind"] == "too_many_clauses"
    assert reader.calls == []
    assert ledger.ledger.status(DAY, 40)["used"] == 0


def test_weak_analyses_are_shown_but_not_cached():
    """Under 80% of clauses verified: shown to this visitor, never cached."""
    cache = MemoryCache()
    n = len(clauses(segment(NORM)))
    bad = n // 4 + 1  # just over a fifth dropped
    reply = post(TEXT, make(ScriptedReader(echo_model(bad_quotes=bad)), cache=cache))
    assert reply.payload["analysis"]["verified"] == n - bad
    assert cache.puts == 0
    post(TEXT, make(ScriptedReader(echo_model(bad_quotes=n // 5 - 1)), cache=cache))
    assert cache.puts == 1  # most quotes verified: cached


def test_headings_only_text_gets_a_normal_reply_with_a_notice():
    reader = ScriptedReader(echo_model())
    reply = post("TERMS OF SERVICE\n\n1. Definitions", make(reader))
    assert reply.status == 200
    p = reply.payload
    assert p["notice"]["kind"] == "no_clauses" and p["analysis"] is None
    assert [s["kind"] for s in p["segments"]] == ["heading", "heading"]
    assert reader.calls == []


def test_body_is_read_with_a_byte_cap():
    def chunks(parts):
        it = iter(parts)

        async def read_chunk():
            return next(it, None)

        return read_chunk

    assert run(collect_body(chunks([b"ab", b"cd"]), 4)) == b"abcd"
    assert run(collect_body(chunks([]), 4)) == b""
    pulled = []

    def endless():
        async def read_chunk():
            pulled.append(1)
            return b"x" * 1000

        return read_chunk

    assert run(collect_body(endless(), 5000)) is None
    assert len(pulled) == 6  # stopped as soon as it passed the cap


@pytest.mark.parametrize("body,ctype,status,code", [
    (b"not json", "application/json", 400, "bad_json"),
    (b'{"text": 5}', "application/json", 400, "bad_request"),
    (b'["text"]', "application/json", 400, "bad_request"),
    (b'{"text": "hi"}', "text/plain", 415, "unsupported_media_type"),
    (b'{"text": "   \\n  "}', "application/json", 422, "empty"),
    (b"\xff\xfe", "application/json", 400, "bad_json"),
])
def test_bad_input_gets_a_clear_error(body, ctype, status, code):
    reply = post(None, make(), raw=body, ctype=ctype)
    assert reply.status == status
    assert reply.payload["error"] == code
    assert reply.payload["message"]


def test_text_over_the_cap_is_rejected():
    reply = post("a" * (MAX_CHARS + 1), make())
    assert reply.status == 413
    reply = post(None, make(), raw=b"x" * (MAX_BODY_BYTES + 1))
    assert reply.status == 413


def test_status_endpoint_live_and_demo():
    live = run(handle(Request("GET", "/api/status"), make(ScriptedReader(echo_model()))))
    assert live.status == 200
    assert live.payload["mode"] == "live" and live.payload["budget"]["limit"] == 40
    assert live.payload["model"] == "claude-opus-5-5" and live.payload["effort"] == "medium"
    assert live.payload["fallbacks"] is True
    limits = live.payload["limits"]
    assert limits["max_ai_chars"] == MAX_AI_CHARS and limits["max_clauses"] == MAX_CLAUSES_PER_CALL
    # The page's own timeout sits above the Worker's worst case for one call.
    assert limits["timeout_s"] > worst_case_s(MAX_TOKENS)
    demo = run(handle(Request("GET", "/api/status"), make(has_key=False)))
    assert demo.payload["mode"] == "demo" and demo.payload["budget"] is None


def test_routing_methods_and_headers():
    deps = make()
    assert run(handle(Request("GET", "/api/analyze"), deps)).status == 405
    assert run(handle(Request("POST", "/api/status"), deps)).status == 405
    assert run(handle(Request("GET", "/api/nope"), deps)).status == 404
    r = run(handle(Request("GET", "/api/status"), deps))
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff"


def test_cors_only_for_allowed_origins():
    deps = make(origins=("https://small-print.pages.dev",))
    ok = run(handle(Request("OPTIONS", "/api/analyze", {"origin": "https://small-print.pages.dev"}), deps))
    assert ok.status == 204 and ok.body() == ""
    assert ok.headers["access-control-allow-origin"] == "https://small-print.pages.dev"
    other = run(handle(Request("GET", "/api/status", {"origin": "https://evil.example"}), deps))
    assert "access-control-allow-origin" not in other.headers


def test_logs_hold_counts_only_never_text_or_key():
    logs = []
    deps = make(ScriptedReader(echo_model(bad_quotes=1)), logs=logs)
    deps.config.has_key = True
    post(TEXT, deps)
    post(TEXT, deps)
    post(TEXT, make(has_key=False, logs=logs))
    assert len(logs) == 3
    joined = "\n".join(logs)
    assert SECRET not in joined
    for s in clauses(segment(NORM)):
        assert NORM[s.start : s.end][:30] not in joined
    for line in logs:
        assert set(json.loads(line)) == {"event", "chars", "clauses", "source", "verified", "dropped", "notice", "error"}


def test_responses_never_contain_the_key():
    class KeyedReader(ScriptedReader):
        key = SECRET

    reply = post(TEXT, make(KeyedReader(echo_model())))
    assert SECRET not in reply.body()


def test_demo_analysis_through_the_handler_matches_hand_written_readings():
    """The hand-written demo analysis, fed through the handler as if the
    model had returned it, verifies completely."""
    analysis = demo_analysis("nimbus-locker")
    reply = post(demo_text("nimbus-locker"), make(ScriptedReader(lambda p: analysis)))
    a = reply.payload["analysis"]
    assert a["dropped"] == 0 and a["relocated"] == 0
    assert a["verified"] == len(analysis["clauses"]) == 46
