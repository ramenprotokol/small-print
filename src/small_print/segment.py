"""Clause segmentation: split normalised terms-of-service text into clauses.

Pure Python, no dependencies. The output covers every non-blank line: each
line is either one heading or one or more clauses. A clause never crosses a
line break, which keeps each clause a verbatim, single-line substring that a
model can quote exactly.

Within a line, clauses are sentences, with legal-text care:

* abbreviations ("e.g.", "Inc.", "U.S.", "No.") and section numbers
  ("Section 4.2") do not end a sentence;
* an inline list ("...; (b) ...; and (c) ...") splits at each item;
* a short inline heading ("1. Acceptance. By using ...", "Fees. You pay
  ...") joins the sentence after it instead of becoming its own clause;
* fragments shorter than ``MIN_CLAUSE`` characters merge into a neighbour,
  and sentences longer than ``MAX_CLAUSE`` split at semicolons if possible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Bump when segmentation output changes: it is part of the cache key, so old
# cached offsets are never applied to new clause boundaries.
SEGMENTER_VERSION = "seg-2"

MIN_CLAUSE = 12
MAX_CLAUSE = 900
MIN_SPLIT_PIECE = 200
MAX_TITLE_WORDS = 3


@dataclass(frozen=True)
class Segment:
    kind: str  # "clause" or "heading"
    start: int  # code-point offset into the normalised text (inclusive)
    end: int  # exclusive
    id: int | None = None  # clause number, 1-based; None for headings

    def to_json(self) -> dict:
        out = {"kind": self.kind, "start": self.start, "end": self.end}
        if self.id is not None:
            out["id"] = self.id
        return out


_ABBREVIATIONS = frozenset(
    """
    e.g i.e etc inc ltd llc llp plc co corp no nos sec secs art arts para paras
    cl sch pp p vs v cf approx dept est fig figs mr mrs ms dr st jr sr u.s u.k
    e.u a.m p.m jan feb mar apr jun jul aug sep sept oct nov dec pty ref
    """.split()
)

# End of sentence: terminal punctuation, optional closing quotes/brackets,
# then whitespace and something that can start a sentence.
_SENTENCE_END = re.compile(
    r"[.!?][\"'”’)\]]*(?=\s+[\"'“‘(\[]?[A-Z0-9À-Þ])"
)
# An inline list item after a semicolon: "; (b) ", "; and (c) ", "; or (ii) ".
# (Not after a colon: "You agree not to: (a) ..." keeps its lead-in.)
_LIST_ITEM = re.compile(
    r";(?=\s+(?:(?:and|or)\s+)?\((?:[a-z]|[ivx]{1,4}|\d{1,2})\)\s)"
)
_SEMICOLON = re.compile(r";(?=\s)")
_LIST_NUMBER = re.compile(r"^\(?(?:\d+(?:\.\d+)*|[ivxlc]+|[a-z])[.)]?$", re.IGNORECASE)
_NUMBERING = re.compile(
    r"^(?:#+\s*)?(?:(?:section|article|part|clause|schedule)\s+)?"
    r"(?:\d+(?:\.\d+)*|[IVXLC]+|[A-Z])[.)]?\s+",
    re.IGNORECASE,
)
_SMALL_WORDS = frozenset(
    "a an and as at by for from in into of on or the to with your our its".split()
)


def is_heading(line: str) -> bool:
    """Whether a single stripped line reads as a section heading."""
    line = line.strip()
    if not line or len(line) > 100:
        return False
    if line.startswith("#"):
        return True
    if line[-1] in ".;,!?":
        return False
    letters = [c for c in line if c.isalpha()]
    if not letters:
        return False
    words = line.split()
    upper = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper >= 0.9 and len(words) <= 12 and len(letters) >= 3:
        return True
    body = _NUMBERING.sub("", line, count=1) if _NUMBERING.match(line) else line
    body_words = [w.strip("()[]\"'“”,&-") for w in body.split()]
    body_words = [w for w in body_words if w]
    if not body_words or len(body_words) > 9 or line.endswith(":"):
        return False
    if not body_words[0][0].isupper():
        return False
    # Title case: every word of four or more letters starts with a capital.
    for w in body_words[1:]:
        if w.lower() in _SMALL_WORDS or len(w) < 4:
            continue
        if not w[0].isupper():
            return False
    return True


def _is_title_fragment(fragment: str) -> bool:
    """Whether a sentence-like piece is really a short inline heading:
    "1. Acceptance.", "Governing Law.", "FEES." (at most three words after
    any numbering, ending in a full stop, in title case or capitals)."""
    if not fragment.endswith("."):
        return False
    body = _NUMBERING.sub("", fragment, count=1)
    words = [w.strip("()[]\"'“”,&-.") for w in body.split()]
    words = [w for w in words if w]
    if not 1 <= len(words) <= MAX_TITLE_WORDS or not words[0][0].isupper():
        return False
    for w in words[1:]:
        if w.lower() in _SMALL_WORDS or len(w) < 4:
            continue
        if not w[0].isupper():
            return False
    return True


def _token_before(line: str, dot: int) -> str:
    """The run of non-space characters that ends at ``dot``. Scanning back
    costs the token's length; searching the whole line up to ``dot`` made a
    long single-line paste quadratic in its number of full stops."""
    start = dot
    while start > 0 and not line[start - 1].isspace():
        start -= 1
    return line[start : dot + 1]


def _is_abbreviation(line: str, dot: int, line_start_token: bool) -> bool:
    raw = _token_before(line, dot)
    if not raw:
        return False
    token = raw.lstrip("(\"'“‘[").rstrip(".").lower()
    if token in _ABBREVIATIONS:
        return True
    if re.fullmatch(r"[a-z]", token):  # an initial: "J. Smith"
        return True
    if re.fullmatch(r"(?:[a-z]\.)+[a-z]", token):  # "u.s.a"
        return True
    # A leading list number: "4. You may..." is one clause, not "4." + rest.
    if line_start_token and _LIST_NUMBER.match(raw):
        return True
    return False


def _cut_points(line: str) -> list[int]:
    """Positions (relative to ``line``) where one clause ends."""
    cuts: set[int] = set()
    first_token_end = len(line.split(None, 1)[0]) if line.split() else 0
    for m in _SENTENCE_END.finditer(line):
        punct = m.start()
        if line[punct] == "." and _is_abbreviation(
            line, punct, line_start_token=punct < first_token_end
        ):
            continue
        cuts.add(m.end())
    for m in _LIST_ITEM.finditer(line):
        cuts.add(m.end())
    return sorted(cuts)


def _trimmed(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    pieces = []
    while end - start > MAX_CLAUSE:
        window = text[start + MIN_SPLIT_PIECE : start + MAX_CLAUSE]
        cuts = [m.end() for m in _SEMICOLON.finditer(window)]
        cuts = [c for c in cuts if end - (start + MIN_SPLIT_PIECE + c) >= MIN_SPLIT_PIECE]
        if not cuts:
            break
        cut = start + MIN_SPLIT_PIECE + cuts[-1]
        pieces.append(_trimmed(text, start, cut))
        start = _trimmed(text, cut, end)[0]
    pieces.append((start, end))
    return pieces


def _clauses_in_line(text: str, start: int, end: int) -> list[tuple[int, int]]:
    line = text[start:end]
    bounds = [0, *_cut_points(line), len(line)]
    raw = []
    for a, b in zip(bounds, bounds[1:]):
        s, e = _trimmed(text, start + a, start + b)
        if e > s:
            raw.append((s, e))

    # An inline heading carries forward into the sentence it introduces.
    titled: list[tuple[int, int]] = []
    carry: int | None = None
    for i, (s, e) in enumerate(raw):
        start = s if carry is None else carry
        carry = None
        if i + 1 < len(raw) and _is_title_fragment(text[s:e]):
            carry = start
            continue
        titled.append((start, e))

    merged: list[tuple[int, int]] = []
    pending: int | None = None  # start of a too-short run waiting for more
    for s, e in titled:
        if pending is not None:
            s = pending
            pending = None
        if e - s < MIN_CLAUSE:
            pending = s
            continue
        merged.append((s, e))
    if pending is not None:
        tail_end = titled[-1][1]
        if merged:
            merged[-1] = (merged[-1][0], tail_end)
        else:
            merged.append((pending, tail_end))

    out: list[tuple[int, int]] = []
    for s, e in merged:
        out.extend(_split_long(text, s, e))
    return out


def segment(text: str) -> list[Segment]:
    """Split normalised text into headings and numbered clauses, in order."""
    segments: list[Segment] = []
    next_id = 1
    offset = 0
    for raw_line in text.split("\n"):
        line_start, line_end = _trimmed(text, offset, offset + len(raw_line))
        offset += len(raw_line) + 1
        if line_end <= line_start:
            continue
        if is_heading(text[line_start:line_end]):
            segments.append(Segment("heading", line_start, line_end))
            continue
        for s, e in _clauses_in_line(text, line_start, line_end):
            segments.append(Segment("clause", s, e, next_id))
            next_id += 1
    return segments


def clauses(segments: list[Segment]) -> list[Segment]:
    return [s for s in segments if s.kind == "clause"]
