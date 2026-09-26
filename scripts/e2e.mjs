// End-to-end check of the real Python Worker running locally in workerd
// (`pywrangler dev`), with no API key and no network:
//
//   1. demo mode (no key): status, clause map without readings, exhibits,
//      and the page in headless Chrome at desktop and 400 px phone width;
//   2. live mode against a LOCAL MOCK of the Anthropic Messages API: the
//      official SDK inside the Worker sends its real request to 127.0.0.1,
//      the mock answers with one deliberately wrong quote, and we check the
//      request shape, quote verification and dropping, the D1 cache, and the
//      Durable Object budget running out.
//
// Usage: npm run test:e2e   (builds dist/ first)
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { findChrome, launchChrome } from '../tests/js/cdp.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const STATE = join(ROOT, '.e2e');
const demo = (slug) => readFileSync(join(ROOT, 'demo', `${slug}.txt`), 'utf8');
const MOCK_KEY = 'e2e-mock-key-not-a-real-key';
const log = (...a) => console.log('  ', ...a);

// ------------------------------------------------------------ mock model
const mockCalls = [];
function startMock() {
  const server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const json = JSON.parse(body);
      mockCalls.push({ path: req.url, headers: req.headers, body: json });
      const prompt = json.messages[0].content;
      const clauses = [];
      for (const line of prompt.split('\n')) {
        const m = /^\[(\d+)\] (.*)$/.exec(line);
        if (!m) continue;
        const words = m[2].split(' ').slice(0, 6).join(' ');
        clauses.push({ id: Number(m[1]), quote: words, favours: ['them', 'you', 'neutral', 'unclear'][clauses.length % 4], reading: 'A mocked one-line reading.', confidence: 'medium' });
      }
      clauses[0].quote = 'words the document never says';
      const reply = {
        id: 'msg_mock', type: 'message', role: 'assistant', model: json.model,
        content: [{ type: 'thinking', thinking: '', signature: 'mock' }, { type: 'text', text: JSON.stringify({ clauses }) }],
        stop_reason: 'end_turn', stop_sequence: null, usage: { input_tokens: 1, output_tokens: 1 },
      };
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(reply));
    });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// ---------------------------------------------------------------- worker
async function startWorker(port, vars) {
  const args = ['run', 'pywrangler', 'dev', '--port', String(port), '--ip', '127.0.0.1', '--persist-to', STATE];
  for (const [k, v] of Object.entries(vars)) args.push('--var', `${k}:${v}`);
  const proc = spawn('uv', args, { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], detached: true });
  let out = '';
  proc.stdout.on('data', (d) => { out += d; });
  proc.stderr.on('data', (d) => { out += d; });
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    if (/Ready on/.test(out)) break;
    if (proc.exitCode !== null) throw new Error(`worker exited:\n${out}`);
    await new Promise((r) => setTimeout(r, 250));
  }
  if (!/Ready on/.test(out)) throw new Error(`worker did not start:\n${out}`);
  return { proc, output: () => out, base: `http://127.0.0.1:${port}` };
}

function stopWorker(w) {
  try { process.kill(-w.proc.pid, 'SIGTERM'); } catch { /* gone */ }
}

async function api(base, path, body) {
  const res = await fetch(base + path, body === undefined ? {} : {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: body }),
  });
  return { status: res.status, json: await res.json() };
}

// ----------------------------------------------------------------- browser
const EXPECTED_OFFLINE = (p) => /fonts\.(googleapis|gstatic)\.com/.test(`${p.text} ${p.url ?? ''}`);

