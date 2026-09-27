// small print: the page. Reads pasted text on this device with the Python
// rule set (local.js, Pyodide in a worker), or through /api (the Python
// Worker and the model) when a server with a key is there, then renders the
// marked-up document, margin notes and the "who it favours" strip.
import {
  FAVOURS,
  MAX_CHARS,
  PART_CHARS,
  REQUESTED_MODEL,
  chunkText,
  combineParts,
  cpLength,
  findingsByRule,
  formatBytes,
  formatInt,
  guessTitle,
  hasReadableText,
  htmlToText,
  looksLikeHtml,
  makeIndexer,
  markRuns,
  modelName,
  plural,
  prepText,
  tally,
} from "./lib.js";
import { canReadLocally, createLocalReader } from "./local.js";

const $ = (sel, root = document) => root.querySelector(sel);
const API = ($('meta[name="sp-api"]')?.content || '').replace(/\/$/, '');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const wideLayout = matchMedia('(min-width: 1100px)');
const LABEL = { you: 'Favours you', them: 'Favours them', neutral: 'Neutral', unclear: 'Unclear', none: 'No reading' };
const labelFor = (doc, fav) => (fav === 'none' && doc?.kind === 'rules' ? 'No rule matched' : LABEL[fav]);
// A rule's "confidence" is how specific the wording it matched is.
const MATCH = { high: 'strong match', medium: 'fair match', low: 'weak match' };
// How long to wait for one part. The Worker reports its own worst case (plus
// a margin) in /api/status; this default is used until then.
const DEFAULT_PART_TIMEOUT_S = 480;
const HISTORY_KEY = 'sp-history-2';
const THEME_KEY = 'sp-theme';
const DROP_LABEL = {
  not_found: 'not in the text word for word',
  ambiguous: 'ambiguous (the words appear more than once)',
  crosses_clause: 'ran past the end of their clause',
  duplicate: 'duplicates',
  malformed: 'malformed',
  too_short: 'too short to count as evidence',
};

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

// The Python runtime the build ships (sizes measured at build time):
// <meta name="sp-runtime" content="bytes=… rules=… pyodide=… python=…">.
const RUNTIME = Object.fromEntries(
  ($('meta[name="sp-runtime"]')?.content || '').split(/\s+/).filter(Boolean).map((kv) => kv.split('=')),
);
const localReader = RUNTIME.bytes && canReadLocally() ? createLocalReader() : null;

// ---------------------------------------------------------------- storage
// Per-viewer conveniences only (theme, recent documents). Any of this may be
// unavailable (private windows, blocked storage); the page works without it.
function store(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(key) ?? 'null');
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    return null;
  }
  return value;
}

// ------------------------------------------------------------------ theme
function initTheme() {
  const root = document.documentElement;
  const saved = store(THEME_KEY);
  if (saved === 'dark' || saved === 'light') root.dataset.theme = saved;
  const btn = $('#lamp');
  const systemDark = matchMedia('(prefers-color-scheme: dark)');
  const isDark = () => root.dataset.theme === 'dark' || (!root.dataset.theme && systemDark.matches);
  const sync = () => {
    btn.setAttribute('aria-pressed', String(isDark()));
    btn.querySelector('.lamp-state').textContent = isDark() ? 'on' : 'off';
  };
  btn.addEventListener('click', () => {
    root.dataset.theme = isDark() ? 'light' : 'dark';
    store(THEME_KEY, root.dataset.theme);
    sync();
  });
  systemDark.addEventListener('change', sync);
  sync();
}

// ---------------------------------------------------------------- service
const service = { state: 'checking', budget: null, timeoutS: DEFAULT_PART_TIMEOUT_S };

async function fetchJSON(url, options = {}, timeout = 10000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const res = await fetch(url, { ...options, signal: ctrl.signal });
    let body = null;
    try {
      body = await res.json();
    } catch {
      body = null;
    }
    return { ok: res.ok, status: res.status, body };
  } finally {
    clearTimeout(timer);
  }
}

async function checkService() {
  try {
    const r = await fetchJSON(`${API}/api/status`);
    if (!r.ok || !r.body) throw new Error('status');
    service.state = r.body.mode === 'live' ? 'live' : 'demo';
    service.budget = r.body.budget;
    service.model = r.body.model;
    service.effort = r.body.effort;
    service.fallbacks = r.body.fallbacks !== false;
    const t = Number(r.body.limits?.timeout_s);
    if (Number.isFinite(t) && t > 0) service.timeoutS = t;
  } catch {
    service.state = 'offline';
  }
  renderServiceStatus();
  updatePlan();
}

function readingsLeft() {
  const b = service.budget;
  if (!b) return 0;
  return Math.min(b.remaining, b.visitor_remaining ?? b.remaining);
}

