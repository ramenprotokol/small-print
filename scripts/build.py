"""Build dist/: the static UI, the on-device Python reader and the demo analyses.

    npm install                            (once: fetches the pinned Pyodide runtime)
    uv run python scripts/build.py        (or: npm run build)

Steps:
1. Run each demo document and its hand-written analysis through the same
   Python pipeline the Worker uses (normalise, segment, verify) and write the
   result in the exact /api/analyze response shape. The build fails if any
   hand-written quote does not verify, or a clause has no reading.
2. The on-device reader: copy the Pyodide runtime from node_modules/pyodide
   (unmodified) into a content-hashed folder, and zip the Python modules the
   browser runs (local.py and what it imports, never the model code) into a
   content-hashed bundle. The worker learns both names, and the page learns
   the runtime's size, at build time.
3. Copy web/ into dist/, giving JS and CSS content-hashed file names so they
   can be cached for a long time safely (index.html, the demo JSON and
   other unhashed files get no long cache).
4. Point the page at the API: same origin by default, or the origin in
   SMALL_PRINT_API_ORIGIN (for a Pages front end talking to the Worker on
   another origin). The CSP's connect-src follows.
5. Write THIRD-PARTY-NOTICES.txt for everything third-party in dist/ (the
   Pyodide runtime and what is compiled into it), from licenses/.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from small_print.normalise import normalise, text_sha256  # noqa: E402
from small_print.rules import RULES  # noqa: E402
from small_print.segment import clauses, segment  # noqa: E402
from small_print.service import response_payload  # noqa: E402
from small_print.verify import verify  # noqa: E402

WEB = ROOT / "web"
DEMO = ROOT / "demo"
DIST = ROOT / "dist"
SRC = ROOT / "src" / "small_print"
LICENSES = ROOT / "licenses"
PYODIDE = ROOT / "node_modules" / "pyodide"
# Each file is hashed after the files it names: the worker before local.js
# (which starts it), lib.js and local.js before app.js (which imports them).
HASHED = ("rules-worker.js", "local.js", "lib.js", "app.js", "style.css")
# What Pyodide loads at start-up: its two modules, the WebAssembly binary,
# the standard library and the package lock file.
RUNTIME_FILES = ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")
# The Python the browser runs: local.py and everything it imports. Not
# service.py, model.py or prompt.py (the Worker's model path).
BROWSER_MODULES = ("__init__.py", "normalise.py", "segment.py", "verify.py", "rules.py", "local.py")
ZIP_TIME = (2026, 1, 1, 0, 0, 0)  # fixed, so the bundle's hash depends only on its contents


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


def _swap(body: str, old: str, new: str, what: str) -> str:
    if body.count(old) != 1:
        raise BuildError(f"{what}: expected exactly one {old!r}")
    return body.replace(old, new)


def build_runtime(assets: Path) -> dict:
    """Copy the Pyodide runtime and write the Python bundle, both under
    content-hashed names. Returns their names and sizes."""
    if not (PYODIDE / "package.json").is_file():
        raise BuildError("node_modules/pyodide is missing: run `npm install` first")
    version = json.loads((PYODIDE / "package.json").read_text(encoding="utf-8"))["version"]
    python = json.loads((PYODIDE / "pyodide-lock.json").read_text(encoding="utf-8"))["info"]["python"]
    digest = hashlib.sha256()
    for name in RUNTIME_FILES:
        digest.update(name.encode() + b"\0" + (PYODIDE / name).read_bytes())
    folder = f"pyodide-{digest.hexdigest()[:10]}"
    (assets / folder).mkdir()
    sizes = {}
    for name in RUNTIME_FILES:
        shutil.copyfile(PYODIDE / name, assets / folder / name)
        sizes[name] = (assets / folder / name).stat().st_size

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in BROWSER_MODULES:
            info = zipfile.ZipInfo(f"small_print/{name}", date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, (SRC / name).read_bytes())
    data = buf.getvalue()
    bundle = f"small_print.{hashlib.sha256(data).hexdigest()[:10]}.zip"
    (assets / bundle).write_bytes(data)
    total = sum(sizes.values()) + len(data)
    if max(sizes.values()) > 25 * 1024 * 1024:
        raise BuildError("a runtime file is over the 25 MiB per-file limit of Cloudflare Pages")
    return {"folder": folder, "bundle": bundle, "sizes": sizes, "bytes": total, "pyodide": version, "python": python}


def worker_source(body: str, runtime: dict) -> str:
    body = _swap(body, 'const RUNTIME = "./pyodide/";', f'const RUNTIME = "./{runtime["folder"]}/";', "rules-worker.js")
    body = _swap(body, 'const BUNDLE = "./small_print.zip";', f'const BUNDLE = "./{runtime["bundle"]}";', "rules-worker.js")
    sizes = {k: runtime["sizes"][k] for k in ("pyodide.mjs", "pyodide.asm.mjs")}
    return _swap(body, 'const SIZES = { "pyodide.mjs": 0, "pyodide.asm.mjs": 0 };', f"const SIZES = {json.dumps(sizes)};", "rules-worker.js")


def build_web(origin: str, runtime: dict) -> None:
    assets = DIST / "assets"
    names: dict[str, str] = {}
    for name in HASHED:
        body = (WEB / name).read_text(encoding="utf-8")
        if name == "rules-worker.js":
            body = worker_source(body, runtime)
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
    facts = f'bytes={runtime["bytes"]} rules={len(RULES)} pyodide={runtime["pyodide"]} python={runtime["python"]}'
    html = _swap(html, '<meta name="sp-runtime" content="">', f'<meta name="sp-runtime" content="{facts}">', "index.html")
    if re.search(r'(href|src)="(app\.js|lib\.js|style\.css)"', html):
        raise BuildError("index.html still references an unhashed asset")
    (DIST / "index.html").write_text(html, encoding="utf-8")

    for extra in ("favicon.svg", "robots.txt"):
        shutil.copy2(WEB / extra, DIST / extra)

    headers = (WEB / "_headers").read_text(encoding="utf-8")
    connect = "'self'" + (f" {origin}" if origin else "")
    (DIST / "_headers").write_text(headers.replace("__CONNECT_SRC__", connect), encoding="utf-8")


NOTICE_HEAD = """THIRD-PARTY NOTICES: small print (the built site)

