// Pure helpers for the page, with no DOM access, so Node can test them.
// Offsets from the API are Unicode code points; JavaScript strings index by
// UTF-16 code units. makeIndexer bridges the two.

export const MAX_CHARS = 60000; // the Worker's hard cap per document
export const PART_CHARS = 10000; // one model call reads at most one part (the Worker's cap too)
export const REQUESTED_MODEL = 'claude-opus-5-5';

export function cpLength(s) {
  let n = 0;
  for (const _ of s) n += 1; // eslint-disable-line no-unused-vars
  return n;
}

// Returns f(codePointIndex) -> UTF-16 index into `text`.
export function makeIndexer(text) {
  if (!/[\uD800-\uDFFF]/.test(text)) return (i) => i;
  const map = [];
  let u = 0;
  let cp = 0;
  for (const ch of text) {
    map[cp] = u;
    cp += 1;
    u += ch.length;
  }
  map[cp] = u;
  return (i) => map[Math.max(0, Math.min(i, cp))];
}

// Light clean-up before splitting; the Worker does the full normalisation.
// Page and line separators become breaks here, as they do on the Worker, so
// a part never grows past PART_CHARS on its way through normalisation.
export function prepText(s) {
  return s
    .replace(/\r\n?/g, '\n')
    .replace(/[\f\u2029]/g, '\n\n')
    .replace(/\u2028/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

// Characters the Worker removes (controls, zero-width marks, the BOM, soft
// hyphens) plus every kind of space: a paste made only of these has nothing
// to read, and is caught here instead of being sent.
const INVISIBLE = /[\s\u0000-\u001f\u007f\u00a0\u00ad\u1680\u2000-\u200d\u2028-\u202f\u205f\u2060\u3000\ufeff]/g;

export function hasReadableText(s) {
  return s.replace(INVISIBLE, '').length > 0;
}

// "claude-opus-5-5" -> "Claude Opus 5.5". Unknown shapes come back as-is.
export function modelName(id) {
  if (!id) return 'the model';
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?$/.exec(id);
  if (!m) return id;
  return `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ''}`;
}

const HTML_TAG = /<\/?(?:p|div|li|ul|ol|h[1-6]|br|section|article|span|a|strong|em|b|i|table|tr|td|body|html|head|main)\b[^>]*>/gi;

// A tag must close with a '>'. Tag starts after the last '>' can never match,
// and each would make [^>]* scan to the end of the text (quadratic on a paste
// of unclosed "<p"), so only the text up to the last '>' is searched.
export function looksLikeHtml(s) {
  const m = s.slice(0, s.lastIndexOf('>') + 1).match(HTML_TAG);
  return !!m && m.length >= 2;
}

const BLOCK = new Set(
  'address article aside blockquote dd div dl dt figcaption figure footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tbody td tfoot th thead tr ul'.split(' '),
);
const PARAGRAPH = new Set('p h1 h2 h3 h4 h5 h6 section article blockquote table ul ol'.split(' '));
const DROP = 'script, style, noscript, template, head, svg, iframe, object, embed, canvas, button, select, textarea';

// HTML source -> plain text, using an inert parsed document (DOMParser does
// not run scripts or load resources). `Parser` is injectable for tests.
export function htmlToText(html, Parser = globalThis.DOMParser) {
  const doc = new Parser().parseFromString(html, 'text/html');
  doc.querySelectorAll(DROP).forEach((n) => n.remove());
  let out = '';
  const walk = (node) => {
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        out += child.nodeValue.replace(/\s+/g, ' ');
      } else if (child.nodeType === 1) {
        const tag = child.tagName.toLowerCase();
        if (tag === 'br') {
          out += '\n';
          continue;
        }
        const sep = PARAGRAPH.has(tag) ? '\n\n' : BLOCK.has(tag) ? '\n' : '';
        out += sep;
        walk(child);
        out += sep;
      }
    }
  };
  walk(doc.body || doc.documentElement);
  // (?<![ \t]) tries a run of spaces only from its first space, so a long run
  // costs its length once, not its length squared.
  return prepText(out.replace(/(?<![ \t])[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n'));
}

function pack(pieces, joiner, max) {
  const out = [];
  let cur = '';
  for (const p of pieces) {
    if (!cur) cur = p;
    else if (cur.length + joiner.length + p.length <= max) cur += joiner + p;
    else {
      out.push(cur);
      cur = p;
    }
  }
  if (cur) out.push(cur);
  return out;
}

function splitLong(p, max) {
  if (p.length <= max) return [p];
  if (p.includes('\n')) return pack(p.split('\n').flatMap((l) => splitLong(l, max)), '\n', max);
  const sentences = p.split(/(?<=[.!?]["'”’)]*)\s+(?=["'“(]?[A-Z0-9])/);
  if (sentences.length > 1) return pack(sentences.flatMap((s) => splitLong(s, max)), ' ', max);
  // Last resort: cut at the last space before the limit (never inside a word).
  const out = [];
  let rest = p;
  while (rest.length > max) {
    let cut = rest.lastIndexOf(' ', max);
    if (cut <= 0) cut = max;
    if (/[\uD800-\uDBFF]/.test(rest[cut - 1])) cut -= 1; // keep surrogate pairs whole
    out.push(rest.slice(0, cut).trimEnd());
    rest = rest.slice(cut).trimStart();
  }
  if (rest) out.push(rest);
  return out;
}

// Split a document into parts of at most `max` characters, at paragraph
// breaks where possible, then line breaks, then sentence ends.
export function chunkText(text, max = PART_CHARS) {
  const paras = text.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
  return pack(paras.flatMap((p) => splitLong(p, max)), '\n\n', max);
}

// Notices after which reading the same part again may work.
const RETRYABLE = new Set(['model_busy', 'model_error', 'model_timeout', 'model_interrupted', 'bad_output']);

// Stitch API responses for consecutive parts into one document. Offsets and
// clause numbers are shifted so they index the combined text, which is the
// parts' normalised texts joined by a blank line. A part whose request
// failed outright is passed as { failed: true, message, text } and
// contributes no text, only an entry in `parts` (so the page can offer to
// read it again). Each reading keeps the model that read its part.
export function combineParts(responses) {
  const texts = [];
  const segments = [];
  const readings = [];
  const notices = [];
  const parts = [];
  const sources = new Set();
  const models = new Set();
  const stats = { received: 0, verified: 0, dropped: 0, relocated: 0, reasons: {} };
  let engine = null;
  let checked = null;
  let offset = 0;
  let idBase = 0;
  let budget = null;
  let unread = 0;
  responses.forEach((r, i) => {
    const info = { part: i + 1, segStart: segments.length, chars: 0, clauses: 0, model: null, source: null };
    parts.push(info);
    if (r.failed) {
      Object.assign(info, { failed: true, message: r.message, text: r.text, chars: cpLength(r.text || '') });
      return;
    }
    let maxId = 0;
    let clauseCount = 0;
    for (const s of r.segments) {
      const seg = { kind: s.kind, start: s.start + offset, end: s.end + offset };
      if (s.kind === 'clause') {
        seg.id = s.id + idBase;
        maxId = Math.max(maxId, s.id);
        clauseCount += 1;
      }
      segments.push(seg);
    }
    info.chars = cpLength(r.text);
    info.clauses = clauseCount;
    if (r.analysis) {
      const a = r.analysis;
      sources.add(a.source);
      info.source = a.source;
      info.model = a.model || null;
      if (a.model) models.add(a.model);
      const shift = (x) => ({ ...x, quote_start: x.quote_start + offset, quote_end: x.quote_end + offset });
      for (const x of a.readings) {
        const r = { ...shift(x), id: x.id + idBase, model: a.model || null, part: i + 1 };
        if (Array.isArray(x.findings)) r.findings = x.findings.map(shift);
        readings.push(r);
      }
      if (a.engine) engine = a.engine;
      if (Array.isArray(a.checked)) checked = a.checked;
      stats.received += a.received;
      stats.verified += a.verified;
      stats.dropped += a.dropped;
      stats.relocated += a.relocated || 0;
      for (const [k, v] of Object.entries(a.dropped_reasons || {})) stats.reasons[k] = (stats.reasons[k] || 0) + v;
      unread += clauseCount - a.readings.length;
    } else {
      unread += clauseCount;
    }
    if (r.notice) {
      notices.push({ part: i + 1, kind: r.notice.kind, message: r.notice.message });
      info.notice = r.notice.kind;
      info.retryable = RETRYABLE.has(r.notice.kind);
    }
    if (r.budget) budget = r.budget;
    texts.push(r.text);
    offset += cpLength(r.text) + 2;
    idBase += maxId;
  });
  return {
    text: texts.join('\n\n'),
    segments,
    readings,
    stats,
    sources: [...sources],
    models: [...models],
    notices,
    budget,
    engine,
    checked,
    unread,
    parts,
    failed: parts.filter((p) => p.failed).length,
    keys: responses.map((r) => r.sha256 || null),
  };
}

export const FAVOURS = ['them', 'you', 'unclear', 'neutral'];

export function tally(readings) {
  const t = { you: 0, them: 0, neutral: 0, unclear: 0 };
  for (const r of readings) t[r.favours] += 1;
  return t;
}

export function formatInt(n) {
  return Math.round(n).toLocaleString('en-US');
}

export function plural(n, one, many = `${one}s`) {
  return `${formatInt(n)} ${n === 1 ? one : many}`;
}

// A title for a pasted document: its first heading, else its first words.
export function guessTitle(doc) {
  const u = makeIndexer(doc.text);
  const first = doc.segments[0];
  if (!first) return 'Pasted document';
  let t = doc.text.slice(u(first.start), u(first.end)).replace(/\s+/g, ' ').trim();
  if (first.kind !== 'heading' && t.length > 60) t = `${t.slice(0, 57).replace(/\s+\S*$/, '')}…`;
  return t || 'Pasted document';
}

// Split [start, end) into runs for highlighting several quotes in one
// clause. `marks` are { start, end, ... } in priority order: where two
// overlap, the earlier one wins. Returns [{ start, end, mark }] covering the
// whole range, with mark = the index into `marks`, or -1 for plain text.
export function markRuns(start, end, marks) {
  const cuts = new Set([start, end]);
  marks.forEach((m) => {
    const s = Math.max(start, Math.min(m.start, end));
    const e = Math.max(start, Math.min(m.end, end));
    if (s < e) cuts.add(s).add(e);
  });
  const points = [...cuts].sort((a, b) => a - b);
  const runs = [];
  for (let i = 0; i + 1 < points.length; i += 1) {
    const a = points[i];
    const b = points[i + 1];
    let mark = -1;
    for (let k = 0; k < marks.length; k += 1) {
      if (marks[k].start <= a && b <= marks[k].end) { mark = k; break; }
    }
    const last = runs[runs.length - 1];
    if (last && last.mark === mark && last.end === a) last.end = b;
    else runs.push({ start: a, end: b, mark });
  }
  return runs;
}

// Rule readings indexed by rule: { rule, topic, favours, ids } for every
// rule that found something, in the order the document first shows it.
export function findingsByRule(readings) {
  const out = new Map();
  for (const r of readings) {
    for (const f of r.findings || []) {
      if (!f.rule) continue;
      if (!out.has(f.rule)) out.set(f.rule, { rule: f.rule, topic: f.topic, favours: f.favours, ids: [] });
      const ids = out.get(f.rule).ids;
      if (ids[ids.length - 1] !== r.id) ids.push(r.id);
    }
  }
  return [...out.values()];
}

// 13,566,000 -> "13.6 MB" (decimal megabytes, as browsers report downloads).
export function formatBytes(n) {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1e6) return `${Math.max(1, Math.round(n / 1e3))} kB`;
  return `${(n / 1e6).toFixed(1)} MB`;
}
