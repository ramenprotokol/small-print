// small print: the page. Talks to /api (the Python Worker), renders the
// marked-up document, margin notes and the "who it favours" strip.
import {
  FAVOURS,
  MAX_CHARS,
  PART_CHARS,
  chunkText,
  combineParts,
  cpLength,
  formatInt,
  guessTitle,
  htmlToText,
  looksLikeHtml,
  makeIndexer,
  plural,
  prepText,
  tally,
} from "./lib.js";

const $ = (sel, root = document) => root.querySelector(sel);
const API = ($('meta[name="sp-api"]')?.content || '').replace(/\/$/, '');
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const wideLayout = matchMedia('(min-width: 1100px)');
const LABEL = { you: 'Favours you', them: 'Favours them', neutral: 'Neutral', unclear: 'Unclear', none: 'No reading' };
const PART_TIMEOUT_MS = 240000;
const HISTORY_KEY = 'sp-history';
const THEME_KEY = 'sp-theme';

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

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
const service = { state: 'checking', budget: null };

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
    rest.textContent = ` Claude Opus 5.5 at ${service.effort || 'medium'} effort. ${plural(left, 'reading')} left for you today `
      + `(${formatInt(service.budget.remaining)} of ${formatInt(service.budget.limit)} across all visitors); resets at 00:00 UTC. `
      + `One reading covers up to ${formatInt(PART_CHARS)} characters.`;
  } else if (service.state === 'demo') {
    lead.textContent = 'Demo mode.';
    rest.textContent = ' This server has no AI key, so pasted text gets a clause map without readings. The three exhibits are fully read.';
  } else {
    lead.textContent = 'The reading service is out of reach.';
    rest.textContent = ' The three exhibits below still open.';
  }
  box.replaceChildren(lead, rest);
}

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
  const r = await fetchJSON(`${API}/api/analyze`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text }),
  }, PART_TIMEOUT_MS);
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
  box.replaceChildren(el('p', 'progress-lead', 'Reading the small print. A model reading can take a while; nothing is lost if you wait.'), list, clock);
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

async function readParts(parts, progress) {
  const results = new Array(parts.length);
  let next = 0;
  const worker = async () => {
    while (next < parts.length) {
      const i = next;
      next += 1;
      progress.set(i, 'reading', 'reading…');
      results[i] = await postPart(parts[i]);
      const a = results[i].analysis;
      progress.set(i, 'done', a ? (a.source === 'cache' ? 'from cache' : 'read') : 'clause map only');
    }
  };
  await Promise.all([worker(), worker()]);
  return results;
}

async function onSubmit(event) {
  event.preventDefault();
  formError('');
  const { text, converted, chars } = currentInput();
  if (!chars) return formError('Paste the terms first.');
  if (chars > MAX_CHARS) return formError(`That is ${formatInt(chars)} characters; the limit is ${formatInt(MAX_CHARS)}.`);
  if (service.state === 'offline') {
    return formError('The reading service is out of reach, so pasted text cannot be read right now. The exhibits still work.');
  }
  const parts = chunkText(text);
  const button = $('#read');
  button.disabled = true;
  const progress = progressUI(parts);
  try {
    const results = await readParts(parts, progress);
    const doc = combineParts(results);
    doc.kind = 'pasted';
    doc.converted = converted;
    doc.title = guessTitle(doc);
    if (doc.budget && service.state === 'live') {
      service.budget = doc.budget;
      renderServiceStatus();
    }
    show(doc);
    remember(doc);
  } catch (err) {
    formError(err.name === 'AbortError' ? 'The reading took too long and was stopped. Try again, or a shorter text.' : err.message);
  } finally {
    progress.done();
    button.disabled = false;
    updatePlan();
  }
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
      b.append(
        el('span', 'ex-label', `Exhibit ${d.exhibit}`),
        el('span', 'ex-name', d.name),
        el('span', 'ex-kind', `${d.kind} · ${d.clauses} clauses · ${d.tally.them} favour them`),
      );
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
    const doc = combineParts([r.body]);
    doc.kind = 'demo';
    doc.title = `Exhibit ${meta.exhibit}: ${meta.name}`;
    show(doc);
  } catch {
    formError('That exhibit could not be loaded.');
  } finally {
    button.disabled = false;
  }
}

