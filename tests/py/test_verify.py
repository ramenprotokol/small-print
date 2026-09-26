import pytest

from small_print.normalise import normalise
from small_print.segment import segment
from small_print.verify import MAX_READING, MIN_QUOTE_CHARS, ModelOutputError, verify, with_quotes

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
    for quote in ("You", "your", "your files", "in writing"):
        r = verify(TEXT, SEGS, {"clauses": [item(1, quote)]})
        assert r["dropped_reasons"] == {"too_short": 1}, quote


def test_quote_of_three_words_or_fifteen_characters_is_enough():
    r = verify(TEXT, SEGS, {"clauses": [item(4, "at any time"), item(1, "ownership of everything"[:MIN_QUOTE_CHARS])]})
    assert r["verified"] == 2  # three short words; fifteen characters in two words


def test_a_short_clause_quoted_whole_is_evidence():
    text = normalise("Fees apply.\nWe may change the fees whenever we like.")
    segs = segment(text)
    r = verify(text, segs, {"clauses": [item(1, "Fees apply.")]})
    assert r["verified"] == 1


# -- quotes must stay inside their clause -----------------------------------
CROSS = normalise(
    "TERMINATION\n"
    "We may terminate your account at any time. We will refund any unused fees within 30 days.\n"
    "PAYMENT\n"
    "You must pay all fees up front."
)
CROSS_SEGS = segment(CROSS)
# Clauses: 1 terminate, 2 refund, 3 pay (headings TERMINATION and PAYMENT)


def cross(items):
    return verify(CROSS, CROSS_SEGS, {"clauses": items})


def test_quote_running_into_the_next_clause_is_dropped():
    r = cross([item(1, "any time. We will refund any unused fees", "you")])
    assert r["verified"] == 0
    assert r["dropped_reasons"] == {"crosses_clause": 1}


def test_quote_crossing_a_line_break_and_heading_is_dropped():
    r = cross([item(2, "within 30 days.\nPAYMENT\nYou must pay")])
    assert r["dropped_reasons"] == {"crosses_clause": 1}
    r = cross([item(2, "unused fees within 30 days.\nPAYMENT")])
    assert r["dropped_reasons"] == {"crosses_clause": 1}


def test_relocation_does_not_accept_a_quote_that_crosses_out_of_its_clause():
    # Named clause 3; the words start in clause 1 and run into clause 2.
    r = cross([item(3, "at any time. We will refund")])
    assert r["verified"] == 0 and r["relocated"] == 0
    assert r["dropped_reasons"] == {"crosses_clause": 1}


def test_quote_ending_exactly_at_the_clause_end_is_kept():
    whole = "We will refund any unused fees within 30 days."
    r = cross([item(2, whole, "you"), item(1, "your account at any time.")])
    assert r["verified"] == 2 and r["dropped"] == 0
    by_id = {x["id"]: x for x in r["readings"]}
    seg2 = next(s for s in CROSS_SEGS if s.id == 2)
    assert by_id[2]["quote_end"] == seg2.end
    seg1 = next(s for s in CROSS_SEGS if s.id == 1)
    assert by_id[1]["quote_end"] == seg1.end


def test_exact_boundary_quote_is_also_kept_when_relocated():
    r = cross([item(3, "We will refund any unused fees within 30 days.")])
    assert r["verified"] == 1 and r["relocated"] == 1
    assert r["readings"][0]["id"] == 2


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
