"""Compare the rule reader's labels with the hand-written exhibit readings.

    uv run python scripts/compare_rules.py

The three exhibits carry a hand-written label for every clause. This prints,
for each hand-written label, what the rules said. It is a sanity check, not
an accuracy benchmark: the rules were written with these exhibits in view,
so real terms will do worse.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from small_print.normalise import normalise  # noqa: E402
from small_print.rules import read  # noqa: E402
from small_print.segment import clauses, segment  # noqa: E402

LABELS = ("them", "you", "neutral", "unclear")


def main() -> None:
    table: Counter = Counter()
    for meta in json.loads((ROOT / "demo" / "index.json").read_text(encoding="utf-8")):
        slug = meta["slug"]
        text = normalise((ROOT / "demo" / f"{slug}.txt").read_text(encoding="utf-8"))
        segs = segment(text)
        hand = {c["id"]: c["favours"] for c in json.loads((ROOT / "demo" / f"{slug}.analysis.json").read_text(encoding="utf-8"))["clauses"]}
        rules = {r["id"]: r["favours"] for r in read(text, segs)["readings"]}
        for s in clauses(segs):
            table[(hand[s.id], rules.get(s.id, "none"))] += 1
    cols = (*LABELS, "none")
    print(f"{'hand-written':<14}" + "".join(f"{'rules: ' + c:>15}" for c in cols) + f"{'total':>8}")
    for h in LABELS:
        row = [table[(h, c)] for c in cols]
        print(f"{h:<14}" + "".join(f"{n:>15}" for n in row) + f"{sum(row):>8}")
    total = sum(table.values())
    same = sum(table[(h, h)] for h in LABELS)
    print(f"\n{total} clauses; the rules gave the hand-written label to {same}, another label to "
          f"{sum(v for (h, c), v in table.items() if c not in (h, 'none'))}, and no label to {sum(table[(h, 'none')] for h in LABELS)}.")


if __name__ == "__main__":
    main()