function renderServiceStatus() {
  const box = $('#service-status');
  box.dataset.state = service.state;
  const lead = el('strong');
  const rest = el('span');
  if (service.state === 'checking') {
    lead.textContent = 'Checking the reading service…';
  } else if (service.state === 'live') {
    lead.textContent = 'AI reading is on.';
    const left = readingsLeft();
    const model = modelName(service.model || REQUESTED_MODEL);
    rest.textContent = ` ${model} at ${service.effort || 'medium'} effort`
      + `${service.fallbacks ? `; if ${model} declines a text, another Claude model may answer` : ''}. `
      + `${plural(left, 'reading')} left for you today `
      + `(${formatInt(service.budget.remaining)} of ${formatInt(service.budget.limit)} across all visitors); resets at 00:00 UTC. `
      + `One reading covers up to ${formatInt(PART_CHARS)} characters.`;
  } else if (localReader) {
    box.dataset.state = 'rules';
    lead.textContent = 'Rule-based reading, no AI.';
    rest.textContent = ` Pasted text is read in this browser by ${formatInt(Number(RUNTIME.rules) || 0)} hand-written rules, `
      + 'in Python (Pyodide: CPython compiled to WebAssembly). Your text never leaves this device. '
      + (localReader.runtime
        ? `Python ${localReader.runtime.python} is loaded.`
        : `The first reading downloads the Python runtime, ${formatBytes(Number(RUNTIME.bytes))}; after that it normally comes from your browser's cache.`);
  } else {
    lead.textContent = 'Pasted text cannot be read in this browser.';
    rest.textContent = ' The on-device reader needs WebAssembly and module workers. The three exhibits below still open.';
  }
  box.replaceChildren(lead, rest);
  // The colophon describes whichever reader this page is using.
  const mode = service.state === 'live' ? 'live' : 'rules';
  document.querySelectorAll('.mode-copy').forEach((n) => { n.hidden = n.dataset.mode !== mode; });
}

const readsLocally = () => service.state !== 'live';

// ----------------------------------------------------------------- intake
function currentInput() {
  let raw = $('#paste').value;
  let converted = false;
  if (looksLikeHtml(raw)) {
    raw = htmlToText(raw);
    converted = true;
  }
  const text = prepText(raw);
  return { text, converted, chars: cpLength(text) };
}

function updatePlan() {
  const plan = $('#plan');
  const { text, converted, chars } = currentInput();
  plan.classList.remove('over');
  if (!chars) {
    plan.textContent = `Plain text or HTML source, up to ${formatInt(MAX_CHARS)} characters.`;
    return;
  }
  if (chars > MAX_CHARS) {
    plan.classList.add('over');
    plan.textContent = `${formatInt(chars)} characters: over the ${formatInt(MAX_CHARS)} limit. Trim it, or paste one section at a time.`;
    return;
  }
  if (service.state === 'checking') {
    plan.textContent = `${formatInt(chars)} characters${converted ? ' (converted from HTML)' : ''}.`;
    return;
  }
  if (readsLocally()) {
    plan.textContent = `${formatInt(chars)} characters${converted ? ' (converted from HTML)' : ''}. Read here by the rule set; nothing is uploaded.`;
    return;
  }
  const parts = chunkText(text).length;
  let msg = `${formatInt(chars)} characters${converted ? ' (converted from HTML)' : ''}, read in ${plural(parts, 'part')}.`;
  if (service.state === 'live') {
    msg += ` Uses up to ${plural(parts, 'reading')} of your ${formatInt(readingsLeft())} left today; parts read before come free from the cache.`;
  } else if (service.state === 'demo') {
    msg += ' Demo mode: you will get the clause map, without readings.';
  }
  plan.textContent = msg;
}

function formError(message) {
  const box = $('#form-error');
  box.textContent = message;
  box.hidden = !message;
}

async function postPart(text) {
  let r;
  try {
    r = await fetchJSON(`${API}/api/analyze`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text }),
    }, service.timeoutS * 1000);
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error('The page stopped waiting for this part. The model may still have been working on it, so it may have used one of today’s readings.');
    }
    throw new Error('The reading service could not be reached.');
  }
  if (!r.ok || !r.body || !Array.isArray(r.body.segments)) {
    throw new Error(r.body?.message || `The reading service answered with an error (${r.status}).`);
  }
  return r.body;
}

function progressUI(parts) {
  const box = $('#progress');
  box.hidden = false;
  const list = el('ol', 'progress-parts');
  const items = parts.map((p, i) => {
    const li = el('li');
    li.dataset.state = 'waiting';
    li.append(el('span', 'pp-name', `Part ${i + 1}`), el('span', 'pp-size', `${formatInt(cpLength(p))} characters`), el('span', 'pp-state', 'waiting'));
    list.append(li);
    return li;
  });
  const clock = el('p', 'progress-clock');
  box.replaceChildren(el('p', 'progress-lead', 'Reading the small print. A model reading can take a few minutes; nothing is lost if you wait.'), list, clock);
  const t0 = performance.now();
  const tick = () => { clock.textContent = `${Math.floor((performance.now() - t0) / 1000)} s elapsed`; };
  tick();
  const timer = setInterval(tick, 1000);
  return {
    set(i, state, label) {
      items[i].dataset.state = state;
      items[i].querySelector('.pp-state').textContent = label || state;
    },
    done() {
      clearInterval(timer);
      box.hidden = true;
    },
  };
}

// Reads the parts two at a time. A part that fails does not sink the rest:
// its slot holds { failed, message, text } and the page offers to read it
// again.
async function readParts(parts, progress) {
  const results = new Array(parts.length);
  let next = 0;
  const worker = async () => {
    while (next < parts.length) {
      const i = next;
      next += 1;
      progress.set(i, 'reading', 'reading…');
      try {
        results[i] = await postPart(parts[i]);
        const a = results[i].analysis;
        progress.set(i, 'done', a ? (a.source === 'cache' ? 'from cache' : 'read') : 'clause map only');
      } catch (err) {
        results[i] = { failed: true, message: err.message, text: parts[i] };
        progress.set(i, 'failed', 'not read');
      }
    }
  };
  await Promise.all([worker(), worker()]);
  return results;
}

