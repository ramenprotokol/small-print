import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  PART_CHARS,
  chunkText,
  combineParts,
  cpLength,
  findingsByRule,
  formatBytes,
  guessTitle,
  hasReadableText,
  looksLikeHtml,
  makeIndexer,
  markRuns,
  modelName,
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
  // Page and paragraph separators become breaks, as on the Worker.
  assert.equal(prepText('a\fb\u2029c\u2028d'), 'a\n\nb\n\nc\nd');
});

test('hasReadableText rejects pastes of only spaces and invisible characters', () => {
  assert.equal(hasReadableText('\u200b\ufeff \u00ad\n\t\u00a0\u2060\u3000'), false);
  assert.equal(hasReadableText(''), false);
  assert.equal(hasReadableText('\u200bTerms'), true);
});

test('modelName gives a friendly name for a model id', () => {
  assert.equal(modelName('claude-opus-5-5'), 'Claude Opus 5.5');
  assert.equal(modelName('claude-opus-5'), 'Claude Opus 5');
  assert.equal(modelName('claude-opus-4-8'), 'Claude Opus 4.8');
  assert.equal(modelName('claude-sonnet-5'), 'Claude Sonnet 5');
  assert.equal(modelName('some-other-model'), 'some-other-model');
  assert.equal(modelName(null), 'the model');
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

function part(text, segments, readings, extra = {}, model = 'claude-opus-5-5') {
  return {
    text,
    sha256: `h${text.length}`,
    segments,
    analysis: readings ? {
      source: 'model', model, readings, received: readings.length + 1,
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
  assert.equal(doc.parts.length, 2);
  assert.deepEqual(doc.parts.map((p) => p.segStart), [0, 2]);
});

test('combineParts keeps the model that read each part', () => {
  const a = part('One clause here.', [{ kind: 'clause', id: 1, start: 0, end: 16 }],
    [{ id: 1, favours: 'you', confidence: 'high', reading: 'r', quote_start: 0, quote_end: 10, quote: 'One clause' }]);
  const b = part('Two clause here.', [{ kind: 'clause', id: 1, start: 0, end: 16 }],
    [{ id: 1, favours: 'them', confidence: 'high', reading: 'r', quote_start: 0, quote_end: 10, quote: 'Two clause' }], {}, 'claude-opus-5');
  const doc = combineParts([a, b]);
  assert.deepEqual(doc.parts.map((p) => p.model), ['claude-opus-5-5', 'claude-opus-5']);
  assert.deepEqual(doc.readings.map((r) => [r.id, r.model, r.part]), [[1, 'claude-opus-5-5', 1], [2, 'claude-opus-5', 2]]);
  assert.deepEqual(doc.models, ['claude-opus-5-5', 'claude-opus-5']);
});

test('combineParts keeps the parts that worked when one part failed', () => {
  const a = part('Aa one. Bb two.', [
    { kind: 'clause', id: 1, start: 0, end: 7 },
    { kind: 'clause', id: 2, start: 8, end: 15 },
  ], [{ id: 2, favours: 'you', confidence: 'high', reading: 'r', quote_start: 8, quote_end: 14, quote: 'Bb two' }]);
  const failed = { failed: true, message: 'The reading service could not be reached.', text: 'Middle part text.' };
  const c = part('Cc three.', [{ kind: 'clause', id: 1, start: 0, end: 9 }],
    [{ id: 1, favours: 'them', confidence: 'low', reading: 'r', quote_start: 0, quote_end: 8, quote: 'Cc three' }]);
  const doc = combineParts([a, failed, c]);
  assert.equal(doc.text, 'Aa one. Bb two.\n\nCc three.');
  const u = makeIndexer(doc.text);
  for (const r of doc.readings) assert.equal(doc.text.slice(u(r.quote_start), u(r.quote_end)), r.quote);
  assert.deepEqual(doc.segments.map((s) => s.id), [1, 2, 3]);
  assert.equal(doc.failed, 1);
  const mid = doc.parts[1];
  assert.equal(mid.failed, true);
  assert.equal(mid.segStart, 2); // rendered between part 1 and part 3
  assert.equal(mid.message, failed.message);
  assert.equal(mid.text, failed.text);
  assert.deepEqual(doc.keys, ['h15', null, 'h9']);
});

test('combineParts marks parts whose model call can be tried again', () => {
  const busy = part('One clause here.', [{ kind: 'clause', id: 1, start: 0, end: 16 }], null,
    { notice: { kind: 'model_timeout', message: 'too long' } });
  const spent = part('Two clause here.', [{ kind: 'clause', id: 1, start: 0, end: 16 }], null,
    { notice: { kind: 'budget_spent', message: 'spent' } });
  const doc = combineParts([busy, spent]);
  assert.deepEqual(doc.parts.map((p) => !!p.retryable), [true, false]);
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

test('markRuns splits a clause into plain and highlighted runs, first mark wins on overlap', () => {
  const runs = markRuns(10, 40, [{ start: 12, end: 20 }, { start: 15, end: 30 }, { start: 35, end: 50 }]);
  assert.deepEqual(runs, [
    { start: 10, end: 12, mark: -1 },
    { start: 12, end: 20, mark: 0 },
    { start: 20, end: 30, mark: 1 },
    { start: 30, end: 35, mark: -1 },
    { start: 35, end: 40, mark: 2 }, // clipped to the clause
  ]);
  assert.deepEqual(markRuns(0, 5, []), [{ start: 0, end: 5, mark: -1 }]);
  assert.deepEqual(markRuns(0, 5, [{ start: 0, end: 5 }, { start: 1, end: 2 }]), [{ start: 0, end: 5, mark: 0 }]);
  // Runs always tile the clause exactly.
  const tiled = markRuns(3, 90, [{ start: 50, end: 60 }, { start: 1, end: 7 }, { start: 58, end: 95 }]);
  assert.equal(tiled[0].start, 3);
  assert.equal(tiled.at(-1).end, 90);
  tiled.slice(1).forEach((r, i) => assert.equal(r.start, tiled[i].end));
});

test('combineParts keeps rule findings, engine and checklist, with offsets shifted', () => {
  const part = (text, extra = {}) => ({
    text,
    sha256: 'x',
    segments: [{ kind: 'clause', start: 0, end: text.length, id: 1 }],
    analysis: {
      source: 'rules', model: null, received: 2, verified: 2, dropped: 0, dropped_reasons: {}, relocated: 0,
      readings: [{ id: 1, favours: 'them', confidence: 'high', reading: 'r', quote_start: 0, quote_end: 4, rule: 'a', topic: 'A',
        findings: [{ rule: 'a', topic: 'A', favours: 'them', quote_start: 0, quote_end: 4 }, { rule: 'b', topic: 'B', favours: 'you', quote_start: 5, quote_end: 9 }] }],
      engine: { rules: 51 }, checked: [{ rule: 'a' }, { rule: 'b' }],
      ...extra,
    },
    notice: null,
  });
  const doc = combineParts([part('abcd efgh'), part('ijkl mnop')]);
  assert.equal(doc.engine.rules, 51);
  assert.equal(doc.checked.length, 2);
  const second = doc.readings[1];
  assert.equal(second.id, 2);
  assert.deepEqual(second.findings.map((f) => [f.quote_start, f.quote_end]), [[11, 15], [16, 20]]);
  assert.equal(doc.text.slice(second.findings[1].quote_start, second.findings[1].quote_end), 'mnop');
  assert.equal(doc.stats.verified, 4);
});

test('findingsByRule indexes every finding by rule, in document order', () => {
  const readings = [
    { id: 3, findings: [{ rule: 'arbitration', topic: 'Forced arbitration', favours: 'them' }, { rule: 'jury_waiver', topic: 'Jury-trial waiver', favours: 'them' }] },
    { id: 7, findings: [{ rule: 'arbitration', topic: 'Forced arbitration', favours: 'them' }] },
    { id: 9, favours: 'you' }, // a model reading: no findings
  ];
  assert.deepEqual(findingsByRule(readings), [
    { rule: 'arbitration', topic: 'Forced arbitration', favours: 'them', ids: [3, 7] },
    { rule: 'jury_waiver', topic: 'Jury-trial waiver', favours: 'them', ids: [3] },
  ]);
});

test('formatBytes reports decimal megabytes like a browser does', () => {
  assert.equal(formatBytes(13555056), '13.6 MB');
  assert.equal(formatBytes(119077), '119 kB');
  assert.equal(formatBytes(10), '1 kB');
  assert.equal(formatBytes(-1), '');
});
