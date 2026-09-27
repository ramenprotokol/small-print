"""The on-device reader: what the browser runs, in Pyodide, with no network.

``analyze(raw)`` normalises and segments pasted text with the same code the
Worker uses, reads it with the rule set (``rules.py``, no AI) and returns a
payload in the ``/api/analyze`` response shape, with ``analysis.source`` set
to ``"rules"``, so the page renders a rule reading exactly like a model
reading. Nothing here imports the model code or touches the network: the
browser loads only this module, ``normalise``, ``segment``, ``verify`` and
``rules``.
"""

from __future__ import annotations

import json
import sys
import time

from .normalise import normalise, text_sha256
from .rules import RULES, RULES_VERSION, catalogue, read, with_finding_quotes
from .segment import SEGMENTER_VERSION, clauses, segment

# The same limits and wording as the Worker (service.py, which imports the
# model SDK and so is not loaded in the browser); a test keeps them equal.
MAX_CHARS = 60_000
NO_CLAUSES = "No clauses found: this text has headings only, so there is nothing to read."


def pipeline_version() -> str:
    return f"{SEGMENTER_VERSION}/{RULES_VERSION}"


def _error(code: str, message: str) -> dict:
    return {"error": code, "message": message}


def analyze(raw: object) -> dict:
    if not isinstance(raw, str):
        return _error("bad_request", "Expected text.")
    if len(raw) > MAX_CHARS * 2:  # cheap guard before normalising
        return _error("too_large", f"The text is longer than {MAX_CHARS:,} characters.")
    t0 = time.perf_counter()
    text = normalise(raw)
    if not text:
        return _error("empty", "There is no text to read.")
    if len(text) > MAX_CHARS:
        return _error("too_large", f"The text is longer than {MAX_CHARS:,} characters.")
    segs = segment(text)
    clause_count = len(clauses(segs))
    analysis = None
    notice = None
    if clause_count:
        found = read(text, segs)
        readings = with_finding_quotes(text, found["readings"])
        analysis = {
            "source": "rules",
            "model": None,
            "readings": readings,
            "received": found["received"],
            "verified": found["verified"],
            "dropped": found["dropped"],
            "dropped_reasons": found["dropped_reasons"],
            "relocated": found["relocated"],
            "engine": {
                "name": "rules",
                "version": RULES_VERSION,
                "rules": len(RULES),
                "python": sys.version.split()[0],
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            },
            "checked": catalogue(),
        }
    else:
        notice = {"kind": "no_clauses", "message": NO_CLAUSES}
    return {
        "text": text,
        "sha256": text_sha256(text),
        "pipeline": pipeline_version(),
        "segments": [s.to_json() for s in segs],
        "analysis": analysis,
        "notice": notice,
        "budget": None,
        "mode": "rules",
    }


def analyze_json(raw: object) -> str:
    """``analyze`` as a JSON string: one copy across the Python/JS boundary."""
    return json.dumps(analyze(raw), ensure_ascii=False)
