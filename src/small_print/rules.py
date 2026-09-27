"""Rule-based clause reading: a fixed, hand-written pattern library, no AI.

Each rule names one kind of clause that matters to the person agreeing
(forced arbitration, auto-renewal, a licence over your uploads...), says who
it favours, and gives a one-line plain reading. A rule fires on a clause when
one of its patterns matches the clause's own words, and the matched words
become the quote. Those quotes then go through the same verifier as model
output (``verify.verify``): exact substring, inside its own clause, long
enough to be evidence. So the "verified quote" promise holds for rules too.

What the rules do, for each clause:

* every rule's patterns are tried in order; the first match that survives
  the checks below is that rule's finding for the clause;
* a match right after a negation ("we do **not** sell your data") does not
  count for rules marked ``negatable``, and nor does a match with a negation
  inside it ("we will **not** change these terms"); the opposite rule
  ("they promise not to sell your data") usually catches the clause instead;
* ``unless`` is a clause-level veto for known near misses ("nothing here
  prevents you from joining a class action" is not a class-action waiver);
* ``not_you`` drops a match whose subject is the reader ("*you* may
  terminate for any reason" is not the company terminating you);
* ``strong`` upgrades a finding to high confidence when the clause also has
  the words that make the clause clear-cut ("*binding* arbitration").

A clause keeps up to four findings. The one with the highest confidence
(then the highest ``weight``) is the clause's label; the rest are shown as
"also" findings. "Confidence" here means how specific the matched wording is,
not how sure anyone is about the law.

The company's own name counts as "we": a party defined near a company suffix
("Nimbus Locker Ltd. ("Nimbus")") or alongside "we"/"us" is read as the
company, so "Nimbus may suspend your account" matches like "we may...".

Every pattern is a bounded regular expression: gaps between words are lazy
and capped (``[^.;!?\\n]{0,N}?``) and never cross a sentence end or a line
break, so a match never spans two clauses and the work per clause is bounded.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache

from .segment import Segment, clauses
from .verify import MIN_QUOTE_CHARS, MIN_QUOTE_WORDS, verify, with_quotes

# Bump when rule output changes (it is shown on the page with each reading).
RULES_VERSION = "rules-1"
MAX_FINDINGS = 4
CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
FLAGS = re.IGNORECASE


def G(n: int) -> str:
    """A lazy gap of at most ``n`` characters inside one sentence part."""
    return rf"[^.;!?\n]{{0,{n}}}?"


# A greedy tail that carries a quote on to the end of its phrase (a full stop
# inside an address or a number, "optout@nimbus.example", does not end it).
# Negation inside the tail does not cancel the match: it is context.
TAIL = r"(?P<tail>(?:[^.;,!?\n]|\.(?=\S)){0,60})"
THEY = "{THEY}"  # replaced per document by "we", "us", "the company" and the company's names
DATA = (
    r"(?:personal (?:data|information|details)|(?:your|user|customer|health|location|usage) "
    r"(?:data|information|details)|your (?:name|email|contacts|location|activity))"
)
PARTIES = (
    r"(?:(?:business |commercial |advertising |marketing )?partners?|advertis\w*|third[- ]part(?:y|ies)|"
    r"data brokers?|affiliates|sponsors|insurers|employers)"
)
AT_WILL = (
    r"(?:at any time|for any (?:or no )?reason|for no reason|without (?:prior |advance )?(?:notice|cause|reason|"
    r"warning|explanation|liability)|with or without (?:notice|cause)|in (?:our|its) (?:sole|absolute) discretion)"
)
NUMBER = r"(?:\d+|one|two|three|seven|ten|fourteen|thirty|sixty|ninety)"


@dataclass(frozen=True)
class Rule:
    id: str
    topic: str
    favours: str  # "you" | "them" | "neutral" | "unclear"
    reading: str
    patterns: tuple  # regex sources (``{THEY}`` allowed); or (source, confidence) pairs
    confidence: str = "medium"
    strong: str | None = None  # clause regex: medium -> high
    unless: str | None = None  # clause regex: the rule does not fire
    negatable: bool = True
    not_you: bool = False
    weight: int = 50


RULES: tuple[Rule, ...] = (
    # ------------------------------------------------------------ disputes
    Rule(
        "arbitration", "Forced arbitration", "them",
        "Disputes go to a private arbitrator instead of a court, and the decision is final.",
        (
            r"\b(?:final and )?binding (?:individual |confidential )?arbitration\b",
            r"\b(?:resolved|settled|decided|determined|heard)\b" + G(40)
            + r"\b(?:by|through|in|via) (?:final |binding |individual |confidential )*arbitration\b",
            r"\b(?:agree|agrees|required|consent) to (?:binding )?arbitrat(?:e|ion)\b",
            r"\bsubmit(?:ted)?\b" + G(40) + r"\bto (?:final |binding |individual )*arbitration\b",
            r"\b(?:referred|go|goes|brought) to (?:binding )?arbitration\b",
            (r"\barbitration (?:agreement|provision|clause)\b", "low"),
        ),
        strong=r"\bbinding\b|\bfinal\b|\bexclusively\b|\bsolely\b|\bagrees? to arbitrate\b",
        weight=95,
    ),
    Rule(
        "class_waiver", "Class-action waiver", "them",
        "You can only bring claims on your own, not together with others in a class action.",
        (
            r"\b(?:waive|waives|waiving|give up|giving up|waiver of)\b" + G(80)
            + r"\b(?:class|collective|representative|group)(?:[- ]wide)?[- ](?:action|arbitration|proceeding|lawsuit|suit|claim)s?\b",
            r"\b(?:class|collective|representative)[- ](?:action|arbitration|proceeding|lawsuit)s?\b" + G(40)
            + r"\b(?:are|is) (?:not (?:permitted|allowed|available)|waived|prohibited)\b",
            r"\b(?:only|solely) (?:on|in) (?:an|your|their) individual (?:basis|capacity)\b",
            r"\bnot as (?:a )?(?:plaintiff|class member|representative)\b" + TAIL,
            r"\b(?:may not|cannot|can't|will not|won't|shall not)\b" + G(30)
            + r"\b(?:bring|join|participate in|take part in|be part of|act as)\b" + G(30)
            + r"\b(?:class|collective|representative)[- ](?:action|arbitration|proceeding|lawsuit)s?\b",
        ),
        confidence="high",
        unless=r"\b(?:nothing\b" + G(40) + r"\b(?:prevents?|stops?|restricts?|limits?|prohibits?)"
        r"|(?:does|do|will|shall) not (?:prevent|stop|restrict|limit|prohibit|waive))\b" + G(50) + r"\bclass\b",
        negatable=False,
        weight=90,
    ),
    Rule(
        "class_action_kept", "Class actions kept", "you",
        "Nothing here stops you from joining a group claim or class action.",
        (
            r"\b(?:nothing\b" + G(40) + r"\b(?:prevents?|stops?|restricts?|limits?|prohibits?)"
            r"|(?:does|do|will|shall) not (?:prevent|stop|restrict|limit|prohibit|waive))\b" + G(50)
            + r"\b(?:class|collective|group|representative)[- ](?:action|claim|lawsuit|proceeding)s?\b",
        ),
        confidence="high",
        negatable=False,
        weight=70,
    ),
    Rule(
        "jury_waiver", "Jury-trial waiver", "them",
        "You give up the right to have a jury decide a dispute.",
        (
            r"\b(?:waive|waives|waiving|give up|giving up|waiver of)\b" + G(60)
            + r"\b(?:trial by (?:a )?jury|jury trials?)\b",
            r"\b(?:trial by (?:a )?jury|jury trials?)\b" + G(30) + r"\b(?:is|are) (?:hereby )?waived\b",
        ),
        confidence="high",
        weight=85,
    ),
    Rule(
        "arbitration_opt_out", "Arbitration opt-out", "you",
        "You can opt out of arbitration, if you do it in time, and keep your right to go to court.",
        (
            r"\bopt(?:-| )?out of (?:this |the |these )?(?:binding )?arbitration(?: agreement| provision| clause)?\b" + TAIL,
            r"\breject (?:this |the )?arbitration (?:agreement|provision|clause)\b",
        ),
        confidence="high",
        weight=60,
    ),
    Rule(
        "time_limit", "Short time to claim", "them",
        "You get a shortened window to bring a claim; after it, the claim is lost.",
        (
            r"\b(?:must|shall) be (?:brought|filed|commenced|made|started)\b" + G(30)
            + rf"\bwithin {NUMBER} (?:years?|months?|days)\b",
            r"\b(?:claims?|causes? of action|actions?|disputes?)\b" + G(80) + rf"\bwithin {NUMBER} (?:years?|months?)\b"
            + G(50) + r"\b(?:barred|waived|lost|time-barred|forever)\b",
            r"\b(?:is|are|will be|shall be) (?:permanently |forever )?(?:time-)?barred\b",
        ),
        confidence="high",
        weight=55,
    ),
    Rule(
        "venue", "Their choice of court", "them",
        "Court cases must be brought in the courts they choose, which may be far from you.",
        (
            r"(?<!non-)(?<!non )\b(?:exclusive|sole) (?:jurisdiction|venue)\b" + G(100) + r"\bcourts?\b" + TAIL,
            r"\bcourts?\b" + G(60) + r"\b(?:will|shall) have (?:exclusive |sole )?jurisdiction\b",
            r"\b(?:submit|consent|agree)\b" + G(20) + r"\bto the (?:exclusive |personal |sole )+jurisdiction\b" + TAIL,
        ),
        negatable=False,
        weight=45,
    ),
    Rule(
        "local_courts", "Your local courts", "you",
        "You can bring a claim in the courts where you live.",
        (
            r"\b(?:courts?|claims?|proceedings)\b" + G(40)
            + r"\b(?:where you live|of your (?:country|state|place) of residence|where you are resident|"
            r"in your (?:home )?(?:country|state))\b",
        ),
        confidence="high",
        weight=55,
    ),
    Rule(
        "small_claims", "Small claims kept", "you",
        "Small disputes can still go to a small-claims court.",
        (r"\b(?:in |to |use )?(?:a |the )?small[- ]claims? courts?\b" + TAIL,),
        weight=40,
    ),
    Rule(
        "informal_first", "Try to settle first", "neutral",
        "Before a formal claim, you have to raise the problem with them and give them time to fix it.",
        (
            r"\bbefore (?:starting|bringing|filing|commencing|you (?:start|bring|file))\b" + G(40)
            + r"\b(?:claim|dispute|proceedings?|arbitration|lawsuit|action)\b" + G(60)
            + r"\b(?:contact|notify|give us|try to resolve|informal\w*|negotiat\w*)\b",
        ),
        weight=30,
    ),
    Rule(
        "governing_law", "Governing law", "neutral",
        "Says which country's or state's law applies to these terms.",
        (
            r"\bgoverned by\b" + G(40) + r"\blaws? of\b[^.;,\n]{1,60}",
            r"\blaws? of\b[^.;,\n]{1,50}\b(?:govern|governs|apply|applies|will apply|shall apply)\b",
        ),
        confidence="high",
        negatable=False,
        weight=30,
    ),
    # ----------------------------------------------------- changes and exits
    Rule(
        "terms_changes", "They can change the terms", "them",
        "They can change these terms, and the new version can apply to you without a new agreement.",
        (
            rf"\b{THEY}\b" + G(30) + r"\b(?:may|can|reserves? the right to|will)\b" + G(30)
            + r"\b(?:change|modify|amend|update|revise|replace)\b" + G(30)
            + r"\b(?:these|this|the|our|its) (?:terms|agreement|policy|policies|conditions|contract)\b",
            r"\b(?:these|this|the) (?:terms|agreement|conditions)\b" + G(30)
            + r"\b(?:may|can|will) be (?:changed|modified|amended|updated|revised)\b",
        ),
        strong=r"\bat any time\b|\bsole discretion\b|\bwithout (?:prior )?notice\b|\bby posting\b",
        weight=70,
    ),
    Rule(
        "continued_use", "Acceptance by carrying on", "them",
        "If you keep using the service after a change, you are treated as accepting it.",
        (
            r"\b(?:continued|continuing|further) (?:use|access)\b" + G(80)
            + r"\b(?:constitutes|means|signifies|indicates|shows|confirms|counts as|is deemed|will be deemed|"
            r"will be treated as|amounts to)\b" + G(30) + r"\b(?:acceptance|accept|agree|agreement|consent)\w*",
            r"\bby (?:continuing to|continuing|keeping on) (?:use|using|access|accessing)\b" + G(60)
            + r"\byou (?:agree|accept|consent)\w*",
            r"\bif you (?:continue|keep) (?:to use|using)\b" + G(60) + r"\byou (?:agree|accept|are deemed|will be deemed)\w*",
        ),
        confidence="high",
        weight=65,
    ),
    Rule(
        "changes_need_consent", "Changes need your agreement", "you",
        "They cannot change these terms against you without your agreement.",
        (
            r"\b(?:not|never)\b" + G(30) + r"\b(?:change|modify|amend)\w*\b" + G(40)
            + r"\bwithout your (?:express |explicit |written )?(?:consent|agreement|permission)\b",
        ),
        confidence="high",
        negatable=False,
        weight=65,
    ),
    Rule(
        "advance_notice", "Advance notice", "you",
        "They promise to tell you before a change takes effect, which gives you time to act.",
        (
            r"\b(?:notify|tell|email|inform|remind|warn|give) you\b" + G(40)
            + rf"\b(?:at least )?{NUMBER} (?:days|weeks|months)(?:'|’)?(?: (?:notice|before|in advance|prior))?",
            rf"\b(?:at least )?{NUMBER} (?:days|weeks|months)(?:'|’)? (?:prior |advance )?(?:written )?notice\b",
            r"\b(?:notify|tell|inform|email|warn) you\b" + G(30) + r"\b(?:before|in advance|beforehand)\b",
        ),
        confidence="high",
        weight=35,
    ),
    Rule(
        "termination", "They can close your account", "them",
        "They can suspend or close your account whenever they decide to.",
        (
            rf"\b{THEY}\b" + G(30) + r"\b(?:may|can|reserves? the right to|(?:is|are) entitled to)\b" + G(30)
            + r"\b(?:suspend|terminate|close|disable|deactivate|ban|block|end|cancel|restrict|delete)\b" + G(80)
            + rf"\b{AT_WILL}",
            r"\b(?:suspend|terminate|close|disable|deactivate)\w*\b" + G(60)
            + r"\b(?:in (?:our|its) (?:sole|absolute) discretion|for any (?:or no )?reason|without (?:prior )?(?:notice|cause|reason))",
            r"\b(?:accounts?|files|content|data|profiles?)\b" + G(60)
            + r"\b(?:may|can|will) be (?:deleted|closed|suspended|terminated|removed|disabled)\b" + TAIL,
        ),
        strong=r"\bat any time\b|\bfor any (?:or no )?reason\b|\bwithout (?:prior )?(?:notice|cause|reason)\b|\bsole discretion\b",
        not_you=True,
        weight=75,
    ),
    Rule(
        "service_changes", "They can change or stop the service", "them",
        "They can change, cut back or shut down the service, or features you rely on.",
        (
            r"\b(?:may|can|reserves? the right to)\b" + G(20)
            + r"\b(?:modify|change|suspend|discontinue|stop|withdraw|remove|reduce|limit)\b" + G(50)
            + r"\b(?:service|services|app|platform|features?|functionality|storage|products?)\b" + G(40)
            + r"\b(?:at any time|from time to time|without (?:prior )?notice|for any reason|in (?:our|its) (?:sole )?discretion)",
            r"\b(?:may|can) be (?:withdrawn|changed|modified|discontinued|cancell?ed|revoked|removed)\b" + G(20)
            + r"\b(?:at any time|without (?:prior |advance )?notice|for any reason)",
            r"\bnot (?:obliged|obligated|required|bound) to (?:keep |continue )?(?:support|provid|maintain|offer|updat)\w*" + TAIL,
            (r"\b(?:discontinue|shut down|stop (?:offering|providing))\b" + G(30) + r"\b(?:the |any |our )?(?:service|services|app)\b", "low"),
        ),
        not_you=True,
        weight=45,
    ),
    Rule(
        "remove_content", "They can remove your content", "them",
        "They can take down what you post or upload, on their own judgment.",
        (
            r"\b(?:may|can|reserves? the right to)\b" + G(20)
            + r"\b(?:remove|delete|take down|disable|refuse|block|edit)\b" + G(40)
            + r"\b(?:content|posts?|files?|material|reviews?|listings?|comments?|uploads?)\b" + G(60)
            + r"\b(?:at any time|for any (?:or no )?reason|without (?:prior )?notice|in (?:our|its) (?:sole|absolute) discretion|"
            r"that (?:we|it) (?:believe|consider|think|decide)s?)",
        ),
        not_you=True,
        weight=40,
    ),
    Rule(
        "assignment", "They can pass the contract on", "them",
        "They can hand this agreement, and your account, to another company.",
        (
            rf"\b{THEY}\b" + G(20) + r"\b(?:may|can)\b" + G(15) + r"\b(?:assign|transfer)\b" + G(40)
            + r"\b(?:these terms|this agreement|the agreement|our rights|its rights|our obligations)\b",
        ),
        confidence="low",
        weight=25,
    ),
    Rule(
        "you_cancel", "You can leave any time", "you",
        "You can cancel or close your account whenever you want.",
        (
            r"\byou (?:can|may|are free to)\b" + G(30)
            + r"\b(?:cancel|close|terminate|end|stop using|leave|delete (?:your )?account)\b" + G(60)
            + r"\b(?:at any time|any ?time|whenever you (?:like|want|wish)|for any reason|free of charge|at no (?:extra )?(?:cost|charge))",
        ),
        confidence="high",
        weight=50,
    ),
    Rule(
        "mutual_termination", "Either side can end it", "neutral",
        "Either side can end the agreement.",
        (r"\beither (?:party|of us)\b" + G(30) + r"\b(?:may|can)\b" + G(20) + r"\b(?:terminate|end|cancel)\b" + TAIL,),
        weight=30,
    ),
    # --------------------------------------------------------------- money
    Rule(
        "auto_renewal", "Automatic renewal", "them",
        "The subscription renews and charges you automatically until you cancel.",
        (
            r"\b(?:renews?|renewed|renewal|extends?|extended|continues?|converts?)\b" + G(30) + r"\bautomatically\b",
            r"\bautomatic(?:ally)?(?:[- ]renew\w*| (?:renew\w*|extend\w*|convert\w*|charged?|billed|bill\w*))\b",
            r"\bauto[- ]?renew\w*\b",
            r"\b(?:continues?|renews?|billed|charged)\b" + G(30) + r"\buntil you cancel\b",
            (r"\b(?:recurring|continuous) (?:billing|charges?|payments?|subscription)\b", "low"),
        ),
        strong=r"\bthen[- ]current (?:price|rate|fee)|\bunless you cancel\b|\buntil you cancel\b",
        weight=60,
    ),
    Rule(
        "price_changes", "They can raise prices", "them",
        "They can change what you pay; the new price applies unless you cancel.",
        (
            r"\b(?:may|can|reserves? the right to)\b" + G(20) + r"\b(?:change|increase|raise|adjust|modify)\b" + G(20)
            + r"\b(?:fees?|prices?|pricing|charges|rates)\b" + TAIL,
            r"\bat the then[- ]current (?:price|rate|fee)s?\b",
            r"\b(?:fees?|prices?|pricing)\b" + G(20) + r"\b(?:may|can|are subject to|is subject to) (?:change|increase)\b",
        ),
        strong=r"\bat any time\b|\bwithout (?:prior |advance )?notice\b",
        not_you=True,
        weight=50,
    ),
    Rule(
        "no_refunds", "No refunds", "them",
        "Money you pay generally will not be refunded, even for time you do not use.",
        (
            r"\b(?:is|are|will be|shall be)\s+(?:strictly\s+|entirely\s+|completely\s+|final and\s+)?non-?refundable\b" + TAIL,
            r"\bno refunds?\b" + TAIL,
            r"\brefunds?\b" + G(20) + r"\b(?:is|are|will|shall)\s+not\s+(?:be\s+)?(?:given|available|provided|issued|offered|made|due)\b",
            r"\b(?:do not|don't|will not|won't) (?:offer|give|provide|issue) refunds?\b",
            (r"\brefunds?\b" + G(40) + r"\b(?:at|in) (?:our|its) (?:sole |absolute )?discretion\b", "medium"),
        ),
        confidence="high",
        negatable=False,
        weight=60,
    ),
    Rule(
        "extra_fees", "Extra charges", "them",
        "You may have to pay fees on top of the headline price, or pay even when you cancel.",
        (
            (r"\b(?:late|cancellation|termination|early termination|service|demand|processing|administration|admin|"
             r"restocking|surge|convenience|inactivity) (?:fees?|charges?)\b", "low"),
            r"\b(?:may|will|shall) be charged (?:in full|the full (?:amount|price))\b",
            r"\binterest (?:at|of) (?:[\d.]+ ?(?:%|percent))",
        ),
        weight=45,
    ),
    Rule(
        "expiring_credit", "Credit that expires", "them",
        "Credit, points or balances they give you can expire or cannot be cashed out.",
        (
            r"\b(?:credits?|balances?|points|vouchers?|gift cards?)\b" + G(40)
            + r"\b(?:expires?|expire after|lapses?|cannot be (?:exchanged|redeemed|converted) for cash|non-transferable)\b" + TAIL,
        ),
        negatable=False,
        weight=40,
    ),
    Rule(
        "refund_right", "Refunds", "you",
        "You can get your money back in the cases this clause describes.",
        (
            r"\b(?:we will|we'll|we shall|you will (?:get|receive)|you (?:can|may) (?:get|receive|claim|request|ask for))\b"
            + G(30) + r"\b(?:a )?(?:full |pro[- ]?rata |prorated |partial )?refund\w*\b" + TAIL,
            r"\bfor a (?:full |pro[- ]?rata |prorated |partial )refund\b",
            r"\b(?:money[- ]back|refund) guarantee\b",
            r"\b(?:we will|we'll) refund\b" + TAIL,
        ),
        confidence="high",
        weight=55,
    ),
    # ---------------------------------------------------------- liability
    Rule(
        "liability_cap", "Liability cap", "them",
        "If something goes wrong, the most they will pay you is capped, often at what you paid.",
        (
            r"\b(?:total|aggregate|maximum|entire|cumulative|combined|whole) liability\b" + G(160)
            + r"\b(?:(?:shall|will|does|may|won't) not exceed|(?:is|are|be|will be|shall be) (?:limited|capped) (?:to|at)|"
            r"limited to|not to exceed)" + TAIL,
            r"\bliability\b" + G(60) + r"\b(?:is|are|be|will be|shall be) limited to (?:the )?(?:greater|lesser|amount|fees?|sum|total|price)" + TAIL,
        ),
        confidence="high",
        negatable=False,
        weight=70,
    ),
    Rule(
        "no_liability", "Not responsible", "them",
        "They say they will not be responsible for certain losses, even ones their service causes.",
        (
            r"\b(?:in no event|under no circumstances)\b" + G(100) + r"\b(?:be )?(?:liable|responsible)\b" + TAIL,
            rf"\b{THEY}\b" + G(30)
            + r"\b(?:(?:are|is|will|shall|can|do|does) not|won't|isn't|aren't|cannot|can't|never)\s+(?:be\s+|accept\s+|take\s+)?"
            r"(?:held\s+)?(?:liable|responsible|liability|responsibility)\b" + TAIL,
            r"\bnot (?:be )?(?:liable|responsible) for (?:any )?(?:indirect|incidental|special|consequential|punitive|exemplary|lost|loss)\w*" + TAIL,
            r"\b(?:disclaim|exclude)s?\b" + G(30) + r"\b(?:all |any )?liability\b",
        ),
        strong=r"\bindirect\b|\bconsequential\b|\bincidental\b|\bpunitive\b|\bany (?:loss|damage|injury)\b|\blost profits\b|\bloss of (?:data|profits?|revenue)\b",
        negatable=False,
        not_you=True,
        weight=65,
    ),
    Rule(
        "no_warranty", "No promises it works", "them",
        "The service comes with no promise that it works, is accurate or suits your needs.",
        (
            r"\b(?:provided|offered|made available|supplied)\b" + G(10)
            + r"(?:on an )?[\"“']?as[- ]is[\"”']?(?:,? (?:and|or) [\"“']?as[- ]available[\"”']?)?",
            r"\bwithout (?:any )?(?:warranty|warranties|guarantees?)(?: of any kind)?\b",
            r"\b(?:disclaim|exclude)s?\b" + G(30) + r"\b(?:all |any )?(?:warranties|guarantees|conditions)\b",
            r"\b(?:make|makes|give|gives) no (?:warranties|warranty|guarantees?|promises|representations)\b",
            r"\b(?:do not|does not|don't|doesn't|cannot|can't|can not) (?:guarantee|warrant|promise)\b" + TAIL,
            r"\b(?:are|is) (?:only )?(?:estimates|approximate|approximations|indicative)(?: only)?\b",
            r"\bmay (?:be|contain|include) (?:inaccura\w+|incomplete|errors|out of date|out-of-date)\b",
            r"\bnot (?:a |an )?(?:medical|legal|financial|professional|investment|tax) (?:advice|device|adviser|advisor)\b",
        ),
        negatable=False,
        weight=50,
    ),
    Rule(
        "warranty", "A warranty for you", "you",
        "They give you a promise about quality, with a repair, replacement or refund if it fails.",
        (
            r"\b(?:limited |full |manufacturer'?s? )?(?:warranty|guarantee) (?:against|for|covering) (?:defects|faults)\b" + TAIL,
            r"\b(?:we|{THEY}) (?:will|shall) (?:repair|replace)\b" + TAIL,
        ),
        weight=50,
    ),
    Rule(
        "accepts_liability", "They accept some responsibility", "you",
        "They accept responsibility for some kinds of loss.",
        (
            rf"\b{THEY}\b" + G(20) + r"\b(?:are|is|will be|shall be|remain|remains)\s+(?:fully\s+)?(?:liable|responsible)\s+for\b" + TAIL,
        ),
        weight=45,
    ),
    Rule(
        "rights_kept", "Rights the law protects", "you",
        "Some of your legal rights cannot be taken away by these terms, and this clause says so.",
        (
            r"\bnothing in (?:these|this|the)\b" + G(60)
            + r"\b(?:limits?|excludes?|restricts?|affects?|removes?|reduces?)\b" + G(60) + r"\b(?:liability|rights?)\b",
            r"\b(?:does|do|will|shall) not (?:affect|limit|exclude|restrict|reduce)\b" + G(40)
            + r"\byour (?:statutory |legal |consumer |mandatory )?rights\b",
            r"\b(?:in addition to|without prejudice to)\b" + G(30) + r"\b(?:any )?(?:statutory |legal |consumer )?rights you (?:have|may have)\b",
        ),
        confidence="high",
        negatable=False,
        weight=60,
    ),
    Rule(
        "indemnity", "You cover their costs", "them",
        "If they are sued or lose money over something you did, you agree to pay their costs.",
        (
            r"\b(?:you|users?|customers?|members?|subscribers?)\s+(?:(?:hereby|also|further)\s+)?"
            r"(?:agree|agrees|will|shall|must|undertake|undertakes|are required|consent)(?:\s+to)?\s+(?:\w+\s+){0,2}?"
            r"(?:indemnify|defend|hold\b" + G(40) + r"\bharmless|reimburse)\b" + TAIL,
            rf"\b(?:indemnify|defend|hold harmless)\b" + G(20) + rf"\b(?:us|{THEY})\b" + TAIL,
        ),
        confidence="high",
        weight=80,
    ),
    Rule(
        "they_indemnify", "They cover your costs", "you",
        "They agree to cover your costs if you are sued over their service.",
        (
            rf"\b{THEY}\b" + G(30) + r"\b(?:will|shall|agrees? to|undertakes? to)\s+(?:\w+\s+){0,2}?"
            r"(?:indemnify|defend|reimburse|compensate) you\b" + TAIL,
        ),
        confidence="high",
        weight=75,
    ),
    Rule(
        "account_risk", "You carry your account's risk", "them",
        "Anything done with your account is your responsibility, even if someone else did it.",
        (
            r"\byou (?:are|will be|shall be|remain) (?:solely |fully |entirely )?responsible for\b" + G(40)
            + r"\b(?:all )?(?:activity|activities|use|actions|charges|conduct)\b" + TAIL,
        ),
        confidence="low",
        weight=25,
    ),
    # -------------------------------------------------------- your content
    Rule(
        "content_licence", "Licence to your content", "them",
        "You give them the right to use, copy and share what you upload or post.",
        (
            rf"\b(?:grant|grants|granting|give|gives)\s+(?:to\s+)?(?:us|{THEY})\b" + G(120) + r"\b(?:licen[cs]e|rights?)\b" + TAIL,
            r"\b(?:licen[cs]e|right)\s+to\s+(?:use|host|store|reproduce|copy|modify|adapt|publish|distribute|display|sell|commerciali[sz]e)\b"
            + G(80) + r"\b(?:your |user |any )?(?:content|photos?|images?|videos?|posts?|reviews?|submissions?|materials?|uploads?|files?|feedback|likeness)\b",
        ),
        strong=r"\bperpetual\b|\birrevocable\b|\bsub-?licens\w+|\btransferable\b|\broyalty[- ]free\b|\bworldwide\b|\badvertis\w+|\bpromot\w+",
        weight=80,
    ),
    Rule(
        "licence_survives", "Licence outlives your account", "them",
        "The licence you gave them keeps running after you delete content or leave.",
        (
            r"\blicen[cs]e\b" + G(40) + r"\b(?:continues|survives|remains|lasts|continue|survive)\b" + G(60)
            + r"\bafter\b" + G(40) + r"\b(?:delete|close|terminat|remove|leave|end|cancel)\w*" + TAIL,
        ),
        confidence="high",
        weight=70,
    ),
    Rule(
        "you_own_content", "You keep ownership", "you",
        "What you upload or create stays yours.",
        (
            r"\byou (?:retain|keep|own|continue to own|remain the owner of)\b" + G(40)
            + r"\b(?:ownership|rights?|title|intellectual property|content|files|data|photos|posts|everything)\b" + TAIL,
            r"\b(?:do|does|will) not (?:claim|take|acquire)\b" + G(20) + r"\bownership\b" + TAIL,
            r"\byour (?:content|files|data|photos|posts) (?:remains?|stays?|is|are) yours\b",
        ),
        confidence="high",
        negatable=False,
        weight=65,
    ),
    Rule(
        "app_licence", "Licence to use the app", "neutral",
        "They let you use the service, on their terms.",
        (rf"\b{THEY}\b" + G(20) + r"\bgrants?\s+you\b" + G(80) + r"\blicen[cs]e\b" + TAIL,),
        weight=20,
    ),
    # ------------------------------------------------------------ your data
    Rule(
        "data_sale", "Selling your data", "them",
        "They may sell or rent out your personal information.",
        (
            r"\b(?:sell|sells|selling|rent|rents|renting|trade|trades|monetise|monetize)\b" + G(40) + rf"\b(?:your |the )?{DATA}",
            rf"\b{DATA}\b" + G(40) + r"\b(?:may|can|will|might) be (?:sold|rented|traded|licensed)\b",
        ),
        confidence="high",
        weight=85,
    ),
    Rule(
        "no_data_sale", "They do not sell your data", "you",
        "They promise not to sell your personal information.",
        (
            r"\b(?:do|does|will|shall) not (?:and will not )?(?:sell|rent|trade)\b" + G(40) + rf"\b(?:your |the )?{DATA}",
            r"\b(?:never|won't|don't|doesn't) (?:sell|rent|trade)\b" + G(40) + r"\b(?:data|information|details)\b",
            r"\b(?:is|are) never (?:sold|rented)\b",
        ),
        confidence="high",
        negatable=False,
        weight=80,
    ),
    Rule(
        "data_sharing", "Sharing your data", "them",
        "They may pass your personal information to other companies, such as advertisers or partners.",
        (
            r"\b(?:share|shares|sharing|disclose|discloses|transfer|transfers|provide|provides|pass|passes)\b" + G(60)
            + rf"\b{DATA}\b" + G(80) + rf"\b{PARTIES}\b" + TAIL,
            rf"\b{DATA}\b" + G(40) + r"\b(?:may be |is |are |will be )?(?:shared with|disclosed to|transferred to|sold to|passed to|made available to)\b"
            + G(50) + rf"\b{PARTIES}\b",
        ),
        strong=r"\badvertis\w*|\bmarketing\b|\bdata brokers?\b|\binsurers?\b|\bemployers?\b",
        unless=r"^(?!.*\b(?:advertis|marketing|data broker|insurer|employer))(?=.*\b(?:service providers?|processors?|on (?:our|its) behalf)\b)",
        weight=75,
    ),
    Rule(
        "data_processors", "Data shared to run the service", "neutral",
        "They share data with companies that help run the service or deliver your order.",
        (
            r"\b(?:share|shares|disclose|provide|pass|give)\w*\b" + G(80)
            + r"\b(?:service providers?|processors?|contractors|vendors|suppliers)\b" + TAIL,
            r"\b(?:share|shares|disclose|provide|pass|give)\w*\b" + G(80)
            + r"\b(?:for your order|to (?:deliver|fulfil|fulfill|complete|process) (?:your |the )?(?:order|delivery|payment)s?)\b",
        ),
        weight=30,
    ),
    Rule(
        "deidentified", "Data they say is anonymous", "unclear",
        "They use or share data they say cannot identify you; how well that holds depends on the method.",
        (r"\b(?:aggregated|de-?identified|anonymi[sz]ed|pseudonymi[sz]ed)\b" + G(40)
         + r"\b(?:data|information|form|results|statistics|insights|reports)\b" + TAIL,),
        weight=35,
    ),
    Rule(
        "ai_training", "Your content trains their AI", "them",
        "They may use your content or data to train automated systems or AI models.",
        (
            r"\b(?:use|uses|using)\b" + G(80) + r"\b(?:content|files|data|messages|photos|posts|uploads|conversations)\b" + G(60)
            + r"\bto (?:train|develop|build|improve)\b" + G(40)
            + r"\b(?:models?|AI|artificial intelligence|machine[- ]learning|algorithms?|automated (?:features|systems|tools))\b",
            r"\b(?:train|training)\b" + G(40) + r"\b(?:machine[- ]learning|artificial intelligence|AI|language) models?\b",
        ),
        weight=70,
    ),
    Rule(
        "tracking", "Tracking and targeted ads", "them",
        "They track what you do and may use it to target ads at you.",
        (
            r"\b(?:targeted|personali[sz]ed|interest[- ]based|behaviou?ral|tailored) (?:ads|advertising|advertisements|adverts|marketing)\b",
            r"\b(?:use|uses|place|places|set|sets)\b" + G(30)
            + r"\b(?:cookies|web beacons|pixels?|tracking technologies|similar technologies|device identifiers|advertising ids?)\b" + TAIL,
            r"\b(?:use|uses|analy[sz]e)\b" + G(40) + r"\b(?:order history|browsing|activity|behaviou?r|purchases|usage)\b" + G(40)
            + r"\b(?:to (?:show|send|serve|target|personali[sz]e)|for)\b" + G(20)
            + r"\b(?:ads|advertising|advertisements|adverts|marketing|recommendations)\b",
            r"\btrack\w*\b" + G(40) + r"\bacross (?:other )?(?:sites|websites|apps|services)\b",
        ),
        weight=50,
    ),
    Rule(
        "marketing_consent", "Marketing messages", "them",
        "By agreeing, you sign up to receive marketing from them.",
        (
            r"\byou (?:agree|consent) to receive\b" + G(40)
            + r"\b(?:marketing|promotional|commercial|advertising|offers|text messages|SMS|calls)\b" + TAIL,
        ),
        weight=40,
    ),
    Rule(
        "opt_out", "You can opt out", "you",
        "You can switch this off or opt out.",
        (
            r"\byou (?:can|may|are able to|have the right to)\b" + G(20)
            + r"\b(?:opt[- ]out|turn off|switch off|disable|withdraw (?:your )?consent|unsubscribe|object)\b" + TAIL,
            r"\b(?:opt[- ]out|unsubscribe)\b" + G(30) + r"\b(?:at any time|any ?time)\b",
        ),
        confidence="high",
        unless=r"\bopt(?:-| )?out of (?:this |the |these )?(?:binding )?arbitration",
        weight=55,
    ),
    Rule(
        "data_rights", "Your data rights", "you",
        "You can get a copy of your data, or have it deleted.",
        (
            r"\byou (?:can|may|have the right to|are entitled to)\b" + G(20)
            + r"\b(?:request |ask (?:us )?to )?(?:export|download|port)\b" + TAIL,
            r"\byou (?:can|may|have the right to|are entitled to)\b" + G(20)
            + r"\b(?:request |ask (?:us )?to )?(?:delete|erase|access|correct|obtain|receive)\b" + G(40)
            + r"\b(?:data|information|files|content|copy|notes|photos|messages|documents|history)\b" + TAIL,
            r"\bright to (?:access|delete|erase|export|correct|port|object)\b" + TAIL,
            r"\b(?:we will|we'll) (?:delete|erase|export)\b" + G(40) + r"\b(?:data|information|files)\b" + TAIL,
        ),
        confidence="high",
        weight=55,
    ),
    # -------------------------------------------------------------- people
    Rule(
        "age_limit", "Age limit", "neutral",
        "Sets a minimum age for using the service.",
        (
            r"\b(?:at least|over|under|below|older than|younger than|aged)\s+(?:the age of\s+)?\d{1,2}"
            r"(?:\s+years?[- ](?:old|of age)|\+|\s+or (?:older|over))",
            r"\b(?:the )?age of (?:\d{1,2}|majority|eighteen|sixteen|thirteen)\b",
            r"\b(?:must|need to|have to) be (?:at least )?(?:\d{1,2}|eighteen|sixteen|thirteen)(?: or (?:older|over))?\b",
            r"\bparent(?:al)?(?: or (?:legal )?guardian(?:'s)?)? (?:consent|permission|supervision)\b",
        ),
        confidence="high",
        negatable=False,
        weight=20,
    ),
)

RULES_BY_ID = {r.id: r for r in RULES}

# --------------------------------------------------------------- the company

_PAREN = re.compile(r"\(([^()\n]{1,160})\)")
_QUOTED = re.compile(r"[\"“”]([^\"“”\n]{1,40})[\"“”]")
_CORP_BEFORE = re.compile(
    r"\b(?:inc|ltd|llc|l\.l\.c|limited|corp|corporation|gmbh|ag|s\.a|sas|b\.v|plc|co|pty|llp|company)\.?,?\s*$",
    re.IGNORECASE,
)
_WE = {"we", "us", "our"}
_NOT_A_PARTY = {
    "you", "your", "user", "users", "customer", "customers", "member", "members", "subscriber",
    "terms", "agreement", "service", "services", "app", "apps", "site", "website", "platform", "content",
    "account", "privacy notice", "privacy policy", "policy", "data", "health data", "personal data",
    "company", "the company", "software", "product", "products",
}
MAX_NAMES = 5


def company_names(text: str) -> tuple[str, ...]:
    """Names the document defines for the company: quoted terms in a
    parenthesis that also defines "we"/"us", or that follows a company
    suffix ("Pacewren Health Inc. ("Pacewren")"). Definitions sit near the
    top, so only the first 20,000 characters are searched."""
    names: list[str] = []
    head = text[:20_000]
    for m in _PAREN.finditer(head):
        quoted = [q.strip() for q in _QUOTED.findall(m.group(1))]
        if not quoted:
            continue
        lowered = {q.lower() for q in quoted}
        if not (lowered & _WE or _CORP_BEFORE.search(head[max(0, m.start() - 60) : m.start()])):
            continue
        for q in quoted:
            low = q.lower()
            if low in _WE or low in _NOT_A_PARTY or not q[:1].isupper() or q in names:
                continue
            names.append(q)
            if len(names) >= MAX_NAMES:
                return tuple(names)
    return tuple(names)


# ------------------------------------------------------------- compiling


@dataclass(frozen=True)
class _Compiled:
    rule: Rule
    patterns: tuple[tuple[re.Pattern, str], ...]
    strong: re.Pattern | None
    unless: re.Pattern | None


@lru_cache(maxsize=16)
def _compile(names: tuple[str, ...]) -> tuple[_Compiled, ...]:
    they = "(?:" + "|".join(["we", "us", "the company", "company", *(re.escape(n) for n in names)]) + ")"
    out = []
    for rule in RULES:
        pats = []
        for p in rule.patterns:
            src, conf = (p, rule.confidence) if isinstance(p, str) else p
            pats.append((re.compile(src.replace(THEY, they), FLAGS), conf))
        out.append(
            _Compiled(
                rule,
                tuple(pats),
                re.compile(rule.strong, FLAGS) if rule.strong else None,
                re.compile(rule.unless, FLAGS | re.DOTALL) if rule.unless else None,
            )
        )
    return tuple(out)


# -------------------------------------------------------------- matching

_NEGATORS = {"not", "never", "no", "nor", "neither", "without", "cannot", "nothing", "none"}
_INNER_NEGATORS = {"not", "never", "cannot"}
_WORD = re.compile(r"[a-z’']+", re.IGNORECASE)
_PART_BREAK = re.compile(r"[.;:!?,]")
_YOU_BEFORE = re.compile(r"\byou\s+(?:[a-z]+\s+){0,2}$", re.IGNORECASE)


def _is_negation(word: str) -> bool:
    w = word.lower().replace("’", "'")
    return w in _NEGATORS or w.endswith("n't")


def _negated_before(body: str, at: int) -> bool:
    """A negation among the last five words before ``at``, within the same
    part of the sentence (the window stops at a comma or colon)."""
    window = body[:at]
    breaks = list(_PART_BREAK.finditer(window))
    if breaks:
        window = window[breaks[-1].end() :]
    words = _WORD.findall(window)[-5:]
    return any(_is_negation(w) for w in words)


def _negated_inside(quote: str) -> bool:
    for w in _WORD.findall(quote):
        low = w.lower().replace("’", "'")
        if low in _INNER_NEGATORS or low.endswith("n't"):
            return True
    return False


def _long_enough(quote: str) -> bool:
    return len(quote) >= MIN_QUOTE_CHARS or len(quote.split()) >= MIN_QUOTE_WORDS


# Words a quote should neither end on nor grow by first when it is widened.
_FUNCTION_WORDS = frozenset(
    "a an and or but nor the to of in on at by for from with as that which who than is are be "
    "into onto about under over per via any all each its their your our this these those if when where".split()
)
# A quote that stops this close to the end of its clause runs on to the end.
_RUN_ON = 25
_END_JUNK = ",;:-(“‘"


def _trim_end(body: str, s: int, e: int) -> int:
    while e > s and (body[e - 1].isspace() or body[e - 1] in _END_JUNK):
        e -= 1
    # A straight double quote at the end is kept only if it closes one.
    if e > s and body[e - 1] == '"' and body[s:e].count('"') % 2 == 1:
        e -= 1
        while e > s and body[e - 1].isspace():
            e -= 1
    return e


def _tidy(body: str, s: int, e: int) -> tuple[int, int]:
    """Trim a match to whole words without edge punctuation, and widen it a
    word at a time if it is too short to count as evidence (to the right,
    unless the next word is a function word and there is room on the left)."""
    n = len(body)
    while 0 < e < n and body[e - 1].isalnum() and body[e].isalnum():
        e += 1  # never end inside a word
    while 0 < s < e and body[s].isalnum() and body[s - 1].isalnum():
        s -= 1
    while s < e and (body[s].isspace() or body[s] in ",;:-)"):
        s += 1
    e = _trim_end(body, s, e)
    while not _long_enough(body[s:e]) and (e < n or s > 0):
        nxt = body.find(" ", e + 1)
        nxt = n if nxt == -1 else nxt
        next_word = body[e:nxt].strip().strip(",;:").lower()
        if e < n and (s == 0 or next_word not in _FUNCTION_WORDS):
            e = nxt
        else:
            prev = body.rfind(" ", 0, max(s - 1, 0))
            s = 0 if prev == -1 else prev + 1
        e = _trim_end(body, s, e)
    rest = body[e:].rstrip(" .;:!?\"'”’)")
    if 0 < len(rest) <= _RUN_ON and not any(c in rest for c in ".,;!?\n"):
        e = _trim_end(body, s, e + len(rest))  # "... arising from your notes", not "... arising from"
    words = body[s:e].split()
    while len(words) > 1 and words[-1].lower().strip(",;:") in _FUNCTION_WORDS:
        cut = body.rfind(" ", s, e)
        if cut <= s or not _long_enough(body[s:cut].rstrip()):
            break
        e = _trim_end(body, s, cut)  # never end on "from", "the", "and"...
        words = body[s:e].split()
    return s, e


@dataclass(frozen=True)
class Finding:
    rule: Rule
    clause: int
    start: int  # code-point offsets into the whole text
    end: int
    confidence: str

    def sort_key(self) -> tuple[int, int]:
        return (CONFIDENCE_RANK[self.confidence], self.rule.weight)


def find(text: str, segments: list[Segment]) -> dict[int, list[Finding]]:
    """Every rule's findings, per clause id, best first (at most MAX_FINDINGS)."""
    compiled = _compile(company_names(text))
    clause_segs = clauses(segments)
    starts = [s.start for s in clause_segs]

    def clause_at(pos: int) -> Segment | None:
        i = bisect_right(starts, pos) - 1
        if i >= 0 and clause_segs[i].start <= pos < clause_segs[i].end:
            return clause_segs[i]
        return None

    found: dict[int, dict[str, Finding]] = {}
    for c in compiled:
        rule = c.rule
        vetoed: dict[int, bool] = {}
        for rx, conf in c.patterns:
            for m in rx.finditer(text):
                seg = clause_at(m.start())
                if seg is None or m.end() > seg.end or m.end() <= m.start():
                    continue
                if rule.id in found.get(seg.id, {}):
                    continue
                body = text[seg.start : seg.end]
                if c.unless is not None:
                    if seg.id not in vetoed:
                        vetoed[seg.id] = bool(c.unless.search(body))
                    if vetoed[seg.id]:
                        continue
                s, e = _tidy(body, m.start() - seg.start, m.end() - seg.start)
                if e <= s:
                    continue
                # Negation counts in the matched words themselves, not in the
                # tail that carries the quote to the end of its phrase.
                tail = m.start("tail") if "tail" in rx.groupindex and m.group("tail") else m.end()
                if rule.negatable and (_negated_before(body, s) or _negated_inside(text[m.start() : tail])):
                    continue
                if rule.not_you and _YOU_BEFORE.search(body[:s]):
                    continue
                level = conf
                if level == "medium" and c.strong is not None and c.strong.search(body):
                    level = "high"
                found.setdefault(seg.id, {})[rule.id] = Finding(rule, seg.id, seg.start + s, seg.start + e, level)

    out: dict[int, list[Finding]] = {}
    for cid, by_rule in found.items():
        ranked = sorted(by_rule.values(), key=Finding.sort_key, reverse=True)
        out[cid] = ranked[:MAX_FINDINGS]
    return out


