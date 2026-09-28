# small print

Paste a Terms of Service; get a clause-by-clause map of who each clause favours, with the exact quote.

**Live:** https://small-print.pages.dev

![A synthetic terms of service read by the rule set: clauses washed pink or green, the matched words highlighted, and margin notes naming each rule ("They can close your account", "Liability cap", "You cover their costs") with who it favours](docs/screenshot.png)

## The 30-second experience

1. Paste a Terms of Service (plain text, or HTML source) into the page, or open one of three fictional **exhibits**.
2. The page reads it **on your device**: a Python rule set runs in your browser, so the text is never uploaded. The first reading downloads the Python runtime (13.6 MB, stated on the page before anything downloads, with a progress bar); later readings reuse it.
3. The document comes back as one long strip, each clause washed in a highlighter colour: **favours you** (green), **favours them** (pink), **neutral** (blue-grey), **unclear** (yellow). The words each rule matched are highlighted like a highlighter on a printout, and the margin names the rule ("Forced arbitration", "Licence to your content") with a one-line plain reading. A clause that matches several rules gets each quote highlighted and an "Also:" line.
4. Above the text, "What the rules found" lists every kind of clause found (click one to step through its clauses), and "Looked for, not found" lists the concerns the rules did not see. A "who it favours" strip down the right edge maps the whole document; `j` / `k` move clause to clause.

Every quote is checked. The words a rule matched go through the same verifier as a model's quotes: an exact substring of your text (no fuzzy matching), wholly inside the clause it labels, and long enough to be evidence. The page says how many quotes were checked and kept, and how many clauses matched no rule.

The page labels this honestly as **"Rule-based reading, no AI"** and says what rules can miss. **This is not legal advice.** It is a reading aid; the page says so above every result.

## Two readers

