import pytest

from small_print.normalise import normalise
from small_print.segment import segment
from small_print.verify import MAX_READING, ModelOutputError, verify, with_quotes

TEXT = normalise(
    "1. Your Files\n"
    "You keep ownership of everything you upload. "
    "We may use your files to train our models. "
    "We may use your files to train our models.\n"
    "2. Ending\n"
    "You can close your account at any time. "
    "We may terminate your account without notice."
)
SEGS = segment(TEXT)
# Clauses: 1 ownership, 2 train (first), 3 train (repeat), 4 close, 5 terminate


def item(cid, quote, favours="them", confidence="high", reading="A reading."):
    return {"id": cid, "quote": quote, "favours": favours, "reading": reading, "confidence": confidence}


def test_exact_quote_in_its_clause_is_kept_with_offsets():
    r = verify(TEXT, SEGS, {"clauses": [item(1, "You keep ownership", "you")]})
    assert r["verified"] == 1 and r["dropped"] == 0
    got = r["readings"][0]
    assert TEXT[got["quote_start"] : got["quote_end"]] == "You keep ownership"
    assert got["id"] == 1 and got["favours"] == "you"


def test_quote_not_in_text_is_dropped_and_counted():
    r = verify(TEXT, SEGS, {"clauses": [
        item(1, "You keep ownership"),
        item(4, "You may close your account"),  # paraphrase: "can" became "may"
        item(5, "we may terminate your account"),  # case changed
        item(5, "We may terminate your account … notice"),  # ellipsis
    ]})
    assert r["received"] == 4
    assert r["verified"] == 1
    assert r["dropped"] == 3
    assert r["dropped_reasons"] == {"not_found": 3}


def test_whitespace_differences_are_not_forgiven():
    r = verify(TEXT, SEGS, {"clauses": [item(4, "You can  close your account")]})
    assert r["dropped_reasons"] == {"not_found": 1}


def test_outer_whitespace_of_a_quote_is_trimmed():
    r = verify(TEXT, SEGS, {"clauses": [item(4, "  You can close your account ")]})
    got = r["readings"][0]
    assert TEXT[got["quote_start"] : got["quote_end"]] == "You can close your account"


def test_quote_found_uniquely_elsewhere_is_relocated_to_that_clause():
    r = verify(TEXT, SEGS, {"clauses": [item(1, "We may terminate your account")]})
    assert r["relocated"] == 1
    assert r["readings"][0]["id"] == 5


def test_repeated_quote_prefers_the_named_clause():
    r = verify(TEXT, SEGS, {"clauses": [item(3, "We may use your files to train our models")]})
    got = r["readings"][0]
    assert got["id"] == 3 and r["relocated"] == 0
    assert got["quote_start"] == SEGS_BY_ID[3].start


def test_repeated_quote_outside_the_named_clause_is_ambiguous():
    r = verify(TEXT, SEGS, {"clauses": [item(1, "We may use your files to train our models")]})
    assert r["dropped_reasons"] == {"ambiguous": 1}


def test_unknown_clause_id_with_unique_quote_is_relocated():
    r = verify(TEXT, SEGS, {"clauses": [item(99, "You can close your account")]})
    assert r["readings"][0]["id"] == 4 and r["relocated"] == 1


def test_quote_matching_only_a_heading_is_dropped():
    r = verify(TEXT, SEGS, {"clauses": [item(1, "1. Your Files")]})
    assert r["dropped_reasons"] == {"not_found": 1}


def test_second_reading_for_the_same_clause_is_a_duplicate():
    r = verify(TEXT, SEGS, {"clauses": [
        item(4, "You can close your account"),
        item(4, "at any time", "neutral"),
    ]})
    assert r["verified"] == 1
    assert r["dropped_reasons"] == {"duplicate": 1}


@pytest.mark.parametrize("bad", [
    {"id": 1, "quote": "You keep ownership", "favours": "both", "reading": "x", "confidence": "high"},
    {"id": 1, "quote": "You keep ownership", "favours": "you", "reading": "x", "confidence": "certain"},
    {"id": "1", "quote": "You keep ownership", "favours": "you", "reading": "x", "confidence": "high"},
    {"id": True, "quote": "You keep ownership", "favours": "you", "reading": "x", "confidence": "high"},
    {"id": 1, "quote": None, "favours": "you", "reading": "x", "confidence": "high"},
    {"id": 1, "quote": "You keep ownership", "favours": "you", "reading": "   ", "confidence": "high"},
    {"id": 1, "favours": "you", "reading": "x", "confidence": "high"},
    "not an object",
])
def test_malformed_entries_are_dropped(bad):
    r = verify(TEXT, SEGS, {"clauses": [bad]})
    assert r["verified"] == 0
    assert r["dropped_reasons"] == {"malformed": 1}


def test_too_short_quote_is_not_evidence():
    r = verify(TEXT, SEGS, {"clauses": [item(1, "You")]})
    assert r["dropped_reasons"] == {"too_short": 1}


@pytest.mark.parametrize("output", [None, [], {"clause": []}, {"clauses": "none"}, "text"])
def test_output_of_the_wrong_shape_is_an_error(output):
    with pytest.raises(ModelOutputError):
        verify(TEXT, SEGS, output)


def test_long_reading_is_clipped_on_a_word_boundary():
    long = "word " * 200
    r = verify(TEXT, SEGS, {"clauses": [item(1, "You keep ownership", reading=long)]})
    reading = r["readings"][0]["reading"]
    assert len(reading) <= MAX_READING
    assert reading.endswith("…")


def test_readings_are_sorted_by_clause():
    r = verify(TEXT, SEGS, {"clauses": [item(5, "We may terminate"), item(1, "You keep ownership")]})
    assert [x["id"] for x in r["readings"]] == [1, 5]


def test_with_quotes_rebuilds_quote_text_and_skips_bad_offsets():
    r = verify(TEXT, SEGS, {"clauses": [item(1, "You keep ownership")]})
    good = with_quotes(TEXT, r["readings"])
    assert good[0]["quote"] == "You keep ownership"
    bad = [{**r["readings"][0], "quote_end": len(TEXT) + 5}]
    assert with_quotes(TEXT, bad) == []


def test_verification_uses_code_point_offsets():
    text = "Smile \U0001F600 please. You keep ownership of your files."
    segs = segment(text)
    r = verify(text, segs, {"clauses": [item(2, "You keep ownership")]})
    got = r["readings"][0]
    assert got["quote_start"] == text.index("You keep")


SEGS_BY_ID = {s.id: s for s in SEGS if s.id is not None}