function buildDoc(results, meta) {
  const doc = combineParts(results);
  Object.assign(doc, meta);
  doc.results = results;
  if (!doc.title) doc.title = guessTitle(doc);
  return doc;
}

function syncBudget(doc) {
  if (doc.budget && service.state === 'live') {
    service.budget = doc.budget;
    renderServiceStatus();
  }
}

async function onSubmit(event) {
  event.preventDefault();
  formError('');
  const { text, converted, chars } = currentInput();
  if (!chars) return formError('Paste the terms first.');
  if (!hasReadableText(text)) return formError('There is no readable text in that paste: only spaces or invisible characters.');
  if (chars > MAX_CHARS) return formError(`That is ${formatInt(chars)} characters; the limit is ${formatInt(MAX_CHARS)}.`);
  if (service.state === 'checking') await service.checked;
  if (readsLocally()) return readLocally(text, converted);
  const parts = chunkText(text);
  const button = $('#read');
  button.disabled = true;
  const progress = progressUI(parts);
  try {
    const results = await readParts(parts, progress);
    const firstFailure = results.find((r) => r.failed);
    if (results.every((r) => r.failed)) {
      formError(firstFailure.message);
      return;
    }
    const doc = buildDoc(results, { kind: 'pasted', converted, partTexts: parts });
    if (!doc.segments.some((s) => s.kind === 'clause') && !doc.failed) {
      formError(doc.notices.find((n) => n.kind === 'no_clauses')?.message || 'No clauses found in that text.');
      return;
    }
    syncBudget(doc);
    show(doc);
    remember(doc);
  } finally {
    progress.done();
    button.disabled = false;
    updatePlan();
  }
}

// The progress box for an on-device reading: the runtime download (first
// time only), then the reading itself.
function localProgressUI() {
  const box = $('#progress');
  const total = Number(RUNTIME.bytes) || 0;
  const warm = !!localReader.runtime;
  const list = el('ol', 'progress-parts local');
  const step = (name, state) => {
    const li = el('li');
    li.dataset.state = state;
    const detail = el('span', 'pp-state', state === 'done' ? 'loaded' : 'waiting');
    // The byte count changes many times a second; screen readers get it from
    // the progress bar instead of hearing every update in this live region.
    const size = el('span', 'pp-size');
    size.setAttribute('aria-hidden', 'true');
    li.append(el('span', 'pp-name', name), size, detail);
    list.append(li);
    return li;
  };
  const rt = step('Python runtime', warm ? 'done' : 'reading');
  const rd = step('Rule set', 'waiting');
  const set = (li, state, label, size) => {
    li.dataset.state = state;
    li.querySelector('.pp-state').textContent = label;
    if (size !== undefined) li.querySelector('.pp-size').textContent = size;
  };
  const meter = el('div', 'meter');
  meter.setAttribute('role', 'progressbar');
  meter.setAttribute('aria-label', 'Python runtime download');
  meter.setAttribute('aria-valuemin', '0');
  meter.setAttribute('aria-valuemax', '100');
  const fill = el('i', 'meter-fill');
  meter.append(fill);
  meter.hidden = warm;
  const clock = el('p', 'progress-clock');
  clock.setAttribute('aria-hidden', 'true');
  const lead = warm ? 'Reading on this device.' : `Reading on this device. First, the Python runtime (${formatBytes(total)}); later readings reuse it.`;
  box.replaceChildren(el('p', 'progress-lead', lead), list, meter, clock);
  box.hidden = false;
  if (!warm) set(rt, 'reading', 'downloading…', `0 of ${formatBytes(total)}`);
  const t0 = performance.now();
  const tick = () => { clock.textContent = `${Math.floor((performance.now() - t0) / 1000)} s elapsed`; };
  tick();
  const timer = setInterval(tick, 1000);
  let pct = -1;
  return {
    update(ev) {
      if (ev.stage === 'download') {
        const frac = total ? Math.min(1, ev.loaded / total) : 0;
        const now = Math.floor(frac * 100);
        fill.style.width = `${(frac * 100).toFixed(1)}%`;
        if (now !== pct) {
          pct = now;
          meter.setAttribute('aria-valuenow', String(now));
        }
        const size = `${formatBytes(Math.min(ev.loaded, total))} of ${formatBytes(total)}`;
        set(rt, 'reading', frac >= 1 ? 'starting Python…' : 'downloading…', size);
      } else if (ev.stage === 'reading') {
        if (!warm) set(rt, 'done', `ready in ${(ev.runtime.ms / 1000).toFixed(1)} s`, `Python ${ev.runtime.python}`);
        meter.hidden = true;
        set(rd, 'reading', 'reading…');
      }
    },
    done() {
      clearInterval(timer);
      box.hidden = true;
    },
  };
}

async function readLocally(text, converted) {
  if (!localReader) {
    formError('This browser cannot run the on-device reader: it needs WebAssembly and module workers. The exhibits still open.');
    return;
  }
  const button = $('#read');
  button.disabled = true;
  const progress = localProgressUI();
  try {
    const { payload, runtime } = await localReader.read(text, progress.update);
    if (!payload.segments.some((s) => s.kind === 'clause')) {
      formError(payload.notice?.message || 'No clauses found in that text.');
      return;
    }
    const doc = buildDoc([payload], { kind: 'rules', converted, runtime });
    show(doc);
    remember(doc);
  } catch (err) {
    formError(err.message);
  } finally {
    progress.done();
    button.disabled = false;
    renderServiceStatus();
    updatePlan();
  }
}

