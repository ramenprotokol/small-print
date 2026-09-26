"""Text normalisation and the cache key.

Every offset small-print reports is a Unicode code-point index into the
*normalised* text, and the API returns that text so the page always shows
exactly the characters the offsets point at. Normalisation only removes
noise that would make verbatim quoting fragile (odd spaces, zero-width
characters, CRLF line ends) and re-joins lines that were hard-wrapped
mid-sentence, as text copied out of a PDF often is.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Character classes are built from code points so that no invisible
# character has to appear literally in this source file.
def _cls(*cps: int) -> str:
    return "".join(chr(c) for c in cps)


# Control characters other than newline: dropped. Form feed (page break in
# PDF text) becomes a paragraph break first.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
# Zero-width characters, the BOM and soft hyphens: invisible, and a model
# will never reproduce them inside a quote.
_INVISIBLE = re.compile("[" + _cls(0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x00AD) + "]")
# Every horizontal space-like character (tab, no-break space, the U+2000
# block, narrow and ideographic spaces), collapsed to one ASCII space.
_SPACES = re.compile(
    "[ \t" + _cls(0x00A0, 0x1680, 0x202F, 0x205F, 0x3000) + chr(0x2000) + "-" + chr(0x200A) + "]+"
)
_EDGE_SPACES = re.compile(r" *\n *")
_MANY_BREAKS = re.compile(r"\n{3,}")
# A line break inside a sentence: the line does not end in terminal or
# list-introducing punctuation, and the next line starts with a lowercase
# letter. Conservative on purpose: headings and new paragraphs start with a
# capital, and a line that opens with a lowercase list marker ("a. ", "b) ",
# "iv. ") is a list item or a sub-heading, never the rest of a sentence.
_SOFT_WRAP = re.compile(
    r"(?<=[^\s.!?:;])\n(?=[a-z])(?![a-z]{1,4}[.)]\s)(?![ivx]+[.)]\s)"
)
_LINE_SEP = chr(0x2028)
_PARA_SEP = chr(0x2029)


def normalise(text: str) -> str:
    """Return the canonical form of ``text`` that all offsets refer to."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x0c", "\n\n").replace(_LINE_SEP, "\n").replace(_PARA_SEP, "\n\n")
    text = unicodedata.normalize("NFC", text)
    text = _CONTROL.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _EDGE_SPACES.sub("\n", text)
    text = _SOFT_WRAP.sub(" ", text)
    text = _MANY_BREAKS.sub("\n\n", text)
    return text.strip()


def text_sha256(normalised: str) -> str:
    """Cache key: SHA-256 (hex) of the normalised text, UTF-8 encoded."""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()
