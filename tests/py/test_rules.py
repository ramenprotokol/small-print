"""The rule-based reader: true positives, near misses, and quote checks.

Every sentence here is written for these tests (no real company's terms).
"""

import json
import time
from pathlib import Path

import pytest

from small_print import local, service
from small_print.normalise import normalise
from small_print.rules import MAX_FINDINGS, RULES, RULES_BY_ID, catalogue, company_names, find, read
from small_print.segment import clauses, segment
from small_print.verify import FAVOURS, MIN_QUOTE_CHARS, MIN_QUOTE_WORDS

from conftest import demo_text

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "quillfeather-terms.txt"
PREAMBLE = 'Quillfeather Labs LLC ("Quillfeather", "we", "us") provides the App.\n'


def fired(sentence: str, preamble: str = PREAMBLE) -> set[str]:
    """Rule ids that fire on the last clause of ``preamble + sentence``."""
    text = normalise(preamble + sentence)
    segs = segment(text)
    last = clauses(segs)[-1]
    assert text[last.start : last.end] == sentence, "the sentence must be one clause"
    return {f.rule.id for f in find(text, segs).get(last.id, [])}


POSITIVES = [
    # forced arbitration
    ("All disputes between you and Quillfeather shall be settled exclusively through confidential arbitration.", "arbitration"),
    ("You and we agree to arbitrate all claims.", "arbitration"),
    ("Any claim will be decided by final and binding arbitration in Examplia City.", "arbitration"),
    # class-action waiver
    ("You agree to bring claims only on an individual basis, and not as a plaintiff or class member in any class proceeding.", "class_waiver"),
    ("Class actions and collective arbitrations are not permitted.", "class_waiver"),
    ("You may not bring or join a class action against us.", "class_waiver"),
    # jury trial
    ("Each party waives the right to a trial by jury.", "jury_waiver"),
    ("Trial by jury is hereby waived.", "jury_waiver"),
    # unilateral changes
    ("Quillfeather reserves the right to amend this Agreement at any time.", "terms_changes"),
    ("These Terms may be revised by us without prior notice.", "terms_changes"),
    ("By continuing to use the App after the changes take effect, you agree to the revised terms.", "continued_use"),
    ("Your continued access to the Service will be deemed acceptance of the updated Agreement.", "continued_use"),
    # auto-renewal
    ("Memberships auto-renew each year.", "auto_renewal"),
    ("Your subscription will be automatically renewed for successive one-month terms.", "auto_renewal"),
    ("Billing continues every month until you cancel.", "auto_renewal"),
    # termination / suspension at will
    ("Quillfeather may deactivate your profile for any reason, without prior notice.", "termination"),
    ("We reserve the right to terminate accounts in our sole discretion.", "termination"),
    ("Inactive accounts may be deleted after one year.", "termination"),
    # liability
    ("In any case, our aggregate liability shall not exceed one hundred euros.", "liability_cap"),
    ("Our maximum liability is capped at the fees you paid in the previous six months.", "liability_cap"),
    ("Under no circumstances will we be liable for lost profits.", "no_liability"),
    ("The Company is not responsible for third-party content.", "no_liability"),
    ('The App is provided "as is", without warranties of any kind.', "no_warranty"),
    # indemnity
    ("You agree to indemnify and hold harmless Quillfeather from any claims arising from your use.", "indemnity"),
    ("Users shall defend Quillfeather against third-party claims.", "indemnity"),
    # personal data
    ("We may sell your personal information to data brokers.", "data_sale"),
    ("User data may be rented to other companies.", "data_sale"),
    ("We may disclose your personal information to advertisers and marketing partners.", "data_sharing"),
    ("Your data may be shared with third parties for their own marketing.", "data_sharing"),
    ("We may use your notes to train our machine-learning models.", "ai_training"),
    # tracking and advertising
    ("We and our partners use cookies and similar technologies to show you interest-based ads.", "tracking"),
    ("We may track your activity across other websites and apps.", "tracking"),
    # licence over your content
    ("By uploading a photo, you grant us a perpetual, irrevocable licence to reproduce and distribute it.", "content_licence"),
    ("You give Quillfeather the right to publish your reviews in its marketing.", "content_licence"),
    # governing law and venue
    ("This Agreement shall be governed by the laws of the State of Examplia.", "governing_law"),
    ("You consent to the exclusive jurisdiction of the courts located in Examplia City.", "venue"),
    # fees and refunds
    ("All sales are final and non-refundable.", "no_refunds"),
    ("Refunds will not be issued for unused time.", "no_refunds"),
    ("A late fee of ten euros applies to overdue invoices.", "extra_fees"),
    ("Quillfeather may raise subscription prices at any time.", "price_changes"),
    ("Claims must be filed within one year of the event, or they are permanently barred.", "time_limit"),
    # age limits
    ("You must be 18 or older to use Quillfeather.", "age_limit"),
    ("Users under the age of 13 may not register.", "age_limit"),
    ("Children need parental consent to open an account.", "age_limit"),
    # clauses that favour you
    ("We will not change these terms without your consent.", "changes_need_consent"),
    ("We will email you at least 30 days before a price increase applies to you.", "advance_notice"),
    ("You retain all rights in the notes you write.", "you_own_content"),
    ("You can export all of your notes at any time.", "data_rights"),
    ("You can turn off personalised ads in Settings.", "opt_out"),
    ("You may close your account at any time.", "you_cancel"),
    ("You may opt out of this arbitration agreement within 30 days.", "arbitration_opt_out"),
    ("If the App fails within a week, we will refund you in full.", "refund_right"),
    ("You may bring a claim in the courts where you live.", "local_courts"),
    ("Nothing in these terms limits your statutory rights as a consumer.", "rights_kept"),
    ("We never sell your personal information.", "no_data_sale"),
    ("We will indemnify you against claims that the App infringes a copyright.", "they_indemnify"),
    ("Nothing in these terms prevents you from joining a class action.", "class_action_kept"),
]


