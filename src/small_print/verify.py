"""Quote verification: keep only model readings whose quote is verbatim.

The model returns, per clause, a label, a one-line reading and a quote. A
reading is kept only if its quote is an exact substring of the normalised
text (a plain ``str.find``: no fuzzy matching, no whitespace or case
folding). Everything else is dropped and counted, with a reason.

Placement rules for a quote that is found:

1. If it occurs starting inside the clause the model named, it is placed
   there.
2. Otherwise, if it occurs exactly once in the whole text, it is moved to the
   clause where that occurrence starts ("relocated"): the quote, not the
   model's clause number, is the evidence.
3. Otherwise (several occurrences, none in the named clause) it is dropped
   as ambiguous.

Each clause keeps at most one reading; later ones are dropped as duplicates.
"""

from __future__ import annotations

from bisect import bisect_right

from .segment import Segment

FAVOURS = ("you", "them", "neutral", "unclear")
CONFIDENCE = ("high", "medium", "low")
MIN_QUOTE = 4
MAX_READING = 280

DROP_REASONS = {
    "malformed": "the entry was missing a field or had an unknown label",
    "not_found": "the quote is not in the text verbatim",
    "ambiguous": "the quote appears several times, none in the named clause",
    "duplicate": "another reading already covers that clause",
    "too_short": "the quote is too short to be evidence",
}


class ModelOutputError(ValueError):
    """The model output as a whole is unusable (not the expected JSON shape)."""


def _clip_reading(reading: str) -> str:
    reading = " ".join(reading.split())
    if len(reading) <= MAX_READING:
        return reading
    cut = reading.rfind(" ", 0, MAX_READING - 1)
    return reading[: cut if cut > 0 else MAX_READING - 1].rstrip(",;:") + "…"


def _valid_item(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    cid = item.get("id")
    if not isinstance(cid, int) or isinstance(cid, bool):
        return False
    if item.get("favours") not in FAVOURS or item.get("confidence") not in CONFIDENCE:
        return False
    quote, reading = item.get("quote"), item.get("reading")
    if not isinstance(quote, str) or not isinstance(reading, str):
        return False
    return bool(reading.strip())


def verify(text: str, segments: list[Segment], output: object) -> dict:
    """Check model output against the text.

    ``output`` is the parsed model JSON: ``{"clauses": [{id, quote, favours,
    reading, confidence}, ...]}``. Returns the verified readings (with
    code-point offsets of each quote) and the drop accounting.
    """
    if not isinstance(output, dict) or not isinstance(output.get("clauses"), list):
        raise ModelOutputError("expected an object with a 'clauses' list")

    clause_segs = [s for s in segments if s.kind == "clause"]
    by_id = {s.id: s for s in clause_segs}
    starts = [s.start for s in clause_segs]

    def clause_at(pos: int) -> Segment | None:
        i = bisect_right(starts, pos) - 1
        if i >= 0 and clause_segs[i].start <= pos < clause_segs[i].end:
            return clause_segs[i]
        return None

    readings: dict[int, dict] = {}
    dropped: list[dict] = []
    relocated = 0
    items = output["clauses"]

    for item in items:
        claimed = item.get("id") if isinstance(item, dict) else None
        if not _valid_item(item):
            dropped.append({"id": claimed if isinstance(claimed, int) else None, "reason": "malformed"})
            continue
        quote = item["quote"].strip()
        if len(quote) < MIN_QUOTE:
            dropped.append({"id": claimed, "reason": "too_short"})
            continue

        seg = by_id.get(claimed)
        at = -1
        if seg is not None:
            found = text.find(quote, seg.start)
            if found != -1 and found < seg.end:
                at = found
        target = seg
        if at == -1:
            first = text.find(quote)
            if first == -1:
                dropped.append({"id": claimed, "reason": "not_found"})
                continue
            if text.find(quote, first + 1) != -1:
                dropped.append({"id": claimed, "reason": "ambiguous"})
                continue
            target = clause_at(first)
            if target is None:  # found, but inside a heading or a gap
                dropped.append({"id": claimed, "reason": "not_found"})
                continue
            at = first
            relocated += 1

        if target.id in readings:
            dropped.append({"id": claimed, "reason": "duplicate"})
            continue
        readings[target.id] = {
            "id": target.id,
            "favours": item["favours"],
            "confidence": item["confidence"],
            "reading": _clip_reading(item["reading"]),
            "quote_start": at,
            "quote_end": at + len(quote),
        }

    reasons: dict[str, int] = {}
    for d in dropped:
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    return {
        "readings": [readings[k] for k in sorted(readings)],
        "received": len(items),
        "verified": len(readings),
        "dropped": len(dropped),
        "dropped_reasons": reasons,
        "relocated": relocated,
    }


def with_quotes(text: str, readings: list[dict]) -> list[dict]:
    """Attach the quote text (sliced from ``text``) to each reading.

    Cached analyses store offsets only; the quote is always rebuilt from the
    text the visitor sent, so a stored analysis can never show words that are
    not in front of the reader.
    """
    out = []
    n = len(text)
    for r in readings:
        s, e = r["quote_start"], r["quote_end"]
        if not (0 <= s < e <= n):
            continue
        out.append({**r, "quote": text[s:e]})
    return out
