// Smoke test of the built dist/: files, references, headers, the Python
// runtime and its licence notices, demo data, and the page itself in
// headless Chrome, served as plain static files with dist/_headers applied
// (as on Pages with no API reachable). Run `npm run build` first (npm test does).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { findChrome, launchChrome } from './cdp.mjs';
import { serveDist } from './serve.mjs';

const DIST = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'dist');
const read = (p) => readFileSync(join(DIST, p), 'utf8');

test('dist/ exists with hashed assets that index.html references', () => {
  assert.ok(existsSync(join(DIST, 'index.html')), 'run npm run build first');
  const html = read('index.html');
  const refs = [...html.matchAll(/(?:href|src)="(assets\/[^"]+\.(?:js|css))"/g)].map((m) => m[1]);
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
  // WebAssembly may be compiled (the Python runtime); JavaScript eval may not.
  assert.match(h, /script-src 'self' 'wasm-unsafe-eval';/);
  assert.match(h, /worker-src 'self';/);
  assert.doesNotMatch(h, /unsafe-inline|'unsafe-eval'/);
  assert.match(h, /connect-src 'self'/);
  assert.match(h, /style-src 'self';/);
  assert.match(h, /font-src 'self';/);
  assert.match(h, /frame-ancestors 'none'/);
  const blocks = h.split(/\n(?=\S)/);
  const cached = blocks.filter((b) => /max-age=31536000/.test(b));
  assert.equal(cached.length, 1);
  assert.match(cached[0], /^\/assets\/\*/);
});

function runtime() {
  const html = read('index.html');
  const meta = /<meta name="sp-runtime" content="([^"]+)">/.exec(html);
  assert.ok(meta, 'the page names its runtime');
  const facts = Object.fromEntries(meta[1].split(' ').map((kv) => kv.split('=')));
  const worker = readdirSync(join(DIST, 'assets')).find((f) => /^rules-worker\.[0-9a-f]{10}\.js$/.test(f));
  const src = read(`assets/${worker}`);
  const folder = /const RUNTIME = "\.\/(pyodide-[0-9a-f]{10})\/";/.exec(src)?.[1];
  const bundle = /const BUNDLE = "\.\/(small_print\.[0-9a-f]{10}\.zip)";/.exec(src)?.[1];
  return { facts, worker, src, folder, bundle };
}

test('the on-device reader: Pyodide runtime and Python bundle, sizes stated on the page', () => {
  const { facts, src, folder, bundle } = runtime();
  assert.ok(folder && bundle, 'the worker names its runtime folder and bundle');
  const files = ['pyodide.mjs', 'pyodide.asm.mjs', 'pyodide.asm.wasm', 'python_stdlib.zip', 'pyodide-lock.json'];
  assert.deepEqual(readdirSync(join(DIST, 'assets', folder)).sort(), [...files].sort());
  let total = 0;
  for (const f of files) {
    const size = statSync(join(DIST, 'assets', folder, f)).size;
    assert.ok(size < 25 * 1024 * 1024, `${f} fits the Pages per-file limit`);
    total += size;
  }
  total += statSync(join(DIST, 'assets', bundle)).size;
  assert.equal(Number(facts.bytes), total, 'the size the page states is the real total');
  assert.equal(facts.pyodide, JSON.parse(readFileSync(join(DIST, '..', 'node_modules', 'pyodide', 'package.json'), 'utf8')).version);
  assert.match(facts.python, /^3\.14\.\d+$/);
  assert.ok(Number(facts.rules) >= 40);
  // The worker's byte counter knows the two imported modules' sizes.
  const sizes = JSON.parse(/const SIZES = (\{[^}]+\});/.exec(src)[1]);
  assert.equal(sizes['pyodide.asm.mjs'], statSync(join(DIST, 'assets', folder, 'pyodide.asm.mjs')).size);
  // The bundle holds the browser's Python only: never the model code.
  const names = new Set(readFileSync(join(DIST, 'assets', bundle), 'latin1').match(/small_print\/\w+\.py/g));
  assert.deepEqual([...names].sort(), ['__init__', 'local', 'normalise', 'rules', 'segment', 'verify'].map((n) => `small_print/${n}.py`));
  // local.js starts the hashed worker; app.js imports the hashed local.js.
  const assets = readdirSync(join(DIST, 'assets'));
  const local = assets.find((f) => /^local\.[0-9a-f]{10}\.js$/.test(f));
  assert.match(read(`assets/${local}`), new RegExp(`new URL\\("\\./${runtime().worker.replace('.', '\\.')}"`));
});

