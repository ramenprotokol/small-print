// End to end: the rule-based reader in a real browser. The built dist/ is
// served locally with its _headers applied (so the strict CSP is enforced),
// the Python runtime is streamed slowly enough to watch the progress bar,
// and a synthetic Terms of Service is pasted. Nothing reaches the network:
// every request must be a GET to this local server (fonts aside).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { findChrome, launchChrome } from './cdp.mjs';
import { serveDist } from './serve.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const DIST = join(ROOT, 'dist');
const TERMS = readFileSync(join(ROOT, 'tests', 'fixtures', 'quillfeather-terms.txt'), 'utf8');
const chromePath = findChrome();
const skip = !chromePath && 'Chrome not found (set CHROME_PATH)';
// The only console noise allowed: the page asks /api/status (there is no
// API on a static host, so this server answers 404) and loads Google Fonts.
const expected = (p) => /\/api\/status|fonts\.(googleapis|gstatic)\.com/.test(`${p.text} ${p.url ?? ''}`);

function facts() {
  const meta = /<meta name="sp-runtime" content="([^"]+)">/.exec(readFileSync(join(DIST, 'index.html'), 'utf8'));
  return Object.fromEntries(meta[1].split(' ').map((kv) => kv.split('=')));
}

const paste = (page, text) => page.evaluate(`(() => {
  const t = document.querySelector('#paste');
  t.value = ${JSON.stringify(text)};
  t.dispatchEvent(new Event('input'));
  document.querySelector('#read').click();
  return true;
})()`);