// ---------------------------------------------------------------- history
function remember(doc) {
  if (!doc.readings.length) return;
  const key = doc.keys.join(':');
  const list = (store(HISTORY_KEY) || []).filter((h) => h.key !== key);
  list.unshift({ key, title: doc.title, when: new Date().toISOString(), doc });
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

function provenanceText(doc) {
  if (doc.kind === 'demo') {
    return 'Readings written by hand for this fictional document, in the same format the model returns, and checked by the same Python verifier.';
  }
  const model = doc.model === 'claude-opus-5-5' || !doc.model ? 'Claude Opus 5.5' : doc.model;
  const from = doc.sources.includes('cache') && !doc.sources.includes('model')
    ? `Read earlier by ${model} and served from the cache (no new model call).`
    : doc.sources.length ? `Read by ${model}${doc.sources.includes('cache') ? ' (some parts from the cache)' : ''}.` : '';
  return from || 'Not read by the model: this is the clause map only.';
}

function verificationText(doc) {
  const s = doc.stats;
  if (!doc.sources.length) return `${plural(doc.segments.filter((x) => x.kind === 'clause').length, 'clause')} found by the Python segmenter. No readings yet.`;
  const reasons = Object.entries(s.reasons).map(([k, v]) => `${v} ${{
    not_found: 'not in the text word for word',
    ambiguous: 'ambiguous (the words appear more than once)',
    duplicate: 'duplicates',
    malformed: 'malformed',
    too_short: 'too short to count as evidence',
  }[k] || k}`);
  let t = `${plural(s.verified, 'quote')} checked word for word against the text and kept; ${formatInt(s.dropped)} dropped`;
  t += reasons.length ? ` (${reasons.join('; ')}).` : '.';
  if (s.relocated) t += ` ${plural(s.relocated, 'reading')} moved to the clause where the quoted words actually are.`;
  if (doc.unread) t += ` ${plural(doc.unread, 'clause')} without a reading.`;
  return t;
}

function noteFor(r, seg) {
  const note = el('span', 'note');
  note.setAttribute('role', 'note');
  note.dataset.fav = r.favours;
  note.id = `n${seg.id}`;
  const head = el('span', 'note-head');
  head.append(el('span', 'sw'), el('span', 'note-label', LABEL[r.favours]), el('span', 'note-conf', `${r.confidence} confidence`));
  const ref = el('span', 'note-ref', `¶ ${seg.id} · quote at characters ${formatInt(r.quote_start)}–${formatInt(r.quote_end)}`);
  ref.title = 'Where the highlighted quote sits in the text as shown, counted in Unicode characters from the start.';
  note.append(head, el('span', 'note-reading', r.reading), ref);
  return note;
}

function renderDocument(doc) {
  const article = $('#doc');
  const text = doc.text;
  const u = makeIndexer(text);
  const slice = (a, b) => text.slice(u(a), u(b));
  const readings = new Map(doc.readings.map((r) => [r.id, r]));
  const frag = document.createDocumentFragment();
  let line = null;
  let prevEnd = 0;
  let first = true;
  clauseEls = [];

  for (const seg of doc.segments) {
    const gap = slice(prevEnd, seg.start);
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
      const qs = r ? Math.max(seg.start, Math.min(r.quote_start, seg.end)) : 0;
      const qe = r ? Math.max(seg.start, Math.min(r.quote_end, seg.end)) : 0;
      if (r && qs < qe) {
        const mark = el('mark', 'q', slice(qs, qe));
        span.append(slice(seg.start, qs), mark, slice(qe, seg.end));
      } else {
        span.append(slice(seg.start, seg.end));
      }
      line.append(span);
      if (r) line.append(noteFor(r, seg));
      clauseEls.push(span);
    }
    prevEnd = seg.end;
    first = false;
  }
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
    s.append(el('span', 'sw'), el('span', 'tally-label', LABEL.none), el('span', 'tally-n', formatInt(doc.unread)));
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
  box.replaceChildren(...[...seen.values()].map((n) => {
    const p = el('p', 'notice');
    const where = doc.parts > 1 ? ` (part${n.parts.length > 1 ? 's' : ''} ${n.parts.join(', ')})` : '';
    p.textContent = n.message + where;
    return p;
  }));
  if (doc.converted) box.append(el('p', 'notice subtle', 'This text was converted from HTML in your browser before reading; offsets count characters in the text as shown.'));
  box.hidden = !box.childElementCount;
}

