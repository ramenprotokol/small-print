"""HTTP handlers, independent of the Workers runtime.

``handle`` takes a plain request (method, path, headers, body) plus its
dependencies (cache, budget, model reader) and returns a plain reply. The
Worker entry point adapts real bindings to these interfaces; the tests use
in-memory fakes and a scripted model, so no test touches the network.

Privacy: the document text and the API key never appear in logs. The only
log line is a count summary per analysis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .model import ModelRefused, ModelUnavailable
from .normalise import normalise, text_sha256
from .prompt import EFFORT, MODEL, pipeline_version, request_params
from .segment import Segment, clauses, segment
from .verify import ModelOutputError, verify, with_quotes

MAX_CHARS = 60_000  # hard cap on a request's text, after normalisation
MAX_AI_CHARS = 16_000  # longest text sent to the model in one call
MAX_BODY_BYTES = 512_000  # JSON body cap, checked before parsing
MAX_CLAUSES_PER_CALL = 120

NOTICES = {
    "demo_mode": "AI reading is off on this server (no API key is set), so this is demo mode. "
    "The clause map is real; the readings need the model. The demo documents show a full reading.",
    "budget_spent": "Today's AI budget is used up. It resets at 00:00 UTC. "
    "Documents that were read before still open from the cache, and the demo documents always work.",
    "visitor_limit": "You have used your AI readings for today. They reset at 00:00 UTC. "
    "Cached documents and the demo documents still work.",
    "too_long": "This part is too long for one reading. The page splits long documents into parts on its own; "
    "send parts of at most 16,000 characters.",
    "too_many_clauses": "This part has too many clauses for one reading. Send a smaller part.",
    "model_busy": "The model is busy right now. Nothing was taken from today's budget. Try again in a minute.",
    "model_error": "The model could not be reached. Nothing was taken from today's budget.",
    "refused": "The model declined to read this text, so there are no readings for it.",
    "bad_output": "The model's reply could not be used (it was cut off or malformed). It still counted against today's budget.",
    "none_verified": "The model replied, but none of its quotes matched the text word for word, so every reading was dropped.",
}


class Cache(Protocol):
    async def get(self, sha: str, pipeline: str) -> dict | None: ...
    async def put(self, sha: str, pipeline: str, analysis: dict) -> None: ...


class Budget(Protocol):
    async def status(self, day: str, limit: int, client: str | None, per_visitor: int) -> dict: ...
    async def consume(self, day: str, limit: int, client: str | None, per_visitor: int) -> dict: ...
    async def refund(self, day: str, client: str | None) -> None: ...


class Reader(Protocol):
    async def read(self, params: dict) -> dict: ...


@dataclass
class Config:
    has_key: bool
    daily_budget: int = 40
    per_visitor: int = 8
    fallbacks: bool = True
    allowed_origins: tuple[str, ...] = ()


@dataclass
class Deps:
    config: Config
    cache: Cache
    budget: Budget
    reader: Reader | None
    today: str
    client: str | None = None  # client IP, used only as budget-ledger input
    log: Callable[[str], None] = lambda line: None


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)  # lower-case names
    body: bytes = b""


@dataclass
class Reply:
    status: int
    payload: dict | None
    headers: dict[str, str] = field(default_factory=dict)

    def body(self) -> str:
        return "" if self.payload is None else json.dumps(self.payload, ensure_ascii=False)


def _error(status: int, code: str, message: str) -> Reply:
    return Reply(status, {"error": code, "message": message})


def _cors(req: Request, config: Config) -> dict[str, str]:
    origin = req.headers.get("origin")
    if origin and origin in config.allowed_origins:
        return {
            "access-control-allow-origin": origin,
            "access-control-allow-methods": "GET, POST, OPTIONS",
            "access-control-allow-headers": "content-type",
            "access-control-max-age": "600",
            "vary": "Origin",
        }
    return {}


BASE_HEADERS = {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


async def handle(req: Request, deps: Deps) -> Reply:
    reply = await _route(req, deps)
    reply.headers = {**BASE_HEADERS, **_cors(req, deps.config), **reply.headers}
    return reply


async def _route(req: Request, deps: Deps) -> Reply:
    if req.method == "OPTIONS":
        return Reply(204, None)
    if req.path == "/api/status":
        if req.method not in ("GET", "HEAD"):
            return _error(405, "method_not_allowed", "Use GET.")
        return await _status(deps)
    if req.path == "/api/analyze":
        if req.method != "POST":
            return _error(405, "method_not_allowed", "Use POST with a JSON body.")
        return await _analyze(req, deps)
    return _error(404, "not_found", "No such endpoint.")


async def _budget_status(deps: Deps) -> dict | None:
    c = deps.config
    if not c.has_key:
        return None
    b = await deps.budget.status(deps.today, c.daily_budget, deps.client, c.per_visitor)
    return {**b, "resets": "00:00 UTC"}


async def _status(deps: Deps) -> Reply:
    return Reply(
        200,
        {
            "mode": "live" if deps.config.has_key else "demo",
            "model": MODEL,
            "effort": EFFORT,
            "budget": await _budget_status(deps),
            "limits": {"max_chars": MAX_CHARS, "max_ai_chars": MAX_AI_CHARS},
        },
    )


def _parse_body(req: Request) -> tuple[str | None, Reply | None]:
    ctype = req.headers.get("content-type", "")
    if "application/json" not in ctype:
        return None, _error(415, "unsupported_media_type", "Send JSON: {\"text\": \"...\"}.")
    if len(req.body) > MAX_BODY_BYTES:
        return None, _error(413, "too_large", f"The text is longer than {MAX_CHARS:,} characters.")
    try:
        data = json.loads(req.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, _error(400, "bad_json", "The request body is not valid JSON.")
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None, _error(400, "bad_request", "Expected {\"text\": \"...\"}.")
    return data["text"], None


def response_payload(text: str, sha: str, segs: list[Segment], analysis: dict | None, source: str | None,
             notice: str | None, budget: dict | None, mode: str) -> dict:
    out_analysis = None
    if analysis is not None:
        readings = with_quotes(text, analysis["readings"])
        out_analysis = {
            "source": source,
            "model": analysis.get("model"),
            "readings": readings,
            "received": analysis["received"],
            "verified": len(readings),
            "dropped": analysis["dropped"],
            "dropped_reasons": analysis["dropped_reasons"],
            "relocated": analysis.get("relocated", 0),
        }
    return {
        "text": text,
        "sha256": sha,
        "pipeline": pipeline_version(),
        "segments": [s.to_json() for s in segs],
        "analysis": out_analysis,
        "notice": {"kind": notice, "message": NOTICES[notice]} if notice else None,
        "budget": budget,
        "mode": mode,
    }


async def _analyze(req: Request, deps: Deps) -> Reply:
    raw, err = _parse_body(req)
    if err:
        return err
    if len(raw) > MAX_CHARS * 2:  # cheap guard before normalising
        return _error(413, "too_large", f"The text is longer than {MAX_CHARS:,} characters.")
    text = normalise(raw)
    if not text:
        return _error(422, "empty", "There is no text to read.")
    if len(text) > MAX_CHARS:
        return _error(413, "too_large", f"The text is longer than {MAX_CHARS:,} characters.")
    segs = segment(text)
    clause_segs = clauses(segs)
    if not clause_segs:
        return _error(422, "no_clauses", "No clauses found: the text has headings only.")

    c = deps.config
    mode = "live" if c.has_key else "demo"
    sha = text_sha256(text)
    pipeline = pipeline_version()

    def done(analysis, source, notice, budget, status=200):
        deps.log(json.dumps({
            "event": "analyze", "chars": len(text), "clauses": len(clause_segs), "source": source or "none",
            "verified": analysis["verified"] if analysis else 0, "dropped": analysis["dropped"] if analysis else 0,
            "notice": notice,
        }))
        return Reply(status, response_payload(text, sha, segs, analysis, source, notice, budget, mode))

    cached = await deps.cache.get(sha, pipeline)
    if cached is not None:
        return done(cached, "cache", None, await _budget_status(deps))

    if not c.has_key or deps.reader is None:
        return done(None, None, "demo_mode", None)
    if len(text) > MAX_AI_CHARS:
        return done(None, None, "too_long", await _budget_status(deps))
    if len(clause_segs) > MAX_CLAUSES_PER_CALL:
        return done(None, None, "too_many_clauses", await _budget_status(deps))

    ticket = await deps.budget.consume(deps.today, c.daily_budget, deps.client, c.per_visitor)
    budget = {**ticket, "resets": "00:00 UTC"}
    budget.pop("allowed", None)
    budget.pop("reason", None)
    if not ticket["allowed"]:
        return done(None, None, "visitor_limit" if ticket["reason"] == "visitor" else "budget_spent", budget)

    params = request_params(text, segs, fallbacks=c.fallbacks)
    try:
        result = await deps.reader.read(params)
        analysis = verify(text, segs, result["data"])
    except ModelUnavailable as exc:
        await deps.budget.refund(deps.today, deps.client)
        budget["used"] = max(0, budget["used"] - 1)
        budget["remaining"] = min(budget["limit"], budget["remaining"] + 1)
        budget["visitor_remaining"] = budget["visitor_remaining"] + 1
        return done(None, None, "model_busy" if exc.kind == "busy" else "model_error", budget)
    except ModelRefused:
        return done(None, None, "refused", budget)
    except ModelOutputError:
        return done(None, None, "bad_output", budget)

    analysis["model"] = result.get("model") or MODEL
    if analysis["verified"] == 0:
        return done(analysis, "model", "none_verified", budget)
    await deps.cache.put(sha, pipeline, analysis)
    return done(analysis, "model", None, budget)