- **Rules, on your device (this site).** A fixed library of 53 hand-written rules in Python, run in the browser by [Pyodide](https://pyodide.org/) (CPython compiled to WebAssembly). Free, no API key, no server, nothing uploaded.
- **Claude, through a Python Worker (optional).** The repository also holds a Python Worker that sends the text to Claude Opus 5.5 and verifies its quotes. It runs only when whoever deploys it sets their own `ANTHROPIC_API_KEY`; the public site has no key, so it does not use it. When a Worker with a key answers `/api/status`, the same page switches to it. See [The AI reader](#the-ai-reader-optional-needs-a-key).

The three exhibits are fictional companies (Nimbus Locker, Pacewren, Brothbike) with terms written for this repo. Their readings (128 of them) were written by hand in the exact JSON shape the model returns, and at build time they pass through the same Python verifier (`scripts/build.py` fails if any quote does not verify).

## Why Python

The core of the job is text work: normalising pasted text, splitting it into clauses without breaking on "e.g.", "Inc." or "Section 4.2", matching legal phrasing, and checking every quote against the source. Python is good at that, and the same Python runs in two places: in the visitor's browser (Pyodide in a Web Worker) and in a Cloudflare Python Worker (Pyodide on workerd). On the live site, Python does all of the reading:

- `src/small_print/rules.py`: the rule reader. 53 rules, 136 bounded regular expressions, plus the checks that keep near misses out (below).
- `src/small_print/local.py`: the browser's entry point. It returns the `/api/analyze` response shape with `analysis.source = "rules"`, so the page renders a rule reading exactly like a model reading.
- `src/small_print/normalise.py`: canonical text and the SHA-256 fingerprint.
- `src/small_print/segment.py`: the clause segmenter. It handles headings, abbreviations, list numbers, inline "(a) … ; (b) …" lists, and short and very long sentences.
- `src/small_print/verify.py`: quote verification. It uses exact substring checks bounded by the clause, relocates a quote that sits wholly in a different clause than the one named, and counts every drop with a reason (`not_found`, `crosses_clause`, `ambiguous`, `too_short`, `duplicate`, `malformed`).

The Worker adds `service.py` (the HTTP handlers), `budget.py` (the daily budget ledger), and `model.py` and `prompt.py` (the streamed model call through the official `anthropic` SDK). The browser bundle never includes those: a test checks that `local.py` imports only the six modules the build zips.

## The rule reader

**What it looks for.** Forced arbitration, class-action and jury-trial waivers, giving up claims, short deadlines to claim, their choice of court, changes to the terms (and acceptance by carrying on), account closure and service changes at their discretion, removal of your content, assignment, auto-renewal, price rises, no refunds, extra charges, expiring credit, liability caps and exclusions, "as is" disclaimers, indemnity, licences over your content (and licences that outlive your account), selling and sharing personal data, AI training on your content, tracking and targeted ads, marketing consent, governing law and age limits. It also looks for the clauses that protect you: opt-outs, refunds, advance notice, your own courts, ownership of your content, data export and deletion, promises not to sell your data or train AI on it, rights the law keeps, and warranties. Each rule says who its kind of clause favours and carries a one-line plain reading.

**How a rule decides.** Each rule is a list of patterns tried against every clause. The gaps between words are lazy and capped (`[^.;!?\n]{0,N}?`), so a pattern never runs past a sentence end or into the next clause, and the work per clause is bounded. On top of the patterns:

- a negation just before the matched words, or inside them, cancels the match for most rules ("we do **not** sell your data", "we will **not** change these terms"); the opposite rule usually catches the clause instead ("They do not sell your data");
- some rules have a clause-level veto for known near misses ("nothing here prevents you from joining a class action" is not a class-action waiver, and sharing data only with service providers is not sharing it with advertisers);
- a match whose subject is the reader is dropped where that flips the meaning ("*you* may terminate for any reason");
- the company's own name counts as "we": a party defined next to a company suffix or alongside "we"/"us" ("Nimbus Locker Ltd. ("Nimbus", "we", "us")") is read as the company;
- "match" on the page (strong, fair or weak) says how specific the wording was, for example *binding* arbitration or a *perpetual, irrevocable* licence. It is not a measure of how sure anyone is about the law.

A clause keeps up to four findings. The strongest one (then the most important rule) is its label; the others appear as "Also:" with their own highlighted quotes.

**Tests.** `tests/py/test_rules.py` has a true positive for every one of the 53 rules (a test enforces it), 32 near misses (negations, the reader as subject, carve-outs, "at least 3 years" that is not an age limit), quote checks over every exhibit and a synthetic terms document, and bounds on a maximum-size hostile paste. Every sentence in those tests was written for them; no real company's terms are used anywhere.

**How it compares with the hand-written exhibit readings.** `uv run python scripts/compare_rules.py` prints this (measured on the build machine):

| Hand-written label | Rules: them | Rules: you | Rules: neutral | Rules: unclear | No rule matched | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| them | 34 | 1 | 0 | 1 | 5 | 41 |
| you | 1 | 24 | 0 | 0 | 5 | 30 |
| neutral | 3 | 0 | 6 | 0 | 41 | 50 |
| unclear | 2 | 0 | 1 | 1 | 3 | 7 |

Of 128 clauses, the rules gave the hand-written label to 65, a different label to 9 and no label to 54 (mostly neutral clauses, which the rules mostly leave alone on purpose). This is a sanity check, not a benchmark: the rules were written with these exhibits in view, so real terms will do worse.

The same script runs a spot check the rules were not written alongside: 24 sentences for a fictional photo-printing service (`tests/fixtures/spot-check.json`), each with its expected label decided before the rules read it. The first run gave the expected label to 11, no label to 12, and a wrong label to 1: "We may raise storage prices with 60 days' notice" came out as favouring you, because the advance-notice rule outranked the clause it softens. That ranking flaw was fixed, so the script now reports 12 expected, 12 with no label and 0 wrong. No phrasing was tuned on these sentences. The honest summary: when a rule fires, its label is usually right, but on text it has not seen it stays silent on about half of the clauses that matter.

**Where it runs.** `scripts/build.py` copies the Pyodide runtime (npm `pyodide` 314.0.7, which is CPython 3.14.2) unmodified into `dist/assets/pyodide-<hash>/`, and zips the six Python modules into `dist/assets/small_print.<hash>.zip`. The page starts a module Web Worker (`web/rules-worker.js`) only when you ask for a reading. The worker counts the bytes as they arrive for the progress bar, then calls `local.analyze_json`. The page stops a worker whose download makes no progress for 60 s, or whose reading takes more than 20 s, so a hostile paste cannot hang the tab; the next reading starts a fresh one. The provenance line shows the Python and Pyodide versions and the time each reading took, measured in Python on your device.

Measured with Pyodide under Node on the build machine (`tests/js/pyodide.test.mjs`, diagnostics only): the Quillfeather test terms (30 clauses) took about 30 ms, a maximum-size 60,000-character document about 190 ms, and a 60,000-character hostile paste of repeated trigger words about 190 ms. Your device may be slower. The same test checks that Pyodide and CPython return identical results, offsets included, for every input.

**What rules can miss.** Rules find set phrases, and only in English: text in another language is split into clauses but matches no rule, so it comes back with nothing marked (the page says the rules read English only). They miss clauses worded in ways they do not know, can misread words used in another sense, cannot weigh how much a clause matters or how it interacts with the rest, and say nothing about a clause that matches no rule. The page says so next to every rule reading, and "Looked for, not found" says that not finding a phrase is not proof the terms lack it.

## The AI reader (optional, needs a key)

This path is unchanged, and runs only on a deployment whose owner sets `ANTHROPIC_API_KEY`. `src/entry.py` is thin glue to the Workers runtime.

The official Anthropic Python SDK runs unmodified inside the Worker. Cloudflare's Pyodide build ships `httpx2_jsfetch`, which routes the SDK's HTTP client through the Workers `fetch`. One adapter was needed: that bridge hands over streamed response bodies as `memoryview` chunks, and the SDK's event-stream decoder needs `bytes`, so `model.py` wraps the transport to convert them (found in the end-to-end run; a unit test reproduces it). This was checked in `wrangler dev`, where the SDK's real streamed request went to a local mock server. So there is no hand-rolled HTTP client.

```
browser                                    Python Worker (/api/*)                  model
-------                                    ----------------------                  -----
paste -> strip HTML (DOMParser, inert)
      -> split into parts <= 10,000 chars
         (the Worker enforces the same cap)
      -> POST /api/analyze  (one per part) -> validate, cap 60,000 chars
                                           -> normalise, SHA-256
                                           -> segment into clauses (Python)
                                           -> D1 cache hit? return it (free)
                                           -> Durable Object: take 1 from
                                              today's budget (atomic)
                                           -> Claude Opus 5.5, effort "medium",  -> JSON: id, quote,
                                              streamed, structured output            favours, reading,
                                              (JSON schema), no SDK retries          confidence
                                           -> verify every quote verbatim,
                                              inside its own clause
                                           -> cache offsets + readings in D1
                                              (if >= 80% of clauses verified)
render strip, highlights, margin notes  <- text, clauses, verified readings,
(a failed part keeps its place, with a        the model that read the part
 button to read it again)
```

The browser strips HTML and splits long documents into parts, which keeps the Worker's CPU work small. While the model thinks, the Worker is only waiting on I/O.

**The model request** (`src/small_print/prompt.py`) follows the Claude Opus 5.5 rules:

- model `claude-opus-5-5`, with `output_config.effort` set explicitly to `"medium"`;
- thinking is never disabled and no `budget_tokens` is sent (adaptive thinking, the model's default);
- no forced `tool_choice`: the clause list comes from structured outputs (`output_config.format` with a strict JSON schema);
- a cached system prompt that tells the model to treat the document as data, not instructions; a pasted `</document>` (or any `<document` tag) is defused before it reaches the prompt, so it cannot close the document early;
- server-side refusal fallbacks (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`). If Opus 5.5 declines, another Claude model may answer, and the page shows which model read each part. Set `ANTHROPIC_FALLBACKS = "off"` to disable them;
- streamed with `messages.stream()` and read to the final message, with the SDK's automatic retries off. The whole call may run for at most 60 s plus `max_tokens` at an assumed floor of 50 output tokens a second (380 s), and the stream may not go silent for more than 60 s. The page waits longer than that (its timeout comes from `/api/status`), so it never gives up on a call the Worker is still waiting on.

**Which failures give the budget unit back.** A request that fails before generation could start (the connection could not be made, or the API answered with a 4xx or 5xx instead of opening the stream) is not billed, so its unit is refunded. Anything after that, a timeout, a stream that breaks off, an error event mid-stream, may already have been billed, so the unit stays spent and the page says so. Nothing is retried automatically: a retried timeout could be billed twice on one unit. A failed part can be read again from the page, and that uses a new unit.

**Demo mode.** A Worker with no `ANTHROPIC_API_KEY` secret answers `/api/status` with `"mode": "demo"` and gives pasted text a clause map without readings. The page does not use that any more: with no key, it reads pasted text on the device with the rule set. Cached analyses and the exhibits still open.

## Build, run and test

Toolchain: Node 20+ (for wrangler, Pyodide and the page tests) and [uv](https://docs.astral.sh/uv/) (it fetches Python and the Pyodide toolchain for the Worker). Chrome or Chromium is needed for the browser tests; they skip cleanly without it (`CHROME_PATH` points at a specific binary).

```sh
npm install           # wrangler and pyodide, both pinned
npm run build         # -> dist/ (static UI, Pyodide runtime, Python bundle, exhibits, notices), via scripts/build.py
npm test              # pytest (299 tests) + build + node --test (33 tests, incl. headless Chrome and Pyodide)
npm run test:e2e      # the real Worker in local workerd, demo mode and a mocked model
npm run dev           # build, apply D1 migrations locally, pywrangler dev on :8787 (demo mode)
uv run python scripts/compare_rules.py   # rule labels vs the exhibit readings and a spot check
```

`npm test` includes:

- `tests/js/rules.test.mjs`: the built `dist/` in headless Chrome, served locally with its `_headers` applied (so the strict CSP is enforced) and the runtime streamed slowly. It pastes the synthetic terms in `tests/fixtures/`, watches the progress bar, and checks the verified quotes, the notes, a clause with two findings, the checklist jump, a second reading with no second download, bad input, pasted HTML that must never run, that every request was a GET for the site's own files (nothing uploaded), no console errors, and a 400 px dark phone layout with no horizontal scroll.
- `tests/js/pyodide.test.mjs`: loads the built Python bundle into Pyodide under Node and checks that it returns exactly what CPython returns for the exhibits, the synthetic terms, awkward Unicode and a maximum-size hostile paste.
- `tests/js/smoke.test.mjs`: the files, hashed names, CSP, the stated runtime size against the real file sizes, the bundle's contents, the third-party notices, and the static page with no API at all.

`npm run test:e2e` starts `pywrangler dev` twice, each time on a random free port.

- **Demo mode (no key):** it checks the API's clause map without readings, the exhibits, `j`/`k` (and the strip's tick), pasting HTML (it must never execute, and with no key it is read on the device: the page makes no `/api/analyze` request), bad input (too long, invisible characters only, headings only) with no console errors, an empty body (400), and a chunked upload over the size cap (413, refused while it is read). The browser checks run at 1280 px and at a true 400 px (DevTools device emulation), with no horizontal scroll.
- **Live mode against a local mock of the Messages API:** the SDK inside the Worker streams its real request to `127.0.0.1`, and the mock answers with server-sent events. The test checks:
  - the request shape: streamed, model, effort, schema, fallbacks and headers;
  - that the mock's one deliberately wrong quote is dropped and reported;
  - that the D1 cache serves repeats with no model call;
  - that a 5xx before the stream opens is refunded, a stream cut off mid-reply is counted, and neither is retried;
  - a two-part paste where one part's request fails: the other part is shown, and "Read part 2 again" reads it, through a mocked refusal fallback, so the page names Claude Opus 5 for that part;
  - that the Durable Object budget stops at the limit while cached documents still open;
  - that the Worker's logs hold counts and the failure kind only, never the key or document text.

No test ever calls the real API.

## Running on Cloudflare's free plan

**The live site is static.** With the rule reader, Cloudflare Pages serves only files: no Worker, no storage, no API calls, nothing that bills.

| Piece | Size | Free-plan limit |
| --- | --- | --- |
| Page, exhibits, fonts, notices | about 25 files | 20,000 files per site; unlimited static requests |
| Pyodide runtime (`assets/pyodide-<hash>/`) | 13.5 MB in 5 files; the largest, `pyodide.asm.wasm`, is 9.6 MB | 25 MiB per file |
| Python bundle (`assets/small_print.<hash>.zip`) | about 24 kB | |

Everything under `/assets/` has a content-hashed name (the runtime folder is named after a hash of its files), so it is served with a one-year immutable cache; `index.html`, the exhibits and the notices are not cached long. The byte counts on the page are the files' uncompressed sizes; what actually crosses the network may be smaller if it is sent compressed.

**With a Worker (optional, for the AI reader):**

| Piece | What it uses | Free-plan limit |
| --- | --- | --- |
| UI (`dist/`) | Static assets on Pages, or the Worker's assets binding. Requests outside `/api/*` never wake the Worker. | Unlimited static requests |
| Worker | One request per document part (up to 10,000 characters; the Worker refuses bigger parts), plus one status check per page view | 100,000 requests/day; 10 ms CPU per request |
| Durable Object `BudgetCounter` | One call per API request; about 2 rows written per model call | SQLite-backed only on Free: 100,000 requests/day, 100,000 rows written/day |
| D1 `small-print-cache` | 1 row read per analysis request, 1 row written per new analysis | 5M rows read, 100,000 rows written per day |

**CPU.** Most of a request's wall time is waiting for the model, which is I/O and does not count as CPU. The Python work per part was measured on CPython on the build machine: normalising and segmenting a 58,000-character document took about 2 ms and 1.5 ms, and verifying 46 readings took under 0.1 ms. Pyodide in the Worker is slower than native CPython, and streaming adds work: the SDK parses and accumulates every stream event in Python. CPU time in production has **not** been measured (it needs a deployed Worker), so whether a call stays inside the free plan's 10 ms is not proven.

**The AI budget.** `DAILY_BUDGET` (default 40) caps model calls per UTC day across all visitors, and `PER_VISITOR_DAILY` (default 8) caps them per visitor. The counter lives in one SQLite-backed Durable Object, so increments are atomic. Every request that might be billed counts: only failures that clearly happened before generation are refunded (see above), so timed-out or abandoned calls cannot slip past the cap. The visitor key is an HMAC of the IP under a random salt that is created each day and deleted with that day's rows. When the budget is spent, the page says so and offers cached documents and the exhibits.

**What it can cost the owner.** The budget caps the number of calls and `max_tokens` caps each call, so there is a hard ceiling:

```
daily ceiling = DAILY_BUDGET x (max_tokens x output price + max input tokens x input price)
```

- Output: `max_tokens` is 16,000 (`prompt.py`). At $20 per million output tokens for `claude-opus-5-5`, that is $0.32 a call.
- Input: a part is at most 10,000 characters. With the system prompt and the clause numbers, that is under about 11,000 tokens even at one token per character. At $4 per million input tokens, that is about $0.044.
- So one call costs at most about $0.36, and the default 40 calls a day at most about **$14.60 a day**.

A typical call is much smaller. A rough, unmeasured estimate: about 2,500 input tokens and 6,000 output tokens (thinking included), roughly $0.13 a call, so around **$5 a day** if all 40 calls are used. That is a typical estimate, not a ceiling. Real token counts will differ.

**Fallbacks bill at the fallback model's own rates.** If Opus 5.5 declines and the API reruns the request on a fallback model (for example Claude Opus 5 or Claude Opus 4.8, at $5 / $25 per million tokens), that call's ceiling is 16,000 x $25 + 11,000 x $5 per million, about $0.46. If the decline comes mid-reply, the partial Opus 5.5 output is billed too, so one such call could reach about $0.82. If every call fell back mid-reply, the day's ceiling would be about $33. Set `ANTHROPIC_FALLBACKS = "off"` for a strict $14.60 ceiling. Prices are the list prices in Anthropic's model table as of September 2026; check current pricing before relying on these numbers.

## Deploying (owner only)

The owner runs every Cloudflare command through a guarded deploy script kept outside this repository, which refuses to run unless the project's own Cloudflare account is configured, so the machine's global wrangler login is never used. `scripts/deploy.sh` wraps it: it takes the guard's path from the `RAMEN_DEPLOY_GUARD` environment variable (and refuses to run if that is unset or not executable), and puts the wrangler pinned in `package.json` first on `PATH`.

**The live site (rules, static):** build, then deploy `dist/` to Pages:

```sh
npm install && npm run build
"$RAMEN_DEPLOY_GUARD" pages dist --project-name small-print --branch main
```

(`scripts/deploy.sh pages` does the same build and guarded deploy.) Nothing else is needed: no D1, no Durable Object, no secret.

**The AI reader (optional):**

1. `scripts/deploy.sh setup`, once. Through the guard (`"$RAMEN_DEPLOY_GUARD" run ...`) it creates the D1 database (`d1 create small-print-cache`), then stops so you can put the printed `database_id` in `wrangler.toml`. It then applies the migrations (`d1 migrations apply small-print-cache --remote`) and sets the secret (`secret put ANTHROPIC_API_KEY`). Press Ctrl-C at the secret prompt to stay without a key.
2. `scripts/deploy.sh worker`. This deploys the API and serves the UI from the same origin.
3. Optional: to keep the UI on Pages and talk to that Worker, build with `SMALL_PRINT_API_ORIGIN=https://<worker host>` so the page (and its CSP `connect-src`) points at the Worker, and list the Pages origin in the Worker's `ALLOWED_ORIGINS`.

**For your own copy** (not how this repo's owner deploys): with your own Cloudflare login, the plain equivalents are `npx wrangler pages deploy dist --project-name small-print` for the static site, and for the AI reader `npx wrangler d1 create small-print-cache` (then set `database_id`), `npx wrangler d1 migrations apply small-print-cache --remote`, `npx wrangler secret put ANTHROPIC_API_KEY` and `uv run pywrangler deploy`.

## Privacy and safety

- **Rule readings never leave the device.** The text is read in a Web Worker in your browser tab. The page fetches only its own files (the runtime comes from this site, not a CDN), and a browser test checks that every request during a reading is a GET for those files.
- The theme choice and your last five read documents (text included) are kept in this browser's `localStorage` only, and the page works without them. The page says so next to the list, and a "Forget these documents" button removes them; the browser test checks it.
- **No third-party requests.** The fonts ship with the site too (`web/fonts/`, under the SIL Open Font License, listed in the third-party notices), so opening the page sends nothing, not even an IP address, to a font host. The browser test fails on any request that is not to the site's own origin.
- A strict CSP: `script-src 'self' 'wasm-unsafe-eval'` (WebAssembly may be compiled; JavaScript `eval` may not), `worker-src 'self'`, `style-src 'self'`, `font-src 'self'`, no inline script or style, and no third-party anything.
- Pasted HTML is parsed in an inert `DOMParser` document: scripts never run and resources never load. All rendering uses text nodes, never `innerHTML`.
- The rule reader is bounded: the patterns cannot run past a sentence, and the page stops a worker that stalls or runs long.
- **On a Worker with a key:** document text and API keys are never logged. The Worker logs one line per analysis with counts and the kind of failure, if any (`config`, `network`, `busy`, `upstream`, `timeout`, `interrupted`, `rejected`, `bad_output`), and a test checks this. Request bodies are read chunk by chunk and refused (413) as soon as they pass 512 KB. The cache is content-addressed and stores offsets, labels and readings, never the text; there is no listing endpoint, and only analyses in which at least 80% of the clauses got a verified reading are cached.

## Honest limitations

- **English only.** The rules are English phrases. A French or Japanese terms of service is read without errors but gets no marks at all.
- **Rules are not understanding.** They find set phrases and can be fooled by unusual wording, by words used in another sense, and by clauses whose meaning depends on the rest of the document. A clause with no mark matched no rule; that does not make it harmless. The comparison above was measured on documents the rules were written alongside, so it flatters them.
- The labels are judgement calls, whoever makes them. The verifier proves only that a quote is in the text, not that the label is right.
- The first reading downloads 13.6 MB (uncompressed). On a slow connection that takes a while; the page shows progress and says the size before anything downloads.
- The on-device reader needs WebAssembly and module Web Workers. It was tested in headless Chrome (desktop and 400 px emulation); other browsers were not tested for this change.
- Segmentation is heuristic. Unusual formatting (tables, one giant paragraph, hard-wrapped PDF text that starts lines with capitals) can produce odd clause boundaries. Short inline headings ("1. Acceptance. By using ...") join the sentence after them, and a wrapped line that starts with a lowercase list marker ("a. ", "iv) ") is kept as its own line, which can occasionally keep apart a sentence that was simply wrapped there. Every character of the text is still shown.
- Quotes must match exactly and stay inside one clause. A model that normalises a curly quote or a double space, or quotes across two sentences, loses that reading; it is dropped and counted, not guessed at.
- Wide screens open up space in the text so that each margin note sits level with its clause. Notes are short (offsets sit behind "Show quote offsets"), but lines holding several short clauses still get some extra space.
- The document limit is 60,000 characters. Longer documents need to be pasted a section at a time.
- For the AI reader: a part longer than 10,000 characters or with more than 120 clauses is not sent to the model; the CPU cost in production and the model's real latency and token use are unmeasured, because no paid calls were made while building this. The call's time limit rests on an assumed floor of 50 output tokens a second.

## Next

- More rules, and a page that shows each rule's patterns, so a reader can see exactly why a clause was flagged.
- A held-out set of synthetic terms, written by someone who has not seen the rules, to measure them fairly.
- When a key is set, a choice between the rule reader and the model instead of switching automatically.
- Test the on-device reader in Firefox and Safari.
- **Bring your own key**: an explicit toggle that sends a visitor's own key to the Worker for one request, never stored or logged.
- **URL fetch**: paste a link instead of text, with safeguards against SSRF and oversized pages.
- Measure CPU time and token use on a deployed Worker, then tune the part size and `max_tokens`.
- Cache expiry, and a "compare two versions of the same terms" view.

## Credits

Built by Ramen Protocol ([ramenprotokol](https://github.com/ramenprotokol)) with AI assistance from Claude. The exhibits and the test terms are fictional, and no real company's terms are quoted anywhere in this repository. Licensed MIT.

**Third-party notices.** `dist/` ships the Pyodide runtime, so the build writes `dist/THIRD-PARTY-NOTICES.txt`, linked from the page's colophon. It lists each component with its version, copyright, licence and source, followed by the full licence texts, which the build takes verbatim from `licenses/`:

- **Pyodide** 314.0.7 (MPL 2.0), served unmodified; source at github.com/pyodide/pyodide.
- **CPython** 3.14.2 (PSF License), compiled into the runtime, with the notices for the software CPython incorporates (expat, libffi, zlib, libmpdec, mimalloc and others), and **HACL\*** (MIT), which CPython's hashlib uses.
- **Emscripten**'s runtime (MIT or NCSA), **musl** libc (MIT) and **MiniLZ4** (MIT), which the runtime is built with.
- **Zstandard** 1.5.7 (BSD), **bzip2** (bzip2 licence), **SQLite** 3.39.0 (public domain) and **XZ Utils** liblzma (public domain or 0BSD), which Pyodide links in.
- **Fonts:** Libre Caslon Text (Copyright 2012 The Libre Caslon Text Project Authors) and Public Sans (Copyright 2015 The Public Sans Project Authors), both under the SIL Open Font License 1.1, ship as the Latin-subset WOFF2 files Google Fonts serves, unmodified, from `web/fonts/`; their licence texts are in `licenses/` and in the notices.
- **Worker only:** the Python Worker calls the model through the official Anthropic Python SDK, [`anthropic`](https://github.com/anthropics/anthropic-sdk-python) 1.8.0 (MIT, Copyright 2023 Anthropic, PBC). It runs on the server, vendored into `python_modules/` with its own dependencies, so none of it reaches visitors.
- **Build and test tools** (wrangler, uv, pywrangler, pytest) never ship.
