import json
import re
import time

import pytest

from small_print.normalise import normalise
from small_print.segment import clauses, segment
from small_print.service import MAX_CHARS
from small_print.verify import verify

from conftest import DEMO, demo_analysis, demo_text

SLUGS = [d["slug"] for d in json.loads((DEMO / "index.json").read_text())]


@pytest.mark.parametrize("slug", SLUGS)
def test_hand_written_analysis_verifies_completely(slug):
    text = normalise(demo_text(slug))
    segs = segment(text)
    r = verify(text, segs, demo_analysis(slug))
    assert r["dropped"] == 0 and r["relocated"] == 0
    assert {x["id"] for x in r["readings"]} == {s.id for s in clauses(segs)}


@pytest.mark.parametrize("slug", SLUGS)
def test_demo_documents_are_marked_fictional_and_use_reserved_domains(slug):
    text = demo_text(slug)
    assert "This is a fictional document written for the small-print demo" in text
    emails = re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)", text)
    assert emails, "each demo has contact addresses"
    for domain in emails:
        assert domain.endswith(".example"), domain  # RFC 2606 reserved name
    assert not re.search(r"\b[\w-]+\.(com|net|org|io|app|co|ai)\b", text)


@pytest.mark.parametrize("slug", SLUGS)
def test_demo_analysis_uses_every_label_shape_the_model_can_return(slug):
    items = demo_analysis(slug)["clauses"]
    assert set(items[0]) == {"id", "quote", "favours", "reading", "confidence"}
    assert all(len(i["reading"].split()) <= 25 for i in items)


def test_a_maximum_size_document_segments_quickly_on_cpython():
    """Not a performance claim: a guard against accidental quadratic work.
    The Worker runs the same code under Pyodide, which is slower."""
    base = normalise(demo_text("nimbus-locker"))
    text = ""
    while len(text) + len(base) + 2 <= MAX_CHARS:
        text += base + "\n\n"
    text = text.strip()
    t0 = time.perf_counter()
    segs = segment(text)
    elapsed = time.perf_counter() - t0
    assert len(clauses(segs)) > 500
    assert elapsed < 1.0
