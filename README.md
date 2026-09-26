# small print

Paste a Terms of Service; get a clause-by-clause map of who each clause favours, with the exact quote.

![A marked-up terms of service: clauses highlighted pink, green, yellow and blue-grey, with margin notes beside each clause](docs/screenshot.png)

## The 30-second experience

1. Paste a Terms of Service (plain text, or HTML source) into the page, or open one of three fictional **exhibits**.
2. The document comes back as one long strip, each clause washed in a highlighter colour: **favours you** (green), **favours them** (pink), **neutral** (blue-grey), **unclear** (yellow).
3. Inside each clause, the exact words that justify the label are marked like a highlighter on a printout. Beside the clause, in the margin: a one-line plain-English reading and a confidence label. "Show quote offsets" (or selecting a clause) adds the quote's character offsets in the text and, for read documents, the model that read it.
4. A "who it favours" strip down the right edge maps the whole document by colour, with a tick at the selected clause; click it to jump. `j` / `k` move clause to clause.

Nothing is summarised without its source clause on screen, and every quote is checked. The Worker looks for each quote in your text, character for character (a plain substring check, no fuzzy matching), and the quote must lie wholly inside the clause it labels. Any reading whose quote is not there, runs into a neighbouring clause, or is too short to be evidence (under 15 characters and under 3 words) is dropped, and the page says how many were dropped and why.

**This is not legal advice.** It is a reading aid; the page says so above every result.

## Why Python

The core of the job is text work: normalising pasted text, splitting it into clauses without breaking on "e.g.", "Inc." or "Section 4.2", and checking model quotes against the source. Python is good at that, and Cloudflare now runs Python Workers (Pyodide on workerd). The Python here does real work:

- `src/small_print/normalise.py`: canonical text and the SHA-256 cache key.
- `src/small_print/segment.py`: the clause segmenter. It handles headings, abbreviations, list numbers, inline "(a) … ; (b) …" lists, and short and very long sentences.
- `src/small_print/verify.py`: quote verification. It uses exact substring checks bounded by the clause, relocates a quote that sits wholly in a different clause than the model named, and counts every drop with a reason (`not_found`, `crosses_clause`, `ambiguous`, `too_short`, `duplicate`, `malformed`).
- `src/small_print/budget.py`: the daily budget ledger (SQL shared by the Durable Object and the tests).
- `src/small_print/service.py`: the HTTP handlers, independent of the runtime.
- `src/small_print/model.py` and `prompt.py`: the streamed model call through the official `anthropic` SDK, and the sorting of failures into "refund" and "count".

`src/entry.py` is thin glue to the Workers runtime.

The official Anthropic Python SDK runs unmodified inside the Worker. Cloudflare's Pyodide build ships `httpx2_jsfetch`, which routes the SDK's HTTP client through the Workers `fetch`. One adapter was needed: that bridge hands over streamed response bodies as `memoryview` chunks, and the SDK's event-stream decoder needs `bytes`, so `model.py` wraps the transport to convert them (found in the end-to-end run; a unit test reproduces it). This was checked in `wrangler dev`, where the SDK's real streamed request went to a local mock server. So there is no hand-rolled HTTP client.

## How it works

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

**Demo mode.** With no `ANTHROPIC_API_KEY` secret set, the app runs in demo mode:

- pasted text still gets the real clause map (segmented in Python), without readings;
- cached analyses still open;
- the three exhibits are fully read.

The exhibits are fictional companies (Nimbus Locker, Pacewren, Brothbike) with terms written for this repo. Their readings (128 of them) were written by hand in the exact JSON shape the model returns, and at build time they pass through the same Python verifier (`scripts/build.py` fails if any quote does not verify). They also work from a static host with no Worker at all.

## Build, run and test

