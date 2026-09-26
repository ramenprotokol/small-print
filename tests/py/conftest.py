"""Shared fakes. Nothing here touches the network."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx2
import pytest

from small_print.budget import BudgetLedger
from small_print.model import AnthropicReader

ROOT = Path(__file__).resolve().parents[2]
DEMO = ROOT / "demo"


def run(coro):
    return asyncio.run(coro)


def sqlite_sql():
    """An sql(query, *params) -> [row dict] function over in-memory SQLite,
    mirroring the Durable Object's ctx.storage.sql.exec."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row

    def sql(query, *params):
        return [dict(r) for r in conn.execute(query, params).fetchall()]

    return sql


class MemoryCache:
    def __init__(self):
        self.rows: dict[tuple[str, str], str] = {}
        self.gets = 0
        self.puts = 0

    async def get(self, sha, pipeline):
        self.gets += 1
        raw = self.rows.get((sha, pipeline))
        return None if raw is None else json.loads(raw)

    async def put(self, sha, pipeline, analysis):
        self.puts += 1
        self.rows[(sha, pipeline)] = json.dumps(analysis)


class LedgerBudget:
    """The real BudgetLedger over SQLite, behind the async interface."""

    def __init__(self):
        self.ledger = BudgetLedger(sqlite_sql())
        self.ledger.setup()
        self.refunds = 0

    async def status(self, day, limit, client, per_visitor):
        return self.ledger.status(day, limit, client, per_visitor)

    async def consume(self, day, limit, client, per_visitor):
        return self.ledger.consume(day, limit, client, per_visitor)

    async def refund(self, day, client):
        self.refunds += 1
        self.ledger.refund(day, client)


class ScriptedReader:
    """Stands in for the model. ``respond(params) -> dict | Exception``."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[dict] = []

    async def read(self, params):
        self.calls.append(params)
        out = self.respond(params)
        if isinstance(out, Exception):
            raise out
        return {"data": out, "model": params["model"]}


def message(content, stop_reason="end_turn", model="claude-opus-5-5", **extra):
    """A final Messages API reply, as a dict."""
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10}, **extra,
    }


def sse_body(msg: dict) -> bytes:
    """The server-sent events that stream ``msg``, the way the API sends them."""
    start = {**msg, "content": [], "stop_reason": None, "stop_sequence": None}
    start.pop("stop_details", None)
    events = [("message_start", {"type": "message_start", "message": start})]
    for i, block in enumerate(msg["content"]):
        if block["type"] == "text":
            events.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "text", "text": ""}}))
            text = block["text"]
            for j in range(0, len(text), 40):
                events.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                       "delta": {"type": "text_delta", "text": text[j:j + 40]}}))
        elif block["type"] == "thinking":
            events.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                   "content_block": {"type": "thinking", "thinking": "", "signature": ""}}))
            events.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                   "delta": {"type": "signature_delta", "signature": block["signature"]}}))
        else:  # e.g. a fallback block: start and stop, no deltas
            events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": block}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    delta = {"stop_reason": msg["stop_reason"], "stop_sequence": None}
    if "stop_details" in msg:
        delta["stop_details"] = msg["stop_details"]
    events.append(("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 10}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return b"".join(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events)


def sse_response(msg: dict) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse_body(msg))


class MemoryviewStream(httpx2.AsyncByteStream):
    """A body delivered in small memoryview chunks, the way the Workers
    fetch bridge (httpx2_jsfetch) delivers it."""

    def __init__(self, body: bytes, size: int = 37):
        self.body, self.size = body, size

    async def __aiter__(self):
        for i in range(0, len(self.body), self.size):
            yield memoryview(self.body[i : i + self.size])


class BrokenStream(httpx2.AsyncByteStream):
    """A response body that sends ``head`` and then fails with ``exc``,
    like a connection dropping (or going silent) mid-stream."""

    def __init__(self, head: bytes, exc: Exception):
        self.head, self.exc = head, exc

    async def __aiter__(self):
        yield self.head
        raise self.exc


def mock_reader(handler, key="mock-transport-key", clock=None):
    """The production AnthropicReader (its own retry and timeout settings),
    with only the HTTP transport swapped for ``handler``. Returns the reader
    and the list of requests it sent."""
    seen = []

    def wrapped(request: httpx2.Request):
        seen.append(request)
        return handler(request)

    kwargs = {"clock": clock} if clock else {}
    return AnthropicReader(key, transport=httpx2.MockTransport(wrapped), **kwargs), seen


def demo_text(slug: str) -> str:
    return (DEMO / f"{slug}.txt").read_text(encoding="utf-8")


def demo_analysis(slug: str) -> dict:
    return json.loads((DEMO / f"{slug}.analysis.json").read_text(encoding="utf-8"))


@pytest.fixture
def memory_cache():
    return MemoryCache()


@pytest.fixture
def ledger_budget():
    return LedgerBudget()
