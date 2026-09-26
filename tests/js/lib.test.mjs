import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PART_CHARS,
  chunkText,
  combineParts,
  cpLength,
  guessTitle,
  looksLikeHtml,
  makeIndexer,
  prepText,
  tally,
} from '../../web/lib.js';

test('cpLength counts code points, not UTF-16 units', () => {
  assert.equal(cpLength('abc'), 3);
  assert.equal(cpLength('a\u{1F600}b'), 3);
  assert.equal('a\u{1F600}b'.length, 4);
});

test('makeIndexer maps code-point offsets to string indices', () => {
  const plain = 'hello';
  assert.equal(makeIndexer(plain)(3), 3);
  const t = 'x\u{1F600}yz';
  const u = makeIndexer(t);
  assert.equal(t.slice(u(2), u(4)), 'yz');
  assert.equal(t.slice(u(1), u(2)), '\u{1F600}');
  assert.equal(u(99), t.length); // clamped
});

test('prepText normalises line endings and blank runs', () => {
  assert.equal(prepText('  a\r\nb\r\n\r\n\r\n\r\nc  '), 'a\nb\n\nc');
});

test('chunkText keeps parts under the limit and loses nothing', () => {
  const para = 'We may change these terms at any time. You agree to them by using the app. ';
  const text = Array.from({ length: 60 }, (_, i) => `${i + 1}. ${para.repeat(3)}`.trim()).join('\n\n');
  const parts = chunkText(text, 2000);
  assert.ok(parts.length > 1);
  for (const p of parts) assert.ok(p.length <= 2000, `part of ${p.length}`);
  assert.equal(parts.join('\n\n'), text);
});

test('chunkText splits a giant paragraph at sentences, then at spaces', () => {
  const sentence = 'You grant us a licence to your content. ';
  const text = sentence.repeat(400).trim();
  const parts = chunkText(text, 1000);
  for (const p of parts) assert.ok(p.length <= 1000);
  assert.equal(parts.join(' '), text);
  const words = 'word '.repeat(600).trim(); // no sentence ends at all
  const wp = chunkText(words, 500);
  for (const p of wp) assert.ok(p.length <= 500 && !p.startsWith(' ') && !p.endsWith(' '));
  assert.equal(wp.join(' '), words);
});

test('chunkText default part size and a short text in one part', () => {
  assert.equal(PART_CHARS, 10000);
  assert.deepEqual(chunkText('One clause.'), ['One clause.']);
  assert.deepEqual(chunkText(''), []);
});

test('looksLikeHtml needs real tags', () => {
  assert.ok(looksLikeHtml('<p>Terms</p><p>More</p>'));
  assert.ok(!looksLikeHtml('Use of the "Service" <is> governed by 1 < 2 > 0'));
  assert.ok(!looksLikeHtml('A single <b>bold'));
});

function part(text, segments, readings, extra = {}) {
  return {
    text,
    sha256: `h${text.length}`,
    segments,
    analysis: readings ? {
      source: 'model', model: 'claude-opus-5-5', readings, received: readings.length + 1,
      verified: readings.length, dropped: 1, dropped_reasons: { not_found: 1 }, relocated: 0,
    } : null,
    notice: null,
    budget: null,
    ...extra,
  };
}

test('combineParts shifts offsets and clause numbers across parts', () => {
  const a = part('Aa \u{1F600}. Bb.', [
    { kind: 'clause', id: 1, start: 0, end: 5 },
    { kind: 'clause', id: 2, start: 6, end: 9 },
  ], [{ id: 2, favours: 'you', confidence: 'high', reading: 'r', quote_start: 6, quote_end: 8, quote: 'Bb' }]);
  const b = part('Cc dd.', [{ kind: 'clause', id: 1, start: 0, end: 6 }],
    [{ id: 1, favours: 'them', confidence: 'low', reading: 'r', quote_start: 0, quote_end: 2, quote: 'Cc' }]);
  const doc = combineParts([a, b]);
  assert.equal(doc.text, 'Aa \u{1F600}. Bb.\n\nCc dd.');
  const u = makeIndexer(doc.text);
  for (const r of doc.readings) assert.equal(doc.text.slice(u(r.quote_start), u(r.quote_end)), r.quote);
  assert.deepEqual(doc.segments.map((s) => s.id), [1, 2, 3]);
  assert.deepEqual(doc.readings.map((r) => r.id), [2, 3]);
  assert.equal(doc.stats.dropped, 2);
  assert.deepEqual(doc.stats.reasons, { not_found: 2 });
  assert.equal(doc.unread, 1);
  assert.deepEqual(tally(doc.readings), { you: 1, them: 1, neutral: 0, unclear: 0 });
});

test('combineParts carries notices per part and the latest budget', () => {
  const a = part('One clause here.', [{ kind: 'clause', id: 1, start: 0, end: 16 }], null,
    { notice: { kind: 'budget_spent', message: 'spent' }, budget: { remaining: 0 } });
  const doc = combineParts([a]);
  assert.deepEqual(doc.notices, [{ part: 1, kind: 'budget_spent', message: 'spent' }]);
  assert.equal(doc.unread, 1);
  assert.deepEqual(doc.budget, { remaining: 0 });
});

test('guessTitle prefers a heading, else trims the first clause', () => {
  assert.equal(guessTitle({ text: 'TERMS\nYou agree.', segments: [{ kind: 'heading', start: 0, end: 5 }] }), 'TERMS');
  const long = 'By using this service you agree to all of the following terms and conditions without exception.';
  const t = guessTitle({ text: long, segments: [{ kind: 'clause', id: 1, start: 0, end: long.length }] });
  assert.ok(t.length <= 60 && t.endsWith('…'));
});
