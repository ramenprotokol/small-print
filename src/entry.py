"""Cloudflare Python Worker entry point: thin glue around small_print.

Everything that decides anything lives in the small_print package and is
tested under CPython. This file only adapts Workers bindings to the
interfaces in small_print.service:

* ``BudgetCounter``: the SQLite-backed Durable Object holding the daily
  budget ledger (one global instance, so increments are atomic);
* ``D1Cache``: the analysis cache in D1;
* ``Default``: the HTTP entry point for /api/*. Every other path is served
  from dist/ by the static-assets layer before this Worker runs.
"""

import json
import warnings
from datetime import datetime, timezone
from urllib.parse import urlparse

from workers import DurableObject, Response, WorkerEntrypoint

# Under Pyodide the SDK's HTTP client runs on the Workers fetch, and it warns
# once that connection-pool options do not apply there. Expected; silenced.
warnings.filterwarnings("ignore", message="The following transport option")

from small_print.budget import BudgetLedger  # noqa: E402
from small_print.model import AnthropicReader  # noqa: E402
from small_print.service import (  # noqa: E402
    BASE_HEADERS,
    MAX_BODY_BYTES,
    Config,
    Deps,
    Reply,
    Request,
    collect_body,
    handle,
)

BUDGET_INSTANCE = "global"


def _py(value):
    """Convert a JS value returned through RPC or D1 into plain Python."""
    return value.to_py() if hasattr(value, "to_py") else value


def _int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class BudgetCounter(DurableObject):
    """The daily budget. Durable Objects run one call at a time, and the
    ledger never awaits between reading and writing, so each consume() is
    atomic."""

    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.ledger = BudgetLedger(self._sql)
        self.ledger.setup()

    def _sql(self, query, *params):
        cursor = self.ctx.storage.sql.exec(query, *params)
        return [_py(row) for row in cursor]

    def status(self, day, limit, client, per_visitor):
        return self.ledger.status(day, limit, client or None, per_visitor)

    def consume(self, day, limit, client, per_visitor):
        return self.ledger.consume(day, limit, client or None, per_visitor)

    def refund(self, day, client):
        self.ledger.refund(day, client or None)
        return True


class DurableBudget:
    def __init__(self, namespace):
        self._stub = namespace.getByName(BUDGET_INSTANCE)

    async def status(self, day, limit, client, per_visitor):
        return _py(await self._stub.status(day, limit, client or "", per_visitor))

    async def consume(self, day, limit, client, per_visitor):
        return _py(await self._stub.consume(day, limit, client or "", per_visitor))

    async def refund(self, day, client):
        await self._stub.refund(day, client or "")


class D1Cache:
    """Content-addressed: a row can only be found by someone who already has
    the exact text. There is no listing endpoint."""

    def __init__(self, db, log):
        self._db = db
        self._log = log

    async def get(self, sha, pipeline):
        try:
            row = await self._db.prepare(
                "SELECT analysis FROM analysis_cache WHERE text_sha256 = ? AND pipeline = ?"
            ).bind(sha, pipeline).first()
        except Exception as exc:  # e.g. migrations not applied: act as a miss
            self._log(json.dumps({"event": "cache_error", "op": "get", "type": type(exc).__name__}))
            return None
        if row is None:
            return None
        try:
            return json.loads(_py(row)["analysis"])
        except (KeyError, TypeError, ValueError):
            return None

    async def put(self, sha, pipeline, analysis):
        try:
            await self._db.prepare(
                "INSERT OR REPLACE INTO analysis_cache (text_sha256, pipeline, analysis, created_at) "
                "VALUES (?, ?, ?, ?)"
            ).bind(sha, pipeline, json.dumps(analysis), datetime.now(timezone.utc).isoformat()).run()
        except Exception as exc:
            self._log(json.dumps({"event": "cache_error", "op": "put", "type": type(exc).__name__}))


def _log(line):
    print(line)


async def _read_body(request):
    """The request body, read chunk by chunk up to MAX_BODY_BYTES. Returns
    None when it is larger (a chunked upload has no Content-Length, so the
    cap cannot be checked up front)."""
    stream = request.js_object.body
    if stream is None or not hasattr(stream, "getReader"):
        return b""
    reader = stream.getReader()

    async def read_chunk():
        result = await reader.read()
        return None if result.done else result.value.to_bytes()

    try:
        return await collect_body(read_chunk, MAX_BODY_BYTES)
    finally:
        try:
            await reader.cancel()  # stops an oversized upload; a no-op once fully read
        except Exception:  # noqa: BLE001 - the stream may already be closed
            pass


class Default(WorkerEntrypoint):
    def _config(self):
        env = self.env
        key = getattr(env, "ANTHROPIC_API_KEY", None)
        origins = str(getattr(env, "ALLOWED_ORIGINS", "") or "")
        return key, Config(
            has_key=bool(key),
            daily_budget=_int(getattr(env, "DAILY_BUDGET", 40), 40),
            per_visitor=_int(getattr(env, "PER_VISITOR_DAILY", 8), 8),
            fallbacks=str(getattr(env, "ANTHROPIC_FALLBACKS", "default")).strip().lower() != "off",
            allowed_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        )

    async def fetch(self, request):
        try:
            return await self._fetch(request)
        except Exception as exc:  # never echo internals (or text) to the client
            _log(json.dumps({"event": "unhandled", "type": type(exc).__name__}))
            reply = Reply(500, {"error": "internal", "message": "Something went wrong on the server."})
            return Response(reply.body(), status=500, headers={"content-type": "application/json; charset=utf-8"})

    async def _fetch(self, request):
        headers = {k.lower(): v for k, v in request.headers.items()}
        length = headers.get("content-length", "")
        too_large = Reply(413, {"error": "too_large", "message": "The request body is too large."})
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            # Refuse before reading the body at all.
            return Response(too_large.body(), status=413, headers=dict(BASE_HEADERS))
        body = await _read_body(request) if request.method == "POST" else b""
        if body is None:
            return Response(too_large.body(), status=413, headers=dict(BASE_HEADERS))

        key, config = self._config()
        reader = None
        if key:
            reader = AnthropicReader(key, base_url=getattr(self.env, "ANTHROPIC_BASE_URL", None))
        deps = Deps(
            config=config,
            cache=D1Cache(self.env.DB, _log),
            budget=DurableBudget(self.env.BUDGET),
            reader=reader,
            today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            client=headers.get("cf-connecting-ip"),
            log=_log,
        )
        req = Request(request.method, urlparse(request.url).path, headers, body)
        reply = await handle(req, deps)
        payload = None if reply.status == 204 else reply.body()
        return Response(payload, status=reply.status, headers=reply.headers)