test('pasted terms are read on this device by Python, with verified quotes', { skip, timeout: 240000 }, async () => {
  const requests = [];
  const server = await serveDist(DIST, { slow: 25, log: (path, method) => requests.push(`${method} ${path}`) });
  const chrome = await launchChrome(chromePath);
  const base = `http://127.0.0.1:${server.address().port}/`;
  const { pyodide, bytes } = facts();
  try {
    const page = await chrome.openPage({ width: 1280, height: 800, scheme: 'light' });
    await page.navigate(base);
    await page.waitFor(`document.querySelector('#service-status').dataset.state === 'rules'`);
    const status = await page.evaluate(`document.querySelector('#service-status').textContent`);
    assert.match(status, /Rule-based reading, no AI\./);
    assert.match(status, /never leaves this device/);
    assert.match(status, /The rules read English only\./);
    assert.ok(status.includes(`${(Number(bytes) / 1e6).toFixed(1)} MB`), 'the first-load size is stated before anything downloads');
    assert.ok(!requests.some((r) => r.includes('pyodide')), 'the runtime is not fetched until a reading is asked for');

    // First reading: the runtime downloads with a progress bar, then Python reads.
    await paste(page, TERMS);
    await page.waitFor(`Number(document.querySelector('#progress .meter')?.getAttribute('aria-valuenow')) > 0`, 60000);
    const midway = await page.evaluate(`document.querySelector('#progress').innerText`);
    assert.match(midway, /Python runtime/);
    assert.match(midway, /MB of \d+\.\d MB/);
    await page.waitFor(`/Rule-based reading, no AI/.test(document.querySelector('#provenance').textContent)`, 120000);
    const r = await page.evaluate(`({
      provenance: document.querySelector('#provenance').textContent,
      verification: document.querySelector('#verification').textContent,
      notices: document.querySelector('#notices').textContent,
      marks: [...document.querySelectorAll('#doc mark.q')].map((m) => m.textContent),
      notes: document.querySelectorAll('#doc .note').length,
      clauses: document.querySelectorAll('#doc .cl').length,
      unmarked: document.querySelectorAll('#doc .cl[data-fav="none"]').length,
      checklist: document.querySelector('#checklist').textContent,
      tally: document.querySelector('#tally').textContent,
      history: document.querySelector('#history-list').textContent,
    })`);
    assert.ok(r.provenance.includes(`Pyodide ${pyodide}`), r.provenance);
    assert.match(r.provenance, /Read by Python 3\.14\.\d+ .* in this browser in \d[\d,]* ms; the text did not leave this device\./);
    const m = /^(\d+) quotes checked word for word against the text, each inside its own clause, and kept; 0 dropped\./.exec(r.verification);
    assert.ok(m, r.verification);
    // Every matched clause has its quote highlighted; a second quote that
    // overlaps the first shares its highlight.
    assert.ok(r.marks.length >= r.notes && r.marks.length <= Number(m[1]), `${r.marks.length} highlights for ${m[1]} quotes`);
    const flat = TERMS.replace(/\s+/g, ' ');
    for (const q of r.marks) assert.ok(flat.includes(q), `highlight "${q}" is verbatim text`);
    assert.equal(r.notes, r.clauses - r.unmarked, 'one margin note per matched clause');
    assert.match(r.notices, /What rules can miss\./);
    assert.match(r.notices, /in English only/);
    assert.match(r.checklist, /Forced arbitration/);
    assert.match(r.checklist, /Looked for, not found:/);
    assert.match(r.tally, /No rule matched/);
    assert.match(r.history, /QUILLFEATHER NOTES/);
    // The reading lands on the start of the result, "Not legal advice" first:
    // the progress box above it must be gone before the page scrolls there.
    await page.waitFor(`new Promise((done) => { const y = scrollY; setTimeout(() => done(scrollY === y && y > 0), 400); })`);
    const top = await page.evaluate(`Math.round(document.querySelector('#result').getBoundingClientRect().top)`);
    assert.ok(Math.abs(top) <= 2, `the result starts at the top of the screen (${top} px)`);

    // Specific clauses: the label, the note's topic, and a second finding.
    const clause = (needle) => page.evaluate(`(() => {
      const c = [...document.querySelectorAll('#doc .cl')].find((x) => x.textContent.includes(${JSON.stringify(needle)}));
      const n = c && document.getElementById('n' + c.dataset.id);
      return c && { id: c.dataset.id, fav: c.dataset.fav, mark: c.querySelector('mark.q')?.textContent, topic: n?.querySelector('.note-topic')?.textContent, also: n?.querySelector('.note-also')?.textContent || '' };
    })()`);
    assert.deepEqual(await clause('settled exclusively through'), { id: (await clause('settled exclusively through')).id, fav: 'them', mark: 'binding arbitration', topic: 'Forced arbitration.', also: '' });
    const never = await clause('We never sell');
    assert.equal(never.fav, 'you');
    assert.equal(never.topic, 'They do not sell your data.');
    const waiver = await clause('individual basis');
    assert.equal(waiver.topic, 'Class-action waiver.');
    assert.match(waiver.also, /Jury-trial waiver \(favours them\)/);
    assert.equal(await page.evaluate(`document.querySelectorAll('#c${waiver.id} mark').length`), 2, 'both quotes in that clause are highlighted');
    assert.equal((await clause('Write to legal@')).fav, 'none');

    // The checklist jumps to a clause the rule found.
    await page.evaluate(`[...document.querySelectorAll('.check-btn')].find((b) => b.dataset.rule === 'jury_waiver').click(), true`);
    assert.equal(await page.evaluate(`document.querySelector('#doc .cl.active')?.dataset.id`), waiver.id);

    // Second reading: the runtime is already up, so no download and no meter.
    const before = requests.length;
    await paste(page, 'You must be 18 or older to use Quillfeather. We may sell your personal information to data brokers.');
    await page.waitFor(`document.querySelectorAll('#doc .cl').length === 2`, 20000);
    assert.ok(!requests.slice(before).some((x) => x.includes('pyodide') || x.includes('.zip')), 'no second download');
    assert.deepEqual(await page.evaluate(`[...document.querySelectorAll('#doc .cl')].map((c) => c.dataset.fav)`), ['neutral', 'them']);

    // Bad input gets a clear message.
    await paste(page, 'x'.repeat(60001));
    await page.waitFor(`/limit is 60,000/.test(document.querySelector('#form-error').textContent)`);
    await paste(page, 'TERMS OF SERVICE\n\nPRIVACY POLICY');
    await page.waitFor(`/No clauses found/.test(document.querySelector('#form-error').textContent)`, 20000);
    // Pasted HTML is converted in an inert document: its script never runs.
    await paste(page, '<html><body><h1>Pasted Terms</h1><p>We may change these Terms at any time.</p><p>You can cancel at any time.</p><script>window.pwned = 1</script></body></html>');
    await page.waitFor(`document.querySelector('#doc-title').textContent === 'Pasted Terms'`, 20000);
    assert.equal(await page.evaluate(`window.pwned === 1`), false);
    assert.match(await page.evaluate(`document.querySelector('#notices').textContent`), /converted from HTML/);

    // Nothing left this device: every request was a GET for the site's own files.
    assert.ok(requests.every((x) => x.startsWith('GET ')), requests.filter((x) => !x.startsWith('GET ')).join(', '));
    assert.ok(!requests.some((x) => x.includes('/api/analyze')));
    assert.deepEqual(page.problems.filter((p) => !expected(p)), [], 'no console errors, no CSP violations');
    await page.close();

    // Phone width, dark, reduced motion: the runtime comes from the cache.
    const phone = await chrome.openPage({ width: 400, height: 860, mobile: true, scale: 2, scheme: 'dark', reducedMotion: true });
    await phone.navigate(base);
    await phone.waitFor(`document.querySelector('#service-status').dataset.state === 'rules'`);
    await paste(phone, TERMS);
    await phone.waitFor(`/Rule-based reading, no AI/.test(document.querySelector('#provenance').textContent)`, 120000);
    const p = await phone.evaluate(`({ sw: document.documentElement.scrollWidth, iw: innerWidth, notes: document.querySelectorAll('#doc .note').length })`);
    assert.ok(p.sw <= p.iw, `no horizontal scroll at 400 px (${p.sw} > ${p.iw})`);
    assert.equal(p.notes, r.notes);
    assert.deepEqual(phone.problems.filter((x) => !expected(x)), []);
    await phone.close();
  } finally {
    await chrome.close();
    server.close();
  }
});