// Read one part again (after a failed request, or a model error), then
// rebuild the document in place.
async function retryPart(index, button) {
  const doc = current;
  if (!doc?.results) return;
  const old = doc.results[index];
  const partText = old.failed ? old.text : doc.partTexts?.[index];
  if (!partText) return;
  button.disabled = true;
  button.textContent = `Reading part ${index + 1}…`;
  let fresh;
  try {
    fresh = await postPart(partText);
  } catch (err) {
    fresh = { failed: true, message: err.message, text: partText };
  }
  const results = doc.results.slice();
  results[index] = fresh;
  const next = buildDoc(results, { kind: doc.kind, converted: doc.converted, title: doc.title, partTexts: doc.partTexts });
  syncBudget(next);
  show(next, { scroll: false });
  remember(next);
  document.getElementById(`part-${index + 1}`)?.focus({ preventScroll: false });
}

// --------------------------------------------------------------- exhibits
async function loadExhibits() {
  const list = $('#exhibits');
  try {
    const r = await fetchJSON('demo/index.json');
    if (!r.ok) throw new Error('demo index');
    list.replaceChildren(...r.body.map((d) => {
      const li = el('li');
      const b = el('button', 'exhibit');
      b.type = 'button';
      b.dataset.slug = d.slug;
      const sticker = el('span', 'ex-sticker');
      sticker.append(el('span', 'ex-word', 'Exhibit'), el('span', 'ex-letter', d.exhibit));
      const body = el('span', 'ex-body');
      body.append(
        el('span', 'ex-name', d.name),
        el('span', 'ex-kind', `${d.kind} · ${d.clauses} clauses · ${d.tally.them} favour them`),
      );
      b.append(sticker, body);
      b.addEventListener('click', () => openExhibit(d, b));
      li.append(b);
      return li;
    }));
  } catch {
    list.replaceChildren(el('li', 'muted', 'The exhibits could not be loaded.'));
  }
}

async function openExhibit(meta, button) {
  button.disabled = true;
  try {
    const r = await fetchJSON(`demo/${meta.slug}.json`);
    if (!r.ok) throw new Error('demo');
    show(buildDoc([r.body], { kind: 'demo', title: `Exhibit ${meta.exhibit}: ${meta.name}` }));
  } catch {
    formError('That exhibit could not be loaded.');
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- history
// Only complete documents are kept (a part that failed cannot be retried
// from history).
function remember(doc) {
  if (!doc.readings.length || doc.failed) return;
  const key = doc.keys.join(':');
  const { results, partTexts, ...saved } = doc; // eslint-disable-line no-unused-vars
  const list = (store(HISTORY_KEY) || []).filter((h) => h.key !== key);
  list.unshift({ key, title: doc.title, when: new Date().toISOString(), doc: saved });
  while (list.length > 5 || (list.length > 1 && JSON.stringify(list).length > 1500000)) list.pop();
  store(HISTORY_KEY, list);
  renderHistory();
}

function renderHistory() {
  const list = store(HISTORY_KEY) || [];
  const box = $('#history');
  box.hidden = !list.length;
  const ul = $('#history-list');
  ul.replaceChildren(...list.map((h) => {
    const li = el('li');
    const b = el('button', 'history-item');
    b.type = 'button';
    b.append(el('span', 'hi-title', h.title), el('span', 'hi-when', new Date(h.when).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })));
    b.addEventListener('click', () => show(h.doc));
    li.append(b);
    return li;
  }));
}

// ----------------------------------------------------------------- render
let current = null;
let clauseEls = [];
let activeIndex = -1;

function partsText(nums) {
  return `part${nums.length > 1 ? 's' : ''} ${nums.join(', ')}`;
}

function provenanceText(doc) {
  if (doc.kind === 'rules') {
    const e = doc.engine || {};
    const clauses = doc.segments.filter((x) => x.kind === 'clause').length;
    let t = `Rule-based reading, no AI: ${plural(Number(e.rules) || 0, 'hand-written rule')} found something in ${formatInt(doc.readings.length)} of ${plural(clauses, 'clause')}.`;
    if (doc.runtime) {
      t += ` Read by Python ${e.python || doc.runtime.python} (Pyodide ${doc.runtime.pyodide}) in this browser`
        + `${Number.isFinite(e.elapsed_ms) ? ` in ${formatInt(Math.max(1, e.elapsed_ms))} ms` : ''}; the text did not leave this device.`;
    }
    return t;
  }
  if (doc.kind === 'demo') {
    return 'Readings written by hand for this fictional document, in the same format the model returns, and checked by the same Python verifier.';
  }
  const read = doc.parts.filter((p) => p.source);
  if (!read.length) return 'Not read by the model: this is the clause map only.';
  const byModel = new Map();
  for (const p of read) {
    const name = modelName(p.model || REQUESTED_MODEL);
    if (!byModel.has(name)) byModel.set(name, []);
    byModel.get(name).push(p.part);
  }
  const multi = doc.parts.length > 1;
  const who = [...byModel].map(([name, nums]) => (multi ? `${name} (${partsText(nums)})` : name)).join(' and ');
  const fallback = read.some((p) => p.model && p.model !== REQUESTED_MODEL)
    ? ` ${modelName(REQUESTED_MODEL)} declined some of this text, so a fallback model answered where marked.`
    : '';
  const cacheOnly = read.every((p) => p.source === 'cache');
  const cacheSome = read.some((p) => p.source === 'cache');
  if (cacheOnly) return `Read earlier by ${who} and served from the cache (no new model call).${fallback}`;
  return `Read by ${who}${cacheSome ? '; some parts came from the cache' : ''}.${fallback}`;
}

