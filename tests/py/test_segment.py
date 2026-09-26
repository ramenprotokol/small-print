from small_print.normalise import normalise
from small_print.segment import MAX_CLAUSE, clauses, is_heading, segment

from conftest import demo_text


def texts(text):
    return [(s.kind, s.id, text[s.start : s.end]) for s in segment(text)]


def clause_texts(text):
    return [text[s.start : s.end] for s in clauses(segment(text))]


def test_sentences_become_numbered_clauses():
    t = "You may cancel at any time. We may suspend your account. Fees are non-refundable."
    assert texts(t) == [
        ("clause", 1, "You may cancel at any time."),
        ("clause", 2, "We may suspend your account."),
        ("clause", 3, "Fees are non-refundable."),
    ]


def test_abbreviations_and_section_numbers_do_not_split():
    t = (
        "Nimbus Locker Ltd. (a fictional company) runs the Service, e.g. storage and sync. "
        "We are based in the U.S. and follow Section 4.2 of this document. "
        "See No. 7 for more."
    )
    assert clause_texts(t) == [
        "Nimbus Locker Ltd. (a fictional company) runs the Service, e.g. storage and sync.",
        "We are based in the U.S. and follow Section 4.2 of this document.",
        "See No. 7 for more.",
    ]


def test_leading_list_number_stays_with_its_sentence():
    assert clause_texts("4. You may not resell storage.") == ["4. You may not resell storage."]
    assert clause_texts("4.2 You may not resell storage.") == ["4.2 You may not resell storage."]


def test_inline_list_splits_at_each_item_but_keeps_the_lead_in():
    t = "You agree not to: (a) copy the Service; (b) resell storage; and (c) upload malware."
    assert clause_texts(t) == [
        "You agree not to: (a) copy the Service;",
        "(b) resell storage;",
        "and (c) upload malware.",
    ]


def test_sentence_end_inside_closing_quote():
    t = 'We call this the "Service." You agree to use it lawfully.'
    assert clause_texts(t) == ['We call this the "Service."', "You agree to use it lawfully."]


def test_short_fragments_merge_into_a_neighbour():
    t = "Yes. You agree to these Terms by signing up."
    assert clause_texts(t) == ["Yes. You agree to these Terms by signing up."]
    t = "You agree to these Terms by signing up. Ok."
    assert clause_texts(t) == ["You agree to these Terms by signing up. Ok."]


def test_a_clause_never_crosses_a_line_break():
    t = "First line without a full stop\nSecond Line starts with a capital."
    for s in clauses(segment(t)):
        assert "\n" not in t[s.start : s.end]


def test_very_long_sentence_splits_at_semicolons():
    part = "we may process your data for purposes that are described at great length here"
    t = "; ".join([part] * 20) + "."
    pieces = clause_texts(t)
    assert len(pieces) > 1
    assert all(len(p) <= MAX_CLAUSE for p in pieces)
    assert " ".join(pieces) == t  # nothing lost, nothing added


def test_headings_are_detected_and_unnumbered():
    t = "1. Acceptance of Terms\nBy using the app you agree.\nLIMITATION OF LIABILITY\nWe are not liable."
    assert texts(t) == [
        ("heading", None, "1. Acceptance of Terms"),
        ("clause", 1, "By using the app you agree."),
        ("heading", None, "LIMITATION OF LIABILITY"),
        ("clause", 2, "We are not liable."),
    ]


def test_is_heading_rules():
    assert is_heading("3. Payments and Refunds")
    assert is_heading("Your Content")
    assert is_heading("## Privacy")
    assert is_heading("TERMS OF SERVICE")
    assert not is_heading("You may cancel at any time.")
    assert not is_heading("You agree not to:")
    assert not is_heading("Scrape or copy the Service")  # a list item, not title case
    assert not is_heading("THE SERVICE IS PROVIDED AS IS, WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, OR ANY OTHER KIND")
    assert not is_heading("")


def test_offsets_are_exact_and_in_order():
    t = normalise(demo_text("nimbus-locker"))
    segs = segment(t)
    last_end = 0
    for s in segs:
        assert s.start >= last_end
        assert t[s.start : s.end] == t[s.start : s.end].strip()
        last_end = s.end
    ids = [s.id for s in clauses(segs)]
    assert ids == list(range(1, len(ids) + 1))


def test_every_non_space_character_is_covered():
    t = normalise(demo_text("brothbike"))
    covered = [False] * len(t)
    for s in segment(t):
        for i in range(s.start, s.end):
            covered[i] = True
    missing = [t[i] for i in range(len(t)) if not covered[i] and not t[i].isspace()]
    assert missing == []


def test_offsets_are_code_points_even_with_astral_characters():
    t = "Emoji \U0001F600 appear here. The next clause starts after them."
    segs = clauses(segment(t))
    assert t[segs[1].start : segs[1].end] == "The next clause starts after them."
    assert segs[1].start == t.index("The next")


def test_demo_documents_segment_to_expected_sizes():
    counts = {slug: len(clauses(segment(normalise(demo_text(slug))))) for slug in ("nimbus-locker", "pacewren", "brothbike")}
    assert counts == {"nimbus-locker": 46, "pacewren": 42, "brothbike": 40}


def test_empty_text_has_no_segments():
    assert segment("") == []
    assert segment("\n\n") == []