small print's own code is MIT-licensed (LICENSE in the source repository).
Everything third-party in this site is the Pyodide runtime, served from
assets/{folder}/ and copied unmodified from the npm package "pyodide"
{pyodide}: pyodide.mjs, pyodide.asm.mjs, pyodide.asm.wasm,
python_stdlib.zip and pyodide-lock.json. It contains the software listed
below. Fonts (Libre Caslon Text, Public Sans; SIL Open Font License 1.1) are
loaded from Google Fonts when the page opens, not shipped here.

Components
----------

1. Pyodide {pyodide}
   Licence: Mozilla Public License 2.0 (full text below).
   Copyright: the Pyodide contributors and Mozilla.
   Source Code Form: https://github.com/pyodide/pyodide/tree/{pyodide}
   (npm: https://www.npmjs.com/package/pyodide/v/{pyodide}). Unmodified.

2. CPython {python}, compiled into pyodide.asm.wasm, with its standard
   library in python_stdlib.zip.
   Licence: Python Software Foundation License Version 2 and the historical
   licences in CPython's LICENSE file (full text below).
   Copyright (c) 2001 Python Software Foundation; All Rights Reserved.
   Source: https://www.python.org/downloads/source/ (Python-{python}.tar.xz).
   CPython incorporates software under other licences; their notices follow
   in "Licenses and Acknowledgements for Incorporated Software" (from the
   Python 3.14 documentation), among them expat, libffi, zlib, libmpdec,
   mimalloc, SipHash24, dtoa and the Unicode Character Database.

3. HACL* (the hash functions behind CPython's hashlib, vendored in
   CPython's Modules/_hacl).
   Licence: MIT (full text below). Copyright (c) 2016-2022 INRIA, CMU and
   Microsoft Corporation; Copyright (c) 2022-2023 HACL* Contributors.
   Source: https://github.com/hacl-star/hacl-star

4. Emscripten (the JavaScript runtime in pyodide.asm.mjs and the system
   libraries compiled into pyodide.asm.wasm).
   Licence: MIT or University of Illinois/NCSA Open Source License (full
   text below). Copyright (c) 2010-2014 Emscripten authors.
   Source: https://github.com/emscripten-core/emscripten
   Emscripten links parts of LLVM's runtime libraries (compiler-rt, and
   libc++/libc++abi where C++ is used) under Apache-2.0 WITH LLVM-exception;
   the exception waives the notice conditions for such embedded object code.

5. musl libc (Emscripten's C library).
   Licence: MIT (full text below). Copyright (c) 2005-2020 Rich Felker, et al.
   Source: https://musl.libc.org/

6. MiniLZ4 in Emscripten's runtime, based on node-lz4.
   Licence: MIT (full text below). Copyright (c) 2012 Pierre Curto.
   Source: https://github.com/pierrec/node-lz4

7. Zstandard 1.5.7 (behind the compression.zstd module).
   Licence: BSD (full text below; Zstandard is dual-licensed BSD or GPLv2,
   and is used here under BSD). Copyright (c) Meta Platforms, Inc. and
   affiliates. Source: https://github.com/facebook/zstd

8. bzip2 / libbzip2 (behind the bz2 module), as built into Pyodide {pyodide}.
   Licence: bzip2 licence (full text below). Copyright (C) 1996-2019
   Julian R Seward. Source: https://sourceware.org/bzip2/

9. SQLite 3.39.0 (behind the sqlite3 module).
   Public domain: "All of the code and documentation in SQLite has been
   dedicated to the public domain by the authors."
   Source: https://sqlite.org/copyright.html

10. XZ Utils liblzma (behind the lzma module), as built into Pyodide {pyodide}.
    Public domain before XZ Utils 5.6.0, BSD Zero Clause License from 5.6.0;
    neither requires a notice. Source: https://tukaani.org/xz/

Full licence texts follow.
"""

NOTICE_TEXTS = (
    ("Mozilla Public License 2.0 (Pyodide)", "MPL-2.0.txt"),
    ("CPython {python}: LICENSE", "CPython-3.14.2-LICENSE.txt"),
    ("CPython: Licenses and Acknowledgements for Incorporated Software (Python 3.14 documentation)", "CPython-3.14-incorporated-software.txt"),
    ("HACL*", "HACL-star-LICENSE.txt"),
    ("Emscripten", "Emscripten-LICENSE.txt"),
    ("musl libc", "musl-COPYRIGHT.txt"),
    ("MiniLZ4 / node-lz4", "node-lz4-LICENSE.txt"),
    ("Zstandard", "zstd-LICENSE.txt"),
    ("bzip2", "bzip2-LICENSE.txt"),
)


def write_notices(runtime: dict) -> None:
    if not runtime["python"].startswith("3.14."):
        raise BuildError(f"licenses/ holds the CPython 3.14 texts, but Pyodide ships Python {runtime['python']}")
    parts = [NOTICE_HEAD.format(**runtime)]
    rule = "=" * 78
    for title, name in NOTICE_TEXTS:
        body = (LICENSES / name).read_text(encoding="utf-8").strip("\n")
        parts.append(f"{rule}\n{title.format(**runtime)}\n{rule}\n\n{body}\n")
    (DIST / "THIRD-PARTY-NOTICES.txt").write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir()
    try:
        origin = api_origin()
        print("building dist/")
        build_demos()
        (DIST / "assets").mkdir()
        runtime = build_runtime(DIST / "assets")
        print(f"  runtime: Pyodide {runtime['pyodide']} (Python {runtime['python']}), {runtime['bytes']:,} bytes; "
              f"{len(RULES)} rules in {runtime['bundle']}")
        build_web(origin, runtime)
        write_notices(runtime)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    files = sorted(p.relative_to(DIST).as_posix() for p in DIST.rglob("*") if p.is_file())
    print(f"dist/ ready: {len(files)} files; API: {origin or 'same origin'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