@pytest.mark.parametrize(("sentence", "rule"), POSITIVES)
def test_rule_fires_on_its_own_kind_of_clause(sentence, rule):
    assert rule in fired(sentence)


# Near misses: the words a rule looks for are there, but the clause says
# something else (a negation, the reader as subject, a carve-out).
NEAR_MISSES = [
    ("Nothing in these terms requires you to arbitrate a dispute.", "arbitration"),
    ("Disputes will not be resolved by arbitration; either of us may go to court.", "arbitration"),
    ("We do not use binding arbitration.", "arbitration"),
    ("Nothing in these terms prevents you from joining a class action.", "class_waiver"),
    ("You do not waive your right to take part in a class action.", "class_waiver"),
    ("You do not waive any right to a jury trial.", "jury_waiver"),
    ("We will not change these terms without your consent.", "terms_changes"),
    ("Your pass does not renew automatically; buy a new one when it runs out.", "auto_renewal"),
    ("You may terminate this agreement for any reason.", "termination"),
    ("We will not suspend your account without notice.", "termination"),
    ("Your liability to us is not limited.", "liability_cap"),
    ("You are not liable for purchases made after you report your card stolen.", "no_liability"),
    ("We are responsible for losses caused by our negligence.", "no_liability"),
    ("We will indemnify you against claims that the App infringes a copyright.", "indemnity"),
    ("You are not required to indemnify us.", "indemnity"),
    ("We never sell your personal information.", "data_sale"),
    ("We do not and will not sell your personal data.", "data_sale"),
    ("We share your personal data with service providers who process it on our behalf.", "data_sharing"),
    ("We won't share your personal information with advertisers.", "data_sharing"),
    ("We do not use personalised advertising.", "tracking"),
    ("We grant you a personal, non-transferable licence to use the App.", "content_licence"),
    ("We have no right to use your photos.", "content_licence"),
    ("The courts of Examplia have non-exclusive jurisdiction.", "venue"),
    ("Your deposit is fully refundable.", "no_refunds"),
    ("Refund requests must be made within 30 days.", "age_limit"),
    ("We keep backups for at least 3 years.", "age_limit"),
    ("You can delete your notes at any time.", "you_cancel"),
    ("We will not refund partial months.", "refund_right"),
    ("If you do not pay, we may remind you by email.", "termination"),
]


@pytest.mark.parametrize(("sentence", "rule"), NEAR_MISSES)
def test_rule_does_not_fire_on_a_near_miss(sentence, rule):
    assert rule not in fired(sentence)


def test_the_opposite_rule_catches_a_negated_clause():
    assert fired("We do not and will not sell your personal data.") == {"no_data_sale"}
    assert "data_processors" in fired("We share your personal data with service providers who process it on our behalf.")
    assert "app_licence" in fired("We grant you a personal, non-transferable licence to use the App.")