test('fonts are served from this site, hashed, with their licences: no request to Google', () => {
  const html = read('index.html');
  const css = read(html.match(/href="(assets\/style\.[0-9a-f]{10}\.css)"/)[1]);
  for (const [name, body] of [['index.html', html], ['style.css', css], ['_headers', read('_headers')]]) {
    assert.doesNotMatch(body, /googleapis|gstatic|fonts\.google/, `${name} names no Google Fonts host`);
  }
  const faces = [...css.matchAll(/@font-face\s*\{([^}]+)\}/g)].map((m) => m[1]);
  assert.equal(faces.length, 5, 'Libre Caslon Text (regular, italic, bold) and Public Sans (variable, italic)');
  const files = faces.map((f) => /url\("([^"]+)"\)/.exec(f)[1]);
  for (const f of files) {
    assert.match(f, /^[A-Za-z-]+\.[0-9a-f]{10}\.woff2$/, `${f} is hashed and sits beside the stylesheet`);
    assert.ok(existsSync(join(DIST, 'assets', f)), `${f} ships`);
    assert.equal(readFileSync(join(DIST, 'assets', f)).subarray(0, 4).toString('latin1'), 'wOF2');
  }
  for (const f of faces) assert.match(f, /font-display:\s*swap/);
  // The two faces the masthead needs first are preloaded, by their hashed names.
  const preloads = [...html.matchAll(/<link rel="preload" href="assets\/([^"]+)" as="font" type="font\/woff2" crossorigin>/g)].map((m) => m[1]);
  assert.equal(preloads.length, 2);
  for (const p of preloads) assert.ok(files.includes(p), `${p} is one of the faces the stylesheet declares`);
  const n = read('THIRD-PARTY-NOTICES.txt');
  for (const needle of ['Libre Caslon Text', 'Public Sans', 'SIL OPEN FONT LICENSE Version 1.1', 'The Libre Caslon Text Project Authors', 'The Public Sans Project Authors']) {
    assert.ok(n.includes(needle), needle);
  }
});

test('THIRD-PARTY-NOTICES.txt ships with every runtime component and its licence', () => {
  assert.ok(existsSync(join(DIST, 'THIRD-PARTY-NOTICES.txt')));
  const n = read('THIRD-PARTY-NOTICES.txt');
  const { facts, folder } = runtime();
  assert.ok(n.includes(`Pyodide ${facts.pyodide}`) && n.includes(`CPython ${facts.python}`) && n.includes(folder));
  for (const needle of [
    'Mozilla Public License Version 2.0',
    'PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2',
    'Licenses and Acknowledgements for Incorporated Software',
    'HACL* Contributors',
    'Emscripten authors',
    'Rich Felker',
    'Pierre Curto',
    'For Zstandard software',
    'Julian R Seward',
    'SQLite',
    'XZ Utils',
  ]) assert.ok(n.includes(needle), needle);
  assert.match(read('index.html'), /href="THIRD-PARTY-NOTICES\.txt"/, 'the colophon links the notices');
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

const chromePath = findChrome();

test('the static page loads and the exhibits work with no API at all', { skip: !chromePath && 'Chrome not found (set CHROME_PATH)' }, async () => {
  const server = await serveDist(DIST);
  const chrome = await launchChrome(chromePath);
  try {
    for (const plan of [
      { width: 1280, height: 800, scheme: 'light' },
      { width: 400, height: 860, mobile: true, scale: 2, scheme: 'dark' },
    ]) {
      const page = await chrome.openPage(plan);
      await page.navigate(`http://127.0.0.1:${server.address().port}/`);
      await page.waitFor(`document.querySelector('#service-status').dataset.state === 'rules'`);
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
      // With no API, pasted text is read on this device by the rule set.
      await page.evaluate(`(() => { const t = document.querySelector('#paste'); t.value = 'We may terminate your account at any time.'; t.dispatchEvent(new Event('input')); document.querySelector('#read').click(); return true; })()`);
      await page.waitFor(`/Rule-based reading, no AI/.test(document.querySelector('#provenance').textContent)`, 60000);
      assert.equal(await page.evaluate(`document.querySelector('#doc mark.q').textContent`), 'We may terminate your account at any time');
      // Only the expected failed /api/status request may log: nothing else is fetched off this server.
      const unexpected = page.problems.filter((p) => !/\/api\/status|404/.test(`${p.text} ${p.url ?? ''}`));
      assert.deepEqual(unexpected, []);
      await page.close();
    }
  } finally {
    await chrome.close();
    server.close();
  }
});