function show(doc) {
  current = doc;
  activeIndex = -1;
  const section = $('#result');
  section.hidden = false;
  $('#doc-title').textContent = doc.title;
  $('#provenance').textContent = provenanceText(doc);
  $('#verification').textContent = verificationText(doc);
  renderNotices(doc);
  renderTally(doc);
  renderDocument(doc);
  $('#where').textContent = '';
  document.body.classList.add('has-result');
  layout();
  section.scrollIntoView({ behavior: reducedMotion.matches ? 'auto' : 'smooth', block: 'start' });
  $('#doc-title').focus({ preventScroll: true });
}

// ------------------------------------------------ margin notes and minimap
// Wide screens: each note sits in the margin level with its clause. When a
// note would collide with the one above, the text opens up (padding above
// the clause's line) instead of the note drifting away from its clause, the
// way annotated editions make room for marginalia. One measuring pass, one
// writing pass.
const NOTE_GAP = 12;

function layout() {
  if (!current) return;
  const article = $('#doc');
  const notes = [...article.querySelectorAll('.note')];
  article.querySelectorAll('.ln, .hd').forEach((l) => { l.style.paddingTop = ''; });
  if (wideLayout.matches) {
    const base = article.getBoundingClientRect().top;
    const measured = notes.map((n) => {
      const clause = n.previousElementSibling;
      const line = clause.parentElement;
      // Open the space above the section heading, if the line opens a
      // section, so a heading never drifts away from its first clause.
      let pad = line;
      while (pad.previousElementSibling?.classList.contains('hd')) pad = pad.previousElementSibling;
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
    for (const m of measured) {
      let top = m.top + shift;
      if (top < floor && m.firstInLine) {
        const extra = Math.ceil(floor - top);
        pads.set(m.pad, (pads.get(m.pad) || 0) + extra);
        shift += extra;
        top += extra;
      }
      const y = Math.max(top, floor);
      m.y = y;
      floor = y + m.height + NOTE_GAP;
    }
    pads.forEach((px, line) => { line.style.paddingTop = `${px}px`; });
    measured.forEach((m) => { m.note.style.top = `${Math.round(m.y)}px`; });
    article.style.minHeight = `${Math.ceil(floor)}px`;
  } else {
    notes.forEach((n) => { n.style.top = ''; });
    article.style.minHeight = '';
  }
  drawMinimap();
}

function drawMinimap() {
  const map = $('#minimap');
  const track = $('#minimap .mm-track');
  const article = $('#doc');
  const box = article.getBoundingClientRect();
  const height = article.scrollHeight || 1;
  track.replaceChildren(...clauseEls.map((c) => {
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

function onMinimapClick(event) {
  const map = event.currentTarget.getBoundingClientRect();
  const frac = (event.clientY - map.top) / map.height;
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
  if (!c) {
    $('#where').textContent = '';
    return;
  }
  c.classList.add('active');
  document.getElementById(`n${c.dataset.id}`)?.classList.add('active');
  c.focus({ preventScroll: true });
  c.scrollIntoView({ block: 'center', behavior: reducedMotion.matches ? 'auto' : 'smooth' });
  const fav = c.dataset.fav;
  $('#where').textContent = `Clause ${c.dataset.id} of ${clauseEls.length}: ${LABEL[fav].toLowerCase()}.`;
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
  checkService();
}

boot();