Toolchain: Node 20+ (for wrangler and the page tests) and [uv](https://docs.astral.sh/uv/) (it fetches Python and the Pyodide toolchain). Chrome or Chromium is needed for the browser tests; they skip cleanly without it (`CHROME_PATH` points at a specific binary).

```sh
npm install           # wrangler, pinned
npm run build         # -> dist/ (static UI + precomputed exhibits), via scripts/build.py
npm test              # pytest (164 tests) + build + node --test (23 tests, incl. headless Chrome)
npm run test:e2e      # the real Worker in local workerd, demo mode and a mocked model
npm run dev           # build, apply D1 migrations locally, pywrangler dev on :8787 (demo mode)
```

`npm run test:e2e` starts `pywrangler dev` twice, each time on a random free port.

- **Demo mode (no key):** it checks the clause map without readings, the exhibits, `j`/`k` (and the strip's tick), pasting HTML (it must never execute), bad input (too long, invisible characters only, headings only) with no console errors, an empty body (400), and a chunked upload over the size cap (413, refused while it is read). The browser checks run at 1280 px and at a true 400 px (DevTools device emulation), with no horizontal scroll.
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

Deploy-ready, not deployed. The owner runs every Cloudflare command through `scripts/deploy.sh`, which calls the guarded script `~/RamenProtocol/_ops/infra/ramen-deploy.sh`. That script refuses to run unless the Ramen Cloudflare account is configured, so the machine's global wrangler login is never used. `scripts/deploy.sh` puts the wrangler pinned in `package.json` first on `PATH`.

1. `scripts/deploy.sh setup`, once. Through the guard (`ramen-deploy.sh run ...`) it creates the D1 database (`d1 create small-print-cache`), then stops so you can put the printed `database_id` in `wrangler.toml`. It then applies the migrations (`d1 migrations apply small-print-cache --remote`) and sets the secret (`secret put ANTHROPIC_API_KEY`). Press Ctrl-C at the secret prompt to stay in demo mode.
2. `scripts/deploy.sh worker`. This deploys the API and serves the UI from the same origin.
3. Optional: `scripts/deploy.sh pages` puts the UI on Pages. Build it with `SMALL_PRINT_API_ORIGIN=https://<worker host>` so the page (and its CSP `connect-src`) points at the Worker, and list the Pages origin in the Worker's `ALLOWED_ORIGINS`.

**For your own copy** (not how this repo's owner deploys): with your own Cloudflare login, the plain equivalents are `npx wrangler d1 create small-print-cache` (then set `database_id`), `npx wrangler d1 migrations apply small-print-cache --remote`, `npx wrangler secret put ANTHROPIC_API_KEY`, `uv run pywrangler deploy`, and optionally `npx wrangler pages deploy dist --project-name small-print`.

## Privacy and safety

- Document text and API keys are never logged. The Worker logs one line per analysis with counts and the kind of failure, if any (`config`, `network`, `busy`, `upstream`, `timeout`, `interrupted`, `rejected`, `bad_output`), and a test checks this.
- Request bodies are read chunk by chunk and refused (413) as soon as they pass 512 KB, including chunked uploads that carry no length.
- The cache is content-addressed. It stores offsets, labels and readings, never the text. A row can be found only by someone who already has the exact text, and there is no listing endpoint. Only analyses in which at least 80% of the clauses got a verified reading are cached, so a weak reading is not frozen in for everyone.
- Pasted HTML is parsed in an inert `DOMParser` document: scripts never run and resources never load. All rendering uses text nodes, never `innerHTML`.
- A strict CSP (`script-src 'self'`, no inline script or style) and no third-party scripts. Fonts come from Google Fonts.
- The theme choice and your last five read documents are kept in this browser's `localStorage` only, and the page works without them.

## Honest limitations

- The labels are one model's reading of legal text, at medium effort. They can be wrong. The verifier proves only that a quote is in the text, not that the label is right.
- Segmentation is heuristic. Unusual formatting (tables, one giant paragraph, hard-wrapped PDF text that starts lines with capitals) can produce odd clause boundaries. Short inline headings ("1. Acceptance. By using ...") join the sentence after them, and a wrapped line that starts with a lowercase list marker ("a. ", "iv) ") is kept as its own line, which can occasionally keep apart a sentence that was simply wrapped there. Every character of the text is still shown.
- Quotes must match exactly and stay inside one clause. A model that normalises a curly quote or a double space, or quotes across two sentences, loses that reading; it is dropped and counted, not guessed at.
- Wide screens open up space in the text so that each margin note sits level with its clause. Notes are short (offsets sit behind "Show quote offsets"), but lines holding several short clauses still get some extra space.
- A part longer than 10,000 characters or with more than 120 clauses is not sent to the model; the page splits by characters, so a part dense with very short clauses can get the clause map only.
- The CPU cost in production and the model's real latency and token use are unmeasured, because no paid calls were made while building this. The call's time limit rests on an assumed floor of 50 output tokens a second.
- The document limit is 60,000 characters. Longer documents need to be pasted a section at a time.

## Next

- **Bring your own key**: an explicit toggle that sends a visitor's own key to the Worker for one request, never stored or logged. It was deferred to keep the security surface small.
- **URL fetch**: paste a link instead of text, with safeguards against SSRF and oversized pages.
- **Workers AI fallback** when the daily budget is spent.
- Measure CPU time and token use on a deployed Worker, then tune the part size and `max_tokens`.
- Cache expiry, and a "compare two versions of the same terms" view.
- Split a part again automatically when it has too many clauses for one reading.

## Credits

Built by Ramen Protocol ([ramenprotokol](https://github.com/ramenprotokol)) with AI assistance from Claude. The exhibits are fictional, and no real company's terms are quoted anywhere in this repository. Licensed MIT.