def test_the_company_name_counts_as_we():
    assert company_names(normalise(PREAMBLE)) == ("Quillfeather",)
    assert company_names('Pacewren Health Inc. ("Pacewren") makes a band.') == ("Pacewren",)
    # A defined term that is not a party is not the company.
    assert company_names('The app and website (together, the "Service").') == ()
    sentence = "Quillfeather may suspend your account at any time."
    assert "termination" in fired(sentence)
    assert "termination" not in fired(sentence, preamble="Another Company Ltd. makes the App.\n")


def test_every_topic_the_reader_was_promised_has_a_rule():
    topics = {r["rule"] for r in catalogue()}
    promised = {
        "arbitration", "class_waiver", "jury_waiver", "terms_changes", "auto_renewal", "termination",
        "liability_cap", "indemnity", "data_sale", "data_sharing", "tracking", "content_licence",
        "governing_law", "venue", "no_refunds", "price_changes", "age_limit",
    }
    assert promised <= topics
    for r in RULES:
        assert r.favours in FAVOURS
        assert r.confidence in ("high", "medium", "low")
        assert len(r.reading.split()) <= 25, r.id
        assert r.reading.endswith("."), r.id
    assert len({r.id for r in RULES}) == len(RULES)
    assert len({r.topic for r in RULES}) == len(RULES)


# ---------------------------------------------------------------- quotes

def all_documents():
    docs = {slug: demo_text(slug) for slug in ("nimbus-locker", "pacewren", "brothbike")}
    docs["quillfeather"] = FIXTURE.read_text(encoding="utf-8")
    docs["sentences"] = PREAMBLE + "\n".join(s for s, _ in POSITIVES + NEAR_MISSES)
    return docs


@pytest.mark.parametrize("name", list(all_documents()))
def test_every_quote_is_verbatim_inside_its_clause_and_long_enough(name):
    text = normalise(all_documents()[name])
    segs = segment(text)
    by_id = {s.id: s for s in clauses(segs)}
    result = read(text, segs)
    assert result["dropped"] == 0 and result["dropped_reasons"] == {}
    assert result["verified"] == result["received"] == sum(len(r["findings"]) for r in result["readings"])
    for r in result["readings"]:
        seg = by_id[r["id"]]
        assert 1 <= len(r["findings"]) <= MAX_FINDINGS
        best = r["findings"][0]
        assert (best["quote_start"], best["quote_end"], best["favours"]) == (r["quote_start"], r["quote_end"], r["favours"])
        assert r["rule"] == best["rule"] and r["topic"] == RULES_BY_ID[best["rule"]].topic
        for f in r["findings"]:
            quote = text[f["quote_start"] : f["quote_end"]]
            assert seg.start <= f["quote_start"] < f["quote_end"] <= seg.end
            assert quote == quote.strip()
            assert len(quote) >= MIN_QUOTE_CHARS or len(quote.split()) >= MIN_QUOTE_WORDS or quote == text[seg.start : seg.end]
            assert f["favours"] == RULES_BY_ID[f["rule"]].favours
        ranks = [({"high": 3, "medium": 2, "low": 1}[f["confidence"]], RULES_BY_ID[f["rule"]].weight) for f in r["findings"]]
        assert ranks == sorted(ranks, reverse=True), "the label is the best finding"


def test_quotes_are_whole_words_and_keep_closing_quote_marks():
    text = normalise('The App is provided "as is" and "as available", with no promises.')
    segs = segment(text)
    (r,) = read(text, segs)["readings"]
    assert text[r["quote_start"] : r["quote_end"]] == 'provided "as is" and "as available"'
    text = normalise("Memberships auto-renew yearly.")
    (r,) = read(text, segment(text))["readings"]
    quote = text[r["quote_start"] : r["quote_end"]]
    assert quote.startswith("Memberships auto-renew") or quote.startswith("auto-renew yearly")
    assert len(quote) >= MIN_QUOTE_CHARS


