// Smoke test of the built dist/: files, references, headers, demo data, and
// the page itself in headless Chrome, served as plain static files (as on
// Pages with no API reachable). Run `npm run build` first (npm test does).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join, dirname, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';
import { findChrome, launchChrome } from './cdp.mjs';

const DIST = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dist');
const read = (p) => readFileSync(join(DIST, p), 'utf8');

test('dist/ exists with hashed assets that index.html references', () => {
  assert.ok(existsSync(join(DIST, 'index.html')), 'run npm run build first');
  const html = read('index.html');
  const refs = [...html.matchAll(/(?:href|src)="(assets\/[^"]+)"/g)].map((m) => m[1]);
  assert.equal(refs.length, 2, 'one stylesheet and one module script');
  for (const r of refs) {
    assert.match(r, /^assets\/(app|style)\.[0-9a-f]{10}\.(js|css)$/);
    assert.ok(existsSync(join(DIST, r)), `${r} exists`);
  }
  const app = refs.find((r) => r.endsWith('.js'));
  const lib = /from "\.\/(lib\.[0-9a-f]{10}\.js)"/.exec(read(app));
  assert.ok(lib && existsSync(join(DIST, 'assets', lib[1])), 'app imports the hashed lib');
  assert.deepEqual(readdirSync(DIST).filter((f) => f.endsWith('.js')), [], 'no unhashed scripts at the root');
});

test('_headers: strict CSP, long cache only for hashed assets', () => {
  const h = read('_headers');
  assert.match(h, /script-src 'self';/);
  assert.doesNotMatch(h, /unsafe-inline|unsafe-eval/);
  assert.match(h, /connect-src 'self'/);
  assert.match(h, /frame-ancestors 'none'/);
  const blocks = h.split(/\n(?=\S)/);
  const cached = blocks.filter((b) => /max-age=31536000/.test(b));
  assert.equal(cached.length, 1);
  assert.match(cached[0], /^\/assets\/\*/);
});

test('demo data: every quote is the exact text at its offsets', () => {
  const index = JSON.parse(read('demo/index.json'));
  assert.equal(index.length, 3);
  for (const d of index) {
    const doc = JSON.parse(read(`demo/${d.slug}.json`));
    const cps = [...doc.text];
    assert.equal(doc.analysis.source, 'demo');
    assert.equal(doc.analysis.dropped, 0);
    assert.equal(doc.analysis.verified, d.clauses);
    for (const r of doc.analysis.readings) {
      assert.equal(cps.slice(r.quote_start, r.quote_end).join(''), r.quote);
      const seg = doc.segments.find((s) => s.id === r.id);
      assert.ok(seg.start <= r.quote_start && r.quote_end <= seg.end, 'quote sits inside its clause');
    }
    assert.match(doc.text, /fictional document/);
  }
});

function serve(dir) {
  const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.txt': 'text/plain' };
  const server = createServer((req, res) => {
    const path = normalize(decodeURIComponent(new URL(req.url, 'http://x').pathname)).replace(/^(\.\.[/\\])+/, '');
    let file = join(dir, path);
    if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
    if (!file.startsWith(dir) || !existsSync(file)) {
      res.writeHead(404, { 'content-type': 'text/plain' });
      res.end('not found');
      return;
    }
    res.writeHead(200, { 'content-type': types[extname(file)] || 'application/octet-stream' });
    res.end(readFileSync(file));
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

const chromePath = findChrome();

test('the static page loads and the exhibits work with no API at all', { skip: !chromePath && 'Chrome not found (set CHROME_PATH)' }, async () => {
  const server = await serve(DIST);
  const chrome = await launchChrome(chromePath);
  try {
    for (const plan of [
      { width: 1280, height: 800, scheme: 'light' },
      { width: 400, height: 860, mobile: true, scale: 2, scheme: 'dark' },
    ]) {
      const page = await chrome.openPage(plan);
      await page.navigate(`http://127.0.0.1:${server.address().port}/`);
      await page.waitFor(`document.querySelector('#service-status').dataset.state === 'offline'`);
      await page.waitFor(`document.querySelectorAll('.exhibit').length === 3`);
      await page.evaluate(`document.querySelectorAll('.exhibit')[2].click(), true`);
      await page.waitFor(`document.querySelectorAll('#doc .note').length === 40`);
      const r = await page.evaluate(`({
        title: document.querySelector('#doc-title').textContent,
        marks: document.querySelectorAll('#doc mark.q').length,
        tally: [...document.querySelectorAll('.tally-btn')].map((b) => b.textContent),
        advice: document.querySelector('.not-advice').textContent,
        strip: document.querySelectorAll('#minimap .mm-block').length,
        sw: document.documentElement.scrollWidth, iw: innerWidth,
      })`);
      assert.equal(r.title, 'Exhibit C: Brothbike');
      assert.equal(r.marks, 40);
      assert.equal(r.strip, 40);
      assert.match(r.advice, /Not legal advice/i);
      assert.ok(r.sw <= r.iw, `no horizontal scroll at ${plan.width}px`);
      // Pasting while the API is unreachable gives a clear message.
      await page.evaluate(`(() => { const t = document.querySelector('#paste'); t.value = 'You agree to these terms.'; t.dispatchEvent(new Event('input')); document.querySelector('#read').click(); return true; })()`);
      await page.waitFor(`!document.querySelector('#form-error').hidden`);
      assert.match(await page.evaluate(`document.querySelector('#form-error').textContent`), /out of reach/);
      // Only the expected failed /api/status request (and fonts, if offline) may log.
      const unexpected = page.problems.filter((p) => !/\/api\/status|404|fonts\.(googleapis|gstatic)/.test(`${p.text} ${p.url ?? ''}`));
      assert.deepEqual(unexpected, []);
      await page.close();
    }
  } finally {
    await chrome.close();
    server.close();
  }
});
