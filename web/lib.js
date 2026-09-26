// Pure helpers for the page, with no DOM access, so Node can test them.
// Offsets from the API are Unicode code points; JavaScript strings index by
// UTF-16 code units. makeIndexer bridges the two.

export const MAX_CHARS = 60000; // the Worker's hard cap per document
export const PART_CHARS = 10000; // one model call reads at most one part

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
export function prepText(s) {
  return s.replace(/\r\n?/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
}

const HTML_TAG = /<\/?(?:p|div|li|ul|ol|h[1-6]|br|section|article|span|a|strong|em|b|i|table|tr|td|body|html|head|main)\b[^>]*>/gi;

export function looksLikeHtml(s) {
  const m = s.match(HTML_TAG);
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
  return prepText(out.replace(/[ \t]+\n/g, '\n').replace(/\n[ \t]+/g, '\n'));
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

// Stitch API responses for consecutive parts into one document. Offsets and
// clause numbers are shifted so they index the combined text, which is the
// parts' normalised texts joined by a blank line.
export function combineParts(responses) {
  const texts = [];
  const segments = [];
  const readings = [];
  const notices = [];
  const sources = new Set();
  const stats = { received: 0, verified: 0, dropped: 0, relocated: 0, reasons: {} };
  let offset = 0;
  let idBase = 0;
  let model = null;
  let budget = null;
  let unread = 0;
  responses.forEach((r, i) => {
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
    if (r.analysis) {
      const a = r.analysis;
      sources.add(a.source);
      model = model || a.model;
      for (const x of a.readings) {
        readings.push({ ...x, id: x.id + idBase, quote_start: x.quote_start + offset, quote_end: x.quote_end + offset });
      }
      stats.received += a.received;
      stats.verified += a.verified;
      stats.dropped += a.dropped;
      stats.relocated += a.relocated || 0;
      for (const [k, v] of Object.entries(a.dropped_reasons || {})) stats.reasons[k] = (stats.reasons[k] || 0) + v;
      unread += clauseCount - a.readings.length;
    } else {
      unread += clauseCount;
    }
    if (r.notice) notices.push({ part: i + 1, kind: r.notice.kind, message: r.notice.message });
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
    model,
    notices,
    budget,
    unread,
    parts: responses.length,
    keys: responses.map((r) => r.sha256),
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