function verificationText(doc) {
  const s = doc.stats;
  const clauses = doc.segments.filter((x) => x.kind === 'clause').length;
  if (!doc.sources.length) return `${plural(clauses, 'clause')} found by the Python segmenter. No readings yet.`;
  const reasons = Object.entries(s.reasons).map(([k, v]) => `${v} ${DROP_LABEL[k] || k}`);
  let t = `${plural(s.verified, 'quote')} checked word for word against the text, each inside its own clause, and kept; ${formatInt(s.dropped)} dropped`;
  t += reasons.length ? ` (${reasons.join('; ')}).` : '.';
  if (s.relocated) t += ` ${plural(s.relocated, 'reading')} moved to the clause where the quoted words actually are.`;
  if (doc.unread) t += doc.kind === 'rules' ? ` ${plural(doc.unread, 'clause')} matched no rule.` : ` ${plural(doc.unread, 'clause')} without a reading.`;
  return t;
}

const span = (r) => `${formatInt(r.quote_start)}–${formatInt(r.quote_end)}`;

function refText(r, seg, doc) {
  let t = `¶ ${seg.id} · quote at characters ${span(r)}`;
  if (doc.kind === 'rules' && r.rule) {
    t += ` · rule ${r.rule}`;
    const also = (r.findings || []).slice(1);
    if (also.length) t += `; also ${also.map((f) => `${f.rule} at ${span(f)}`).join(', ')}`;
  } else if (doc.kind !== 'demo' && r.model) {
    t += ` · read by ${modelName(r.model)}`;
  }
  return t;
}

function noteFor(r, seg, doc) {
  const note = el('span', 'note');
  note.setAttribute('role', 'note');
  note.dataset.fav = r.favours;
  note.id = `n${seg.id}`;
  const rules = doc.kind === 'rules' && r.topic;
  const head = el('span', 'note-head');
  head.append(el('span', 'sw'), el('span', 'note-label', LABEL[r.favours]), el('span', 'note-conf', rules ? `, ${MATCH[r.confidence]}.` : `, ${r.confidence} confidence.`));
  const ref = el('span', 'note-ref', refText(r, seg, doc));
  ref.title = 'Where the highlighted quote sits in the text as shown, counted in Unicode characters from the start.';
  // Run-in head: the label leads straight into the reading, as marginalia do.
  const body = el('span', 'note-body');
  body.append(head, ' ');
  if (rules) body.append(el('span', 'note-topic', `${r.topic}.`), ' ');
  body.append(el('span', 'note-reading', r.reading));
  note.append(body);
  const also = rules ? (r.findings || []).slice(1) : [];
  if (also.length) {
    const line = el('span', 'note-also');
    line.append(el('span', 'note-also-lead', 'Also: '));
    also.forEach((f, i) => {
      const item = el('span', 'note-also-item');
      item.dataset.fav = f.favours;
      item.append(el('span', 'sw'), el('span', null, f.topic), el('span', 'note-also-fav', ` (${LABEL[f.favours].toLowerCase()})`));
      item.title = f.reading;
      line.append(item, i + 1 < also.length ? '; ' : '.');
    });
    note.append(line);
  }
  note.append(ref);
  return note;
}

function showPartRules(doc) {
  return doc.parts.length > 1 || doc.parts.some((p) => p.failed || p.retryable);
}

function partRule(doc, p) {
  const box = el('div', `part-rule${p.failed ? ' failed' : ''}${p.retryable ? ' retryable' : ''}`);
  box.id = `part-${p.part}`;
  box.tabIndex = -1;
  box.setAttribute('role', 'group');
  box.setAttribute('aria-label', `Part ${p.part} of ${doc.parts.length}`);
  let status;
  if (p.failed) status = 'not read';
  else if (p.source) status = `${p.source === 'cache' ? 'from the cache, read by' : 'read by'} ${modelName(p.model || REQUESTED_MODEL)}`;
  else status = p.notice === 'demo_mode' ? 'clause map only (demo mode)' : 'clause map only, no readings';
  box.append(el('p', 'part-label', `Part ${p.part} of ${doc.parts.length} · ${formatInt(p.chars)} characters · ${status}`));
  if (p.failed) box.append(el('p', 'part-error', p.message));
  if ((p.failed || p.retryable) && doc.results) {
    const again = el('button', 'retry', `Read part ${p.part} again`);
    again.type = 'button';
    again.addEventListener('click', () => retryPart(p.part - 1, again));
    box.append(again);
  }
  if (p.failed && p.text) {
    const more = el('details', 'part-text');
    more.append(el('summary', null, 'Show this part’s text, unread'));
    for (const para of p.text.split(/\n{2,}/)) more.append(el('p', null, para));
    box.append(more);
  }
  return box;
}