def test_the_synthetic_terms_read_as_expected():
    text = normalise(FIXTURE.read_text(encoding="utf-8"))
    segs = segment(text)
    labels = {}
    for r in read(text, segs)["readings"]:
        seg = next(s for s in segs if s.id == r["id"])
        labels[text[seg.start : seg.end]] = (r["rule"], r["favours"])
    expect = {
        "You must be 18 or older to use the App.": ("age_limit", "neutral"),
        "Quillfeather Pro is billed yearly and will be automatically renewed for successive one-year terms.": ("auto_renewal", "them"),
        "All subscription fees are final and non-refundable.": ("no_refunds", "them"),
        "You retain all rights in the notes you write.": ("you_own_content", "you"),
        "We never sell your personal information.": ("no_data_sale", "you"),
        "Quillfeather may deactivate your account for any reason, without prior notice.": ("termination", "them"),
        "You may close your account at any time.": ("you_cancel", "you"),
        "All disputes will be settled exclusively through binding arbitration.": ("arbitration", "them"),
        "This Agreement is governed by the laws of the State of Examplia.": ("governing_law", "neutral"),
    }
    for clause, want in expect.items():
        assert labels.get(clause) == want, clause
    # Clauses no rule covers are left without a reading, not guessed at.
    assert "Write to legal@quillfeather.example." not in labels


# ------------------------------------------------------ the browser entry

def test_local_payload_has_the_api_shape():
    raw = FIXTURE.read_text(encoding="utf-8")
    out = local.analyze(raw)
    text = normalise(raw)
    segs = segment(text)
    api = service.response_payload(text, "x", segs, None, None, None, None, "demo")
    assert set(out) == set(api)
    assert out["mode"] == "rules" and out["budget"] is None and out["notice"] is None
    assert out["text"] == text and out["segments"] == [s.to_json() for s in segs]
    a = out["analysis"]
    assert a["source"] == "rules" and a["model"] is None
    assert {"readings", "received", "verified", "dropped", "dropped_reasons", "relocated"} <= set(a)
    assert a["engine"]["rules"] == len(RULES) and a["engine"]["elapsed_ms"] >= 0
    assert [c["rule"] for c in a["checked"]] == [r.id for r in RULES]
    cps = list(out["text"])
    for r in a["readings"]:
        assert "".join(cps[r["quote_start"] : r["quote_end"]]) == r["quote"]
        for f in r["findings"]:
            assert "".join(cps[f["quote_start"] : f["quote_end"]]) == f["quote"]
    again = json.loads(local.analyze_json(raw))
    again["analysis"]["engine"].pop("elapsed_ms")
    a["engine"].pop("elapsed_ms")
    assert again == json.loads(json.dumps(out))


def test_local_limits_match_the_worker():
    assert local.MAX_CHARS == service.MAX_CHARS
    assert local.NO_CLAUSES == service.NOTICES["no_clauses"]
    assert local.analyze("  ​ ")["error"] == "empty"
    assert local.analyze("x" * (service.MAX_CHARS + 1))["error"] == "too_large"
    assert local.analyze(None)["error"] == "bad_request"
    heads = local.analyze("TERMS OF SERVICE\n\n1. Definitions")
    assert heads["analysis"] is None and heads["notice"]["kind"] == "no_clauses"


def test_offsets_count_code_points_not_utf16_units():
    raw = "Emoji first \U0001F600. We may sell your personal information to data brokers."
    out = local.analyze(raw)
    (r,) = out["analysis"]["readings"]
    assert list(out["text"])[r["quote_start"] : r["quote_end"]] == list(r["quote"])
    assert r["quote"].startswith("sell your personal information")


def test_a_maximum_size_hostile_document_stays_bounded():
    """Not a performance claim: a guard against runaway pattern matching.
    Repeated trigger words with no sentence ends are the worst case for the
    bounded gaps; the browser runs the same code under Pyodide (slower), in
    a worker it can stop."""
    chunk = "we may suspend you agree to indemnify share your personal data with partners at any time "
    raw = (chunk * (service.MAX_CHARS // len(chunk)))[: service.MAX_CHARS]
    t0 = time.perf_counter()
    out = local.analyze(raw)
    elapsed = time.perf_counter() - t0
    assert out["analysis"] is not None
    assert elapsed < 5.0, f"{elapsed:.2f} s"


def test_a_maximum_size_ordinary_document_reads_quickly_on_cpython():
    base = normalise(demo_text("nimbus-locker"))
    text = ""
    while len(text) + len(base) + 2 <= service.MAX_CHARS:
        text += base + "\n\n"
    t0 = time.perf_counter()
    out = local.analyze(text)
    elapsed = time.perf_counter() - t0
    assert len(out["analysis"]["readings"]) > 300
    assert elapsed < 2.0, f"{elapsed:.2f} s"
