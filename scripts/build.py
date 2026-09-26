"""Build dist/: the static UI plus the precomputed demo analyses.

    uv run python scripts/build.py        (or: npm run build)

Steps:
1. Run each demo document and its hand-written analysis through the same
   Python pipeline the Worker uses (normalise, segment, verify) and write the
   result in the exact /api/analyze response shape. The build fails if any
   hand-written quote does not verify, or a clause has no reading.
2. Copy web/ into dist/, giving JS and CSS content-hashed file names so they
   can be cached for a long time safely (index.html, the demo JSON and
   other unhashed files get no long cache).
3. Point the page at the API: same origin by default, or the origin in
   SMALL_PRINT_API_ORIGIN (for a Pages front end talking to the Worker on
   another origin). The CSP's connect-src follows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from small_print.normalise import normalise, text_sha256  # noqa: E402
from small_print.segment import clauses, segment  # noqa: E402
from small_print.service import response_payload  # noqa: E402
from small_print.verify import verify  # noqa: E402

WEB = ROOT / "web"
DEMO = ROOT / "demo"
DIST = ROOT / "dist"
HASHED = ("lib.js", "app.js", "style.css")  # lib first: app.js imports it


class BuildError(Exception):
    pass


def build_demos() -> list[dict]:
    out_dir = DIST / "demo"
    out_dir.mkdir(parents=True)
    index = []
    for meta in json.loads((DEMO / "index.json").read_text(encoding="utf-8")):
        slug = meta["slug"]
        text = normalise((DEMO / f"{slug}.txt").read_text(encoding="utf-8"))
        segs = segment(text)
        raw = json.loads((DEMO / f"{slug}.analysis.json").read_text(encoding="utf-8"))
        analysis = verify(text, segs, raw)
        ids = {s.id for s in clauses(segs)}
        covered = {r["id"] for r in analysis["readings"]}
        if analysis["dropped"] or analysis["relocated"] or ids != covered:
            raise BuildError(
                f"{slug}: dropped={analysis['dropped_reasons']} relocated={analysis['relocated']} "
                f"missing={sorted(ids - covered)}"
            )
        analysis["model"] = None
        payload = response_payload(text, text_sha256(text), segs, analysis, "demo", None, None, "demo")
        (out_dir / f"{slug}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tally = {k: 0 for k in ("you", "them", "neutral", "unclear")}
        for r in analysis["readings"]:
            tally[r["favours"]] += 1
        index.append({**meta, "clauses": len(ids), "chars": len(text), "tally": tally})
        print(f"  demo {slug}: {len(ids)} clauses, {analysis['verified']} readings verified, 0 dropped")
    (out_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    return index


def api_origin() -> str:
    raw = os.environ.get("SMALL_PRINT_API_ORIGIN", "").strip().rstrip("/")
    if not raw:
        return ""
    u = urlparse(raw)
    if u.scheme != "https" or not u.netloc or u.path or u.query:
        raise BuildError("SMALL_PRINT_API_ORIGIN must be a bare https origin, e.g. https://api.example.dev")
    return f"{u.scheme}://{u.netloc}"


def build_web(origin: str) -> None:
    assets = DIST / "assets"
    assets.mkdir(parents=True)
    names: dict[str, str] = {}
    for name in HASHED:
        body = (WEB / name).read_text(encoding="utf-8")
        for old, new in names.items():
            body = body.replace(f'"./{old}"', f'"./{new}"')
        stem, ext = name.rsplit(".", 1)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:10]
        names[name] = f"{stem}.{digest}.{ext}"
        (assets / names[name]).write_text(body, encoding="utf-8")

    html = (WEB / "index.html").read_text(encoding="utf-8")
    for name, hashed in names.items():
        html = html.replace(f'"{name}"', f'"assets/{hashed}"')
    html = html.replace('<meta name="sp-api" content="">', f'<meta name="sp-api" content="{origin}">')
    if re.search(r'(href|src)="(app\.js|lib\.js|style\.css)"', html):
        raise BuildError("index.html still references an unhashed asset")
    (DIST / "index.html").write_text(html, encoding="utf-8")

    for extra in ("favicon.svg", "robots.txt"):
        shutil.copy2(WEB / extra, DIST / extra)

    headers = (WEB / "_headers").read_text(encoding="utf-8")
    connect = "'self'" + (f" {origin}" if origin else "")
    (DIST / "_headers").write_text(headers.replace("__CONNECT_SRC__", connect), encoding="utf-8")


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    try:
        origin = api_origin()
        print("building dist/")
        build_demos()
        build_web(origin)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    files = sorted(p.relative_to(DIST).as_posix() for p in DIST.rglob("*") if p.is_file())
    print(f"dist/ ready: {len(files)} files; API: {origin or 'same origin'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
