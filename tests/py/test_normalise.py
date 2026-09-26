from small_print.normalise import normalise, text_sha256


def test_line_endings_become_newlines():
    assert normalise("One.\r\nTwo.\rThree.") == "One.\nTwo.\nThree."


def test_odd_spaces_collapse_to_one_ascii_space():
    nbsp, thin, ideographic = chr(0xA0), chr(0x2009), chr(0x3000)
    assert normalise(f"a{nbsp}b{thin}c{ideographic}d\t\te   f") == "a b c d e f"


def test_invisible_characters_are_removed():
    zwsp, bom, shy = chr(0x200B), chr(0xFEFF), chr(0xAD)
    assert normalise(f"{bom}Ter{zwsp}ms of ser{shy}vice.") == "Terms of service."


def test_control_characters_dropped_and_form_feed_is_a_paragraph_break():
    assert normalise("Page one.\x0cPage two.\x07") == "Page one.\n\nPage two."


def test_unicode_is_composed_nfc():
    decomposed = "Cafe" + chr(0x301)  # e + combining acute
    assert normalise(decomposed) == "Caf" + chr(0xE9)


def test_runs_of_blank_lines_collapse_to_one_blank_line():
    assert normalise("A.\n\n\n\n\nB.") == "A.\n\nB."


def test_spaces_around_newlines_are_trimmed():
    assert normalise("A.   \n   B.") == "A.\nB."


def test_hard_wrapped_sentence_is_rejoined():
    assert normalise("You grant us a licence to host\nand copy your files.") == (
        "You grant us a licence to host and copy your files."
    )


def test_line_ending_in_punctuation_or_next_line_capitalised_is_kept():
    assert normalise("We may do this:\nstop.") == "We may do this:\nstop."
    assert normalise("End of sentence.\nnext line") == "End of sentence.\nnext line"
    assert normalise("Heading\nThe body starts here.") == "Heading\nThe body starts here."


def test_outer_whitespace_is_stripped_and_empty_stays_empty():
    assert normalise("  \n\n Terms. \n ") == "Terms."
    assert normalise(" \t\r\n ") == ""


def test_normalise_is_idempotent():
    messy = "  A" + chr(0xA0) + "b.\r\n\r\n\r\nC  d\nwrapped.\x0c" + chr(0x200B)
    once = normalise(messy)
    assert normalise(once) == once


def test_cache_key_is_sha256_of_normalised_text_and_ignores_noise():
    a = normalise("Terms apply.\r\n\r\n\r\nYou agree.")
    b = normalise("Terms  apply.\n\nYou agree.  ")
    assert a == b
    assert text_sha256(a) == text_sha256(b)
    assert len(text_sha256(a)) == 64
    # Known vector: sha256("abc")
    assert text_sha256("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_cache_key_changes_with_meaningful_text():
    assert text_sha256(normalise("You may cancel.")) != text_sha256(normalise("You may not cancel."))