function renderDocument(doc) {
  const article = $('#doc');
  const text = doc.text;
  const u = makeIndexer(text);
  const slice = (a, b) => text.slice(u(a), u(b));
  const readings = new Map(doc.readings.map((r) => [r.id, r]));
  const rulesAt = new Map();
  if (showPartRules(doc)) {
    for (const p of doc.parts) {
      if (!rulesAt.has(p.segStart)) rulesAt.set(p.segStart, []);
      rulesAt.get(p.segStart).push(p);
    }
  }
  article.classList.toggle('rules', doc.kind === 'rules');
  const frag = document.createDocumentFragment();
  let line = null;
  let prevEnd = 0;
  let first = true;
  clauseEls = [];

  const flushRules = (index) => {
    for (const p of rulesAt.get(index) || []) {
      frag.append(partRule(doc, p));
      line = null;
      first = true;
    }
  };

  doc.segments.forEach((seg, index) => {
    flushRules(index);
    const gap = first ? '' : slice(prevEnd, seg.start);
    const breaks = (gap.match(/\n/g) || []).length;
    const newPara = first || breaks > 1;
    if (seg.kind === 'heading') {
      const h = el('h3', `hd${newPara ? ' para' : ''}`, slice(seg.start, seg.end));
      frag.append(h);
      line = null;
    } else {
      if (!line || breaks > 0) {
        line = el('p', `ln${newPara ? ' para' : ''}`);
        frag.append(line);
      } else if (gap) {
        line.append(gap);
      }
      const r = readings.get(seg.id);
      const span = el('span', 'cl');
      span.dataset.fav = r ? r.favours : 'none';
      span.dataset.id = seg.id;
      span.id = `c${seg.id}`;
      span.tabIndex = -1;
      const num = el('span', 'num', String(seg.id));
      num.setAttribute('aria-hidden', 'true');
      span.append(num);
      // Every quote in the clause is highlighted in its own reading's colour;
      // where two overlap, the clause's label (the first) wins.
      const marks = r ? (r.findings?.length ? r.findings : [r]) : [];
      for (const run of markRuns(seg.start, seg.end, marks.map((m) => ({ start: m.quote_start, end: m.quote_end })))) {
        if (run.mark < 0) {
          span.append(slice(run.start, run.end));
        } else {
          const mark = el('mark', run.mark === 0 ? 'q' : 'q also', slice(run.start, run.end));
          if (run.mark > 0) mark.dataset.fav = marks[run.mark].favours;
          span.append(mark);
        }
      }
      line.append(span);
      if (r) line.append(noteFor(r, seg, doc));
      clauseEls.push(span);
    }
    prevEnd = seg.end;
    first = false;
  });
  flushRules(doc.segments.length);
  article.replaceChildren(frag);
}

function renderTally(doc) {
  const counts = tally(doc.readings);
  const box = $('#tally');
  const buttons = FAVOURS.map((f) => {
    const b = el('button', 'tally-btn');
    b.type = 'button';
    b.dataset.fav = f;
    b.disabled = counts[f] === 0;
    b.append(el('span', 'sw'), el('span', 'tally-label', LABEL[f]), el('span', 'tally-n', formatInt(counts[f])));
    b.title = counts[f] ? `Jump to the next clause marked “${LABEL[f].toLowerCase()}”` : '';
    b.addEventListener('click', () => jumpToNext(f));
    return b;
  });
  if (doc.unread) {
    const s = el('span', 'tally-btn static');
    s.dataset.fav = 'none';
    s.append(el('span', 'sw'), el('span', 'tally-label', labelFor(doc, 'none')), el('span', 'tally-n', formatInt(doc.unread)));
    buttons.push(s);
  }
  box.replaceChildren(...buttons);
}

function renderNotices(doc) {
  const box = $('#notices');
  const seen = new Map();
  for (const n of doc.notices) {
    if (!seen.has(n.kind)) seen.set(n.kind, { ...n, parts: [] });
    seen.get(n.kind).parts.push(n.part);
  }
  const notes = [...seen.values()].map((n) => {
    const p = el('p', 'notice');
    const where = doc.parts.length > 1 ? ` (${partsText(n.parts)})` : '';
    p.textContent = n.message + where;
    return p;
  });
  if (doc.failed) {
    const nums = doc.parts.filter((p) => p.failed).map((p) => p.part);
    notes.unshift(el('p', 'notice', `${nums.length === 1 ? 'One part' : `${nums.length} parts`} could not be read (${partsText(nums)}). The rest is below; each unread part has a button to read it again.`));
  }
  if (doc.kind === 'rules') {
    const caveat = el('p', 'notice caveat');
    caveat.append(
      el('strong', null, 'What rules can miss. '),
      'The rules look for set phrases. They miss clauses worded in ways they do not know, can misread words used in another sense, '
      + 'and cannot weigh how much a clause matters or how it plays with the rest. An unmarked clause matched no rule; that does not make it harmless.',
    );
    notes.push(caveat);
  }
  box.replaceChildren(...notes);
  if (doc.converted) box.append(el('p', 'notice subtle', 'This text was converted from HTML in your browser before reading; offsets count characters in the text as shown.'));
  box.hidden = !box.childElementCount;
}

// For a rule reading: every rule that found something, grouped by who it
// favours (click one to step through its clauses), and the concerns the
// rules looked for and did not find.
const GROUPS = [['them', 'Favour them'], ['unclear', 'Unclear'], ['you', 'Favour you'], ['neutral', 'Neutral']];