def read(text: str, segments: list[Segment]) -> dict:
    """Read ``text`` with the rule set.

    Returns the verifier's analysis shape (``readings``, ``received``,
    ``verified``, ``dropped``, ``dropped_reasons``, ``relocated``). Each
    reading is one clause: its label, confidence, reading and quote come
    from the clause's best finding, and ``findings`` lists every finding
    (best first), each with its own verified quote offsets. Every quote,
    primary or not, passes through ``verify.verify`` exactly as a model's
    quote would.
    """
    by_clause = find(text, segments)
    rounds = max((len(v) for v in by_clause.values()), default=0)
    readings: dict[int, dict] = {}
    received = verified = dropped = 0
    reasons: dict[str, int] = {}
    for k in range(rounds):
        batch = [(cid, fs[k]) for cid, fs in sorted(by_clause.items()) if len(fs) > k]
        items = [
            {
                "id": cid,
                "quote": text[f.start : f.end],
                "favours": f.rule.favours,
                "reading": f.rule.reading,
                "confidence": f.confidence,
            }
            for cid, f in batch
        ]
        result = verify(text, segments, {"clauses": items})
        received += result["received"]
        verified += result["verified"]
        dropped += result["dropped"]
        for key, v in result["dropped_reasons"].items():
            reasons[key] = reasons.get(key, 0) + v
        rule_of = {cid: f.rule for cid, f in batch}
        for r in result["readings"]:
            rule = rule_of[r["id"]]
            finding = {
                "rule": rule.id,
                "topic": rule.topic,
                "favours": r["favours"],
                "confidence": r["confidence"],
                "reading": r["reading"],
                "quote_start": r["quote_start"],
                "quote_end": r["quote_end"],
            }
            if r["id"] not in readings:
                # The first round holds each clause's best finding: its label.
                readings[r["id"]] = {**r, "rule": rule.id, "topic": rule.topic, "findings": [finding]}
            else:
                readings[r["id"]]["findings"].append(finding)
    return {
        "readings": [readings[k] for k in sorted(readings)],
        "received": received,
        "verified": verified,
        "dropped": dropped,
        "dropped_reasons": reasons,
        "relocated": 0,
    }


def with_finding_quotes(text: str, readings: list[dict]) -> list[dict]:
    """``verify.with_quotes`` for a rule reading and each of its findings."""
    out = []
    for r in with_quotes(text, readings):
        r = dict(r)
        r["findings"] = with_quotes(text, r.get("findings", []))
        out.append(r)
    return out


def catalogue() -> list[dict]:
    """The rule set as the page lists it: what was looked for."""
    return [{"rule": r.id, "topic": r.topic, "favours": r.favours, "reading": r.reading} for r in RULES]