async function browserChecks(chrome, base, { live }) {
  const results = {};
  for (const plan of [
    { name: 'desktop', width: 1280, height: 800, scheme: 'light' },
    { name: 'phone', width: 400, height: 860, mobile: true, scale: 2, scheme: 'dark', reducedMotion: true },
  ]) {
    const page = await chrome.openPage(plan);
    await page.navigate(`${base}/`);
    await page.waitFor(`document.querySelectorAll('.exhibit').length === 3 && document.querySelector('#service-status').dataset.state !== 'checking'`);
    const state = await page.evaluate(`document.querySelector('#service-status').dataset.state`);
    assert.equal(state, live ? 'live' : 'demo', `${plan.name}: service state`);

    // Exhibit A: every hand-written reading renders as a highlight + note.
    await page.evaluate(`document.querySelector('.exhibit').click(), true`);
    await page.waitFor(`document.querySelectorAll('#doc .note').length === 46`);
    const exhibit = await page.evaluate(`({
      marks: document.querySelectorAll('#doc mark.q').length,
      clauses: document.querySelectorAll('#doc .cl').length,
      verification: document.querySelector('#verification').textContent,
      sw: document.documentElement.scrollWidth, iw: innerWidth,
    })`);
    assert.equal(exhibit.marks, 46);
    assert.equal(exhibit.clauses, 46);
    assert.match(exhibit.verification, /46 quotes checked word for word/);
    assert.ok(exhibit.sw <= exhibit.iw, `${plan.name}: no horizontal scroll (${exhibit.sw} > ${exhibit.iw})`);

    // j / k move between clauses.
    await page.evaluate(`document.activeElement.blur(), true`);
    await page.key('j');
    await page.key('j');
    await page.key('k');
    const active = await page.evaluate(`document.querySelector('#doc .cl.active')?.dataset.id`);
    assert.equal(active, '1', `${plan.name}: j j k lands on clause 1`);

    // Paste HTML source: converted in the browser, sent in parts, rendered.
    const html = `<html><body><h1>Test Terms</h1><p>${demo('pacewren').split('\n').filter(Boolean).slice(3, 12).join('</p><p>')}</p><script>window.pwned = 1</script></body></html>`;
    await page.evaluate(`(() => { const t = document.querySelector('#paste'); t.value = ${JSON.stringify(html)}; t.dispatchEvent(new Event('input')); return true; })()`);
    await page.evaluate(`document.querySelector('#read').click(), true`);
    await page.waitFor(`document.querySelector('#doc-title').textContent === 'Test Terms'`, 60000);
    const pasted = await page.evaluate(`({
      clauses: document.querySelectorAll('#doc .cl').length,
      marks: document.querySelectorAll('#doc mark.q').length,
      notices: document.querySelector('#notices').textContent,
      verification: document.querySelector('#verification').textContent,
      pwned: window.pwned === 1,
      sw: document.documentElement.scrollWidth, iw: innerWidth,
    })`);
    assert.equal(pasted.pwned, false, 'pasted HTML must never execute');
    assert.ok(pasted.clauses > 5, 'pasted text is segmented');
    assert.ok(pasted.sw <= pasted.iw, `${plan.name}: no horizontal scroll after paste`);
    if (live) {
      assert.equal(pasted.marks, pasted.clauses - 1, 'all but the one bad quote are highlighted');
      assert.match(pasted.verification, /1 dropped \(1 not in the text word for word\)/);
    } else {
      assert.equal(pasted.marks, 0);
      assert.match(pasted.notices, /demo mode/i);
    }

    // Bad input gets a clear message.
    await page.evaluate(`(() => { const t = document.querySelector('#paste'); t.value = 'x'.repeat(60001); t.dispatchEvent(new Event('input')); document.querySelector('#read').click(); return true; })()`);
    await page.waitFor(`!document.querySelector('#form-error').hidden`);
    assert.match(await page.evaluate(`document.querySelector('#form-error').textContent`), /limit is 60,000/);

    const problems = page.problems.filter((p) => !EXPECTED_OFFLINE(p));
    assert.deepEqual(problems, [], `${plan.name}: console errors / exceptions`);
    results[plan.name] = { exhibit, pasted: { clauses: pasted.clauses, marks: pasted.marks } };
    await page.close();
  }
  return results;
}