function renderChecklist(doc) {
  const box = $('#checklist');
  if (doc.kind !== 'rules' || !Array.isArray(doc.checked)) {
    box.hidden = true;
    box.replaceChildren();
    return;
  }
  const found = findingsByRule(doc.readings);
  const foundIds = new Set(found.map((f) => f.rule));
  const head = el('h3', 'side-h check-h', `What the rules found: ${formatInt(found.length)} of ${formatInt(doc.checked.length)} kinds of clause`);
  const parts = [head];
  for (const [fav, label] of GROUPS) {
    const mine = found.filter((f) => f.favours === fav);
    if (!mine.length) continue;
    const row = el('div', 'check-row');
    const list = el('ul', 'check-found');
    list.setAttribute('aria-label', label);
    for (const f of mine) {
      const li = el('li');
      const b = el('button', 'check-btn');
      b.type = 'button';
      b.dataset.fav = f.favours;
      b.dataset.rule = f.rule;
      const where = `¶ ${f.ids.slice(0, 3).join(', ')}${f.ids.length > 3 ? ', …' : ''}`;
      b.append(el('span', 'sw'), el('span', 'check-topic', f.topic), el('span', 'check-where', where));
      b.setAttribute('aria-label', `${f.topic}, in ${plural(f.ids.length, 'clause')}: jump to the next one.`);
      b.addEventListener('click', () => jumpToRule(f.rule));
      li.append(b);
      list.append(li);
    }
    row.append(el('span', 'check-group', label), list);
    parts.push(row);
  }
  const missing = doc.checked.filter((c) => c.favours === 'them' && !foundIds.has(c.rule)).map((c) => c.topic);
  if (missing.length) {
    const p = el('p', 'check-missing');
    p.append(el('span', 'check-missing-lead', 'Looked for, not found: '), `${missing.join(' · ')}.`, el('span', 'check-missing-tail', ' Not finding a phrase is not proof the terms lack it.'));
    parts.push(p);
  }
  box.replaceChildren(...parts);
  box.hidden = false;
}

function show(doc, { scroll = true } = {}) {
  current = doc;
  activeIndex = -1;
  const section = $('#result');
  section.hidden = false;
  $('#doc-title').textContent = doc.title;
  $('#provenance').textContent = provenanceText(doc);
  $('#verification').textContent = verificationText(doc);
  renderNotices(doc);
  renderTally(doc);
  renderChecklist(doc);
  renderDocument(doc);
  $('#where').textContent = '';
  document.body.classList.add('has-result');
  layout();
  if (scroll) {
    section.scrollIntoView({ behavior: reducedMotion.matches ? 'auto' : 'smooth', block: 'start' });
    $('#doc-title').focus({ preventScroll: true });
  }
}

// ------------------------------------------------ margin notes and minimap
// Wide screens: each note sits in the margin level with its clause. When a
// note would collide with the one above, the text opens up (padding above
// the clause's line) instead of the note drifting away from its clause, the
// way annotated editions make room for marginalia. Notes for clauses that
// share a line are set as one tight group. One measuring pass, one writing
// pass.
const NOTE_GAP = 10;
const GROUP_GAP = 3;

function layout() {
  if (!current) return;
  const article = $('#doc');
  const notes = [...article.querySelectorAll('.note')];
  article.querySelectorAll('.ln, .hd, .part-rule').forEach((l) => { l.style.paddingTop = ''; });
  if (wideLayout.matches) {
    const base = article.getBoundingClientRect().top;
    const measured = notes.map((n) => {
      const clause = n.previousElementSibling;
      const line = clause.parentElement;
      // Open the space above the section heading (or part rule), if the
      // line opens one, so it never drifts away from its first clause.
      let pad = line;
      while (pad.previousElementSibling?.matches('.hd, .part-rule')) pad = pad.previousElementSibling;
      return {
        note: n,
        line,
        pad,
        firstInLine: line.querySelector('.cl') === clause,
        top: (clause.getClientRects()[0]?.top ?? base) - base,
        height: n.offsetHeight,
      };
    });
    let shift = 0;
    let floor = 0;
    const pads = new Map();
    measured.forEach((m, i) => {
      let top = m.top + shift;
      if (top < floor && m.firstInLine) {
        const extra = Math.ceil(floor - top);
        pads.set(m.pad, (pads.get(m.pad) || 0) + extra);
        shift += extra;
        top += extra;
      }
      const y = Math.max(top, floor);
      m.y = y;
      const sameLine = measured[i + 1]?.line === m.line;
      m.note.classList.toggle('grouped', sameLine || measured[i - 1]?.line === m.line);
      floor = y + m.height + (sameLine ? GROUP_GAP : NOTE_GAP);
    });
    pads.forEach((px, line) => { line.style.paddingTop = `${px}px`; });
    measured.forEach((m) => { m.note.style.top = `${Math.round(m.y)}px`; });
    article.style.minHeight = `${Math.ceil(floor)}px`;
  } else {
    notes.forEach((n) => { n.style.top = ''; n.classList.remove('grouped'); });
    article.style.minHeight = '';
  }
  drawMinimap();
}

function drawMinimap() {
  const map = $('#minimap');
  const blocks = $('#minimap .mm-blocks');
  const article = $('#doc');
  const box = article.getBoundingClientRect();
  const height = article.scrollHeight || 1;
  blocks.replaceChildren(...clauseEls.map((c) => {
    const rects = c.getClientRects();
    if (!rects.length) return el('i');
    const top = rects[0].top - box.top;
    const bottom = rects[rects.length - 1].bottom - box.top;
    const block = el('i', 'mm-block');
    block.dataset.fav = c.dataset.fav;
    block.style.top = `${(top / height) * 100}%`;
    block.style.height = `${Math.max(0.25, ((bottom - top) / height) * 100)}%`;
    return block;
  }));
  map.hidden = false;
  updateMinimapView();
  placeTick();
}

function updateMinimapView() {
  if (!current) return;
  const article = $('#doc');
  const box = article.getBoundingClientRect();
  const height = article.scrollHeight || 1;
  const view = $('#minimap .mm-view');
  const top = Math.max(0, Math.min(1, -box.top / height));
  const size = Math.min(1, innerHeight / height);
  view.style.top = `${top * 100}%`;
  view.style.height = `${size * 100}%`;
}

// A tick on the strip marks the selected clause.
function placeTick() {
  const tick = $('#minimap .mm-tick');
  const c = clauseEls[activeIndex];
  const rect = c?.getClientRects()[0];
  if (!rect) {
    tick.hidden = true;
    return;
  }
  const article = $('#doc');
  const top = rect.top - article.getBoundingClientRect().top;
  tick.style.top = `${(top / (article.scrollHeight || 1)) * 100}%`;
  tick.hidden = false;
}

function onMinimapClick(event) {
  const track = event.currentTarget.querySelector('.mm-track').getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (event.clientY - track.top) / track.height));
  const article = $('#doc');
  const top = article.getBoundingClientRect().top + scrollY;
  scrollTo({ top: top + frac * article.scrollHeight - innerHeight / 2, behavior: reducedMotion.matches ? 'auto' : 'smooth' });
}

// ------------------------------------------------------------- navigation
function setActive(i) {
  const prev = clauseEls[activeIndex];
  if (prev) {
    prev.classList.remove('active');
    document.getElementById(`n${prev.dataset.id}`)?.classList.remove('active');
  }
  activeIndex = i;
  const c = clauseEls[i];
  placeTick();
  if (!c) {
    $('#where').textContent = '';
    return;
  }
  c.classList.add('active');
  const note = document.getElementById(`n${c.dataset.id}`);
  note?.classList.add('active');
  c.focus({ preventScroll: true });
  c.scrollIntoView({ block: 'center', behavior: reducedMotion.matches ? 'auto' : 'smooth' });
  const fav = c.dataset.fav;
  const ref = note?.querySelector('.note-ref')?.textContent;
  $('#where').textContent = `Clause ${c.dataset.id} of ${clauseEls.length}: ${labelFor(current, fav).toLowerCase()}.${ref ? ` ${ref.replace(/^¶ \d+ · /, '')}.` : ''}`;
}

function step(delta) {
  if (!clauseEls.length) return;
  const i = activeIndex < 0 ? (delta > 0 ? 0 : clauseEls.length - 1) : (activeIndex + delta + clauseEls.length) % clauseEls.length;
  setActive(i);
}

function jumpToNext(fav) {
  const n = clauseEls.length;
  for (let k = 1; k <= n; k += 1) {
    const i = (Math.max(activeIndex, -1) + k) % n;
    if (clauseEls[i].dataset.fav === fav) return setActive(i);
  }
  return undefined;
}

function jumpToRule(rule) {
  const hits = new Set(current.readings.filter((r) => (r.findings || []).some((f) => f.rule === rule)).map((r) => String(r.id)));
  const n = clauseEls.length;
  for (let k = 1; k <= n; k += 1) {
    const i = (Math.max(activeIndex, -1) + k) % n;
    if (hits.has(clauseEls[i].dataset.id)) return setActive(i);
  }
  return undefined;
}

function onKey(event) {
  if (!current || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest?.('input, textarea, select, [contenteditable="true"]')) return;
  if (event.key === 'j') step(1);
  else if (event.key === 'k') step(-1);
  else if (event.key === 'Escape' && activeIndex >= 0) setActive(-1);
  else return;
  event.preventDefault();
}

function linkHover(event) {
  const target = event.target.closest?.('.cl, .note');
  document.querySelectorAll('#doc .hot').forEach((n) => n.classList.remove('hot'));
  if (!target) return;
  const id = target.classList.contains('cl') ? target.dataset.id : target.id.slice(1);
  $(`#c${id}`)?.classList.add('hot');
  document.getElementById(`n${id}`)?.classList.add('hot');
}

function onDocClick(event) {
  const c = event.target.closest?.('.cl');
  if (c && !getSelection().toString()) setActive(clauseEls.indexOf(c));
}

// The quote offsets (and, for read documents, the model) live behind one
// disclosure for the whole page, so notes stay short; the selected clause's
// note always shows them.
function initOffsets() {
  const btn = $('#offsets');
  btn.addEventListener('click', () => {
    const on = btn.getAttribute('aria-pressed') !== 'true';
    btn.setAttribute('aria-pressed', String(on));
    $('#doc').classList.toggle('show-refs', on);
    layout();
  });
}

// ------------------------------------------------------------------- boot
function debounce(fn, ms) {
  let t;
  return () => {
    clearTimeout(t);
    t = setTimeout(fn, ms);
  };
}

function boot() {
  initTheme();
  initOffsets();
  $('#intake').addEventListener('submit', onSubmit);
  $('#paste').addEventListener('input', debounce(updatePlan, 120));
  document.addEventListener('keydown', onKey);
  $('#doc').addEventListener('mouseover', linkHover);
  $('#doc').addEventListener('click', onDocClick);
  $('#minimap').addEventListener('click', onMinimapClick);
  let frame = 0;
  addEventListener('scroll', () => {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; updateMinimapView(); });
  }, { passive: true });
  addEventListener('resize', debounce(layout, 150));
  wideLayout.addEventListener('change', layout);
  document.fonts?.ready.then(layout);
  renderServiceStatus();
  renderHistory();
  updatePlan();
  loadExhibits();
  service.checked = checkService();
}

boot();