// -------------------------------------------------------------------- main
async function main() {
  rmSync(STATE, { recursive: true, force: true });
  mkdirSync(STATE, { recursive: true });
  execFileSync('npx', ['wrangler', 'd1', 'migrations', 'apply', 'small-print-cache', '--local', '--persist-to', STATE], { cwd: ROOT, stdio: 'ignore' });
  const chromePath = findChrome();
  const chrome = chromePath ? await launchChrome(chromePath) : null;
  if (!chrome) log('Chrome not found: skipping the browser part (set CHROME_PATH).');
  const report = {};

  console.log('1. demo mode (no API key)');
  let w = await startWorker(8799, {});
  try {
    const status = await api(w.base, '/api/status');
    assert.equal(status.json.mode, 'demo');
    assert.equal(status.json.budget, null);
    const r = await api(w.base, '/api/analyze', demo('brothbike'));
    assert.equal(r.status, 200);
    assert.equal(r.json.analysis, null);
    assert.equal(r.json.notice.kind, 'demo_mode');
    assert.equal(r.json.segments.filter((s) => s.kind === 'clause').length, 40);
    const bad = await api(w.base, '/api/analyze', ' ');
    assert.equal(bad.status, 422);
    const home = await fetch(`${w.base}/`);
    assert.equal(home.status, 200);
    const demoJson = await (await fetch(`${w.base}/demo/nimbus-locker.json`)).json();
    assert.equal(demoJson.analysis.verified, 46);
    log('API: status demo, clause map without readings, 422 on empty text, exhibits served');
    if (chrome) {
      report.demo = await browserChecks(chrome, w.base, { live: false });
      log('browser: exhibits, j/k, HTML paste, bad input, 1280 px and 400 px, no console errors');
    }
    assert.ok(!w.output().includes(MOCK_KEY));
  } finally {
    stopWorker(w);
  }

  console.log('2. live mode against a local mock of the Messages API');
  const mock = await startMock();
  const mockUrl = `http://127.0.0.1:${mock.address().port}`;
  w = await startWorker(8798, { ANTHROPIC_API_KEY: MOCK_KEY, ANTHROPIC_BASE_URL: mockUrl, DAILY_BUDGET: '4', PER_VISITOR_DAILY: '4' });
  try {
    const status = await api(w.base, '/api/status');
    assert.equal(status.json.mode, 'live');
    assert.deepEqual([status.json.budget.limit, status.json.budget.used], [4, 0]);

    const first = await api(w.base, '/api/analyze', demo('nimbus-locker'));
    const a = first.json.analysis;
    assert.equal(a.source, 'model');
    assert.equal(a.received, 46);
    assert.equal(a.dropped, 1);
    assert.deepEqual(a.dropped_reasons, { not_found: 1 });
    assert.equal(a.verified, 45);
    for (const x of a.readings) assert.equal([...first.json.text].slice(x.quote_start, x.quote_end).join(''), x.quote);
    assert.equal(first.json.budget.used, 1);

    const call = mockCalls[0];
    assert.equal(call.body.model, 'claude-opus-5-5');
    assert.equal(call.body.output_config.effort, 'medium');
    assert.equal(call.body.output_config.format.type, 'json_schema');
    assert.equal(call.body.fallbacks, 'default');
    assert.equal(call.headers['anthropic-beta'], 'server-side-fallback-2026-07-01');
    assert.equal(call.headers['x-api-key'], MOCK_KEY);
    assert.ok(!('thinking' in call.body) && !('tool_choice' in call.body));
    log('SDK request from inside workerd: model, effort, schema, fallbacks, headers all as specified');

    const again = await api(w.base, '/api/analyze', `${demo('nimbus-locker')}\n\n\n`);
    assert.equal(again.json.analysis.source, 'cache');
    assert.equal(mockCalls.length, 1, 'cache hit makes no model call');
    assert.equal(again.json.budget.used, 1);
    log('D1 cache: same text (different whitespace) served from cache, no model call, no budget');

    if (chrome) {
      report.live = await browserChecks(chrome, w.base, { live: true });
      log('browser: pasted HTML read via the mocked model, 1 dropped quote reported');
    }
    // Spend the rest of the budget, then one more.
    const used = (await api(w.base, '/api/status')).json.budget.used;
    const texts = [demo('pacewren'), demo('brothbike'), 'You agree to pay a fee of ten euros every month for as long as you live.', 'We may keep your data forever. You may not ask us to delete it.'];
    let spent = null;
    for (const t of texts) {
      const r = await api(w.base, '/api/analyze', t);
      if (r.json.notice?.kind === 'budget_spent') { spent = r.json; break; }
    }
    assert.ok(spent, `budget should run out (used ${used} of 4 before)`);
    assert.equal(spent.analysis, null);
    assert.equal(spent.budget.remaining, 0);
    const callsAtLimit = mockCalls.length;
    const cachedAfter = await api(w.base, '/api/analyze', demo('nimbus-locker'));
    assert.equal(cachedAfter.json.analysis.source, 'cache', 'cached documents still work when the budget is spent');
    assert.equal(mockCalls.length, callsAtLimit);
    log(`Durable Object budget: stopped at 4 of 4 model calls; cached document still served`);

    // Runtime logs only: wrangler's own start-up banner lists --var values,
    // which is local tooling (in production the key is a secret and is never
    // printed). Everything the Worker itself logs comes after "Ready on".
    const all = w.output();
    const runtime = all.slice(all.indexOf('Ready on'));
    const leaks = runtime.split('\n').filter((l) => l.includes(MOCK_KEY) || l.includes('You keep ownership of everything you upload'));
    assert.deepEqual(leaks.map((l) => l.replaceAll(MOCK_KEY, '<key>')), [], 'Worker logs must not contain the key or document text');
    assert.ok(/"event": "analyze"/.test(runtime), 'the count-only log line is there');
    log('Worker logs: count summaries only, no key, no document text');
    report.modelCalls = mockCalls.length;
  } finally {
    stopWorker(w);
    mock.close();
    if (chrome) await chrome.close();
  }
  writeFileSync(join(STATE, 'report.json'), JSON.stringify(report, null, 2));
  console.log('e2e: all checks passed');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
