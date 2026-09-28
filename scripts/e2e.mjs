// End-to-end check of the real Python Worker running locally in workerd
// (`pywrangler dev`), with no API key and no network:
//
//   1. demo mode (no key): the API's status, clause map without readings,
//      exhibits and input limits; and the page in headless Chrome at desktop
//      and 400 px phone width, where pasted text is read on the device by
//      the Python rule set (Pyodide), not sent to the Worker;
//   2. live mode against a LOCAL MOCK of the Anthropic Messages API: the
//      official SDK inside the Worker streams its real request from
//      127.0.0.1, the mock answers with server-sent events (one quote
//      deliberately wrong), and we check the request shape, quote
//      verification and dropping, the D1 cache, which failures refund the
//      budget and which count, partial failures in the page, the fallback
//      model display, and the Durable Object budget running out.
//
// Every server listens on a random free port, so parallel runs don't clash.
// Usage: npm run test:e2e   (builds dist/ first)
import { spawn, execFileSync } from 'node:child_process';
import { createServer, request as httpRequest } from 'node:http';
import { createServer as createNetServer } from 'node:net';
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

// Markers in the pasted text that make the mock misbehave.
const UPSTREAM_503 = 'E2E-UPSTREAM-503';
const CUT_STREAM = 'E2E-CUT-STREAM';
const FALLBACK = 'E2E-FALLBACK';

function freePort() {
  return new Promise((resolve, reject) => {
    const s = createNetServer();
    s.unref();
    s.on('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

// ------------------------------------------------------------ mock model
const mockCalls = [];

function sseEvents(model, blocks) {
  const events = [['message_start', { type: 'message_start', message: {
    id: 'msg_mock', type: 'message', role: 'assistant', model, content: [], stop_reason: null, stop_sequence: null,
    usage: { input_tokens: 1, output_tokens: 1 },
  } }]];
  blocks.forEach((b, index) => {
    if (b.type === 'text') {
      events.push(['content_block_start', { type: 'content_block_start', index, content_block: { type: 'text', text: '' } }]);
      for (let i = 0; i < b.text.length; i += 200) {
        events.push(['content_block_delta', { type: 'content_block_delta', index, delta: { type: 'text_delta', text: b.text.slice(i, i + 200) } }]);
      }
    } else if (b.type === 'thinking') {
      events.push(['content_block_start', { type: 'content_block_start', index, content_block: { type: 'thinking', thinking: '', signature: '' } }]);
      events.push(['content_block_delta', { type: 'content_block_delta', index, delta: { type: 'signature_delta', signature: 'mock' } }]);
    } else {
      events.push(['content_block_start', { type: 'content_block_start', index, content_block: b }]);
    }
    events.push(['content_block_stop', { type: 'content_block_stop', index }]);
  });
  events.push(['message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: 1 } }]);
  events.push(['message_stop', { type: 'message_stop' }]);
  return events.map(([name, data]) => `event: ${name}\ndata: ${JSON.stringify(data)}\n\n`);
}

function startMock() {
  const server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => {
      const json = JSON.parse(body);
      mockCalls.push({ path: req.url, headers: req.headers, body: json });
      const prompt = json.messages[0].content;
      if (prompt.includes(UPSTREAM_503)) {
        res.writeHead(503, { 'content-type': 'application/json' });
        res.end(JSON.stringify({ type: 'error', error: { type: 'api_error', message: 'mock outage' } }));
        return;
      }
      const clauses = [];
      for (const line of prompt.split('\n')) {
        const m = /^\[(\d+)\] (.*)$/.exec(line);
        if (!m) continue;
        const words = m[2].split(' ').slice(0, 6).join(' ');
        clauses.push({ id: Number(m[1]), quote: words, favours: ['them', 'you', 'neutral', 'unclear'][clauses.length % 4], reading: 'A mocked one-line reading.', confidence: 'medium' });
      }
      clauses[0].quote = 'words the document never says';
      const text = JSON.stringify({ clauses });
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
      if (prompt.includes(CUT_STREAM)) {
        // Open the stream, send the start of the reply, then drop the connection.
        const events = sseEvents(json.model, [{ type: 'text', text }]);
        res.write(events.slice(0, 3).join(''));
        setTimeout(() => res.socket.destroy(), 50);
        return;
      }
      const blocks = prompt.includes(FALLBACK)
        ? [{ type: 'fallback', from: { model: json.model }, to: { model: 'claude-opus-5' }, trigger: { type: 'refusal', category: null } }, { type: 'text', text }]
        : [{ type: 'thinking' }, { type: 'text', text }];
      res.end(sseEvents(json.model, blocks).join(''));
    });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

// ---------------------------------------------------------------- worker
async function startWorker(vars) {
  const port = await freePort();
  const inspector = await freePort();
  const args = ['run', 'pywrangler', 'dev', '--port', String(port), '--inspector-port', String(inspector), '--ip', '127.0.0.1', '--persist-to', STATE];
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
  // Only the process group this script started.
  try { process.kill(-w.proc.pid, 'SIGTERM'); } catch { /* gone */ }
}

async function api(base, path, body) {
  const res = await fetch(base + path, body === undefined ? {} : {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ text: body }),
  });
  return { status: res.status, json: await res.json() };
}

// A chunked upload (no Content-Length) on a one-off connection: the Worker
// answers before the whole body is sent, so that connection is not reusable.
function chunkedPost(base, bytes) {
  return new Promise((resolve, reject) => {
    const u = new URL(`${base}/api/analyze`);
    let answered = false;
    const req = httpRequest({
      host: u.hostname, port: u.port, path: u.pathname, method: 'POST', agent: false,
      headers: { 'content-type': 'application/json', 'transfer-encoding': 'chunked', connection: 'close' },
    }, (res) => {
      answered = true;
      let body = '';
      res.on('data', (d) => { body += d; });
      res.on('end', () => resolve({ status: res.statusCode, json: JSON.parse(body) }));
    });
    req.on('error', (e) => { if (!answered && !['EPIPE', 'ECONNRESET'].includes(e.code)) reject(e); });
    const chunk = Buffer.alloc(64 * 1024, 'x');
    let sent = 0;
    const pump = () => {
      while (sent < bytes) {
        sent += chunk.length;
        if (!req.write(chunk)) { req.once('drain', pump); return; }
      }
      req.end();
    };
    pump();
  });
}

async function inputLimits(base) {
  const empty = await fetch(`${base}/api/analyze`, { method: 'POST', headers: { 'content-type': 'application/json' } });
  assert.equal(empty.status, 400);
  const headings = await api(base, '/api/analyze', 'TERMS OF SERVICE\n\n1. Definitions');
  assert.equal(headings.status, 200);
  assert.equal(headings.json.notice.kind, 'no_clauses');
}

// ----------------------------------------------------------------- browser
async function paste(page, text) {
  await page.evaluate(`(() => { const t = document.querySelector('#paste'); t.value = ${JSON.stringify(text)}; t.dispatchEvent(new Event('input')); document.querySelector('#read').click(); return true; })()`);
}

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
    assert.equal(state, live ? 'live' : 'rules', `${plan.name}: service state`);
    if (live) {
      assert.match(await page.evaluate(`document.querySelector('#service-status').textContent`), /Claude Opus 5\.5 at medium effort; if Claude Opus 5\.5 declines a text, another Claude model may answer/);
    }

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

    // j / k move between clauses; the strip marks the selected clause.
    await page.evaluate(`document.activeElement.blur(), true`);
    await page.key('j');
    await page.key('j');
    await page.key('k');
    const active = await page.evaluate(`({ id: document.querySelector('#doc .cl.active')?.dataset.id, tick: !document.querySelector('#minimap .mm-tick').hidden })`);
    assert.deepEqual(active, { id: '1', tick: true }, `${plan.name}: j j k lands on clause 1, with a tick on the strip`);

    // Count requests to the analysis API: with no key there must be none.
    await page.evaluate(`(() => {
      const real = window.fetch; window.__analyzeCalls = 0;
      window.fetch = (url, opts) => { if (String(url).includes('/api/analyze')) window.__analyzeCalls += 1; return real(url, opts); };
      return true;
    })()`);
    // Paste HTML source: converted in the browser, then read (by the model
    // through the Worker in parts, or on this device by the rule set).
    const html = `<html><body><h1>Test Terms</h1><p>${demo('pacewren').split('\n').filter(Boolean).slice(3, 12).join('</p><p>')}</p><script>window.pwned = 1</script></body></html>`;
    await paste(page, html);
    await page.waitFor(`document.querySelector('#doc-title').textContent === 'Test Terms'`, 60000);
    const pasted = await page.evaluate(`({
      clauses: document.querySelectorAll('#doc .cl').length,
      marks: document.querySelectorAll('#doc mark.q').length,
      notices: document.querySelector('#notices').textContent,
      provenance: document.querySelector('#provenance').textContent,
      verification: document.querySelector('#verification').textContent,
      pwned: window.pwned === 1,
      analyzeCalls: window.__analyzeCalls,
      sw: document.documentElement.scrollWidth, iw: innerWidth,
    })`);
    assert.equal(pasted.pwned, false, 'pasted HTML must never execute');
    assert.ok(pasted.clauses > 5, 'pasted text is segmented');
    assert.ok(pasted.sw <= pasted.iw, `${plan.name}: no horizontal scroll after paste`);
    if (live) {
      assert.equal(pasted.marks, pasted.clauses - 1, 'all but the one bad quote are highlighted');
      assert.match(pasted.verification, /1 dropped \(1 not in the text word for word\)/);
      assert.match(pasted.provenance, /Claude Opus 5\.5/);
    } else {
      // No key: read on this device by the rule set, never sent to the Worker.
      assert.ok(pasted.marks > 0, 'the rule set marks some clauses');
      assert.match(pasted.provenance, /Rule-based reading, no AI/);
      assert.match(pasted.notices, /What rules can miss/);
      assert.equal(pasted.analyzeCalls, 0, 'the pasted text was not sent to the Worker');
    }

    // Bad input gets a clear message, and none of it logs a console error.
    await paste(page, 'x'.repeat(60001));
    await page.waitFor(`/limit is 60,000/.test(document.querySelector('#form-error').textContent)`);
    await paste(page, '​​ ﻿­');
    await page.waitFor(`/no readable text/.test(document.querySelector('#form-error').textContent)`);
    await paste(page, 'TERMS OF SERVICE\n\nPRIVACY POLICY');
    await page.waitFor(`/No clauses found/.test(document.querySelector('#form-error').textContent)`);

    if (live && plan.name === 'desktop') {
      // Partial failure: two parts, and the request for the second fails in
      // the browser. The first part still shows; the second offers a retry,
      // which reads it (via a refusal fallback, so part 2 names the fallback
      // model).
      const two = `${demo('brothbike')}\n\n${demo('pacewren')}\n\n${demo('nimbus-locker')}\n\n${FALLBACK} fallback terms follow.`;
      await page.evaluate(`(() => {
        const real = window.fetch; let n = 0;
        window.fetch = (url, opts) => (String(url).includes('/api/analyze') && ++n === 2
          ? Promise.reject(new TypeError('Failed to fetch')) : real(url, opts));
        return true;
      })()`);
      await paste(page, two);
      await page.waitFor(`document.querySelector('#part-2.failed') !== null`, 60000);
      const partial = await page.evaluate(`({
        rules: document.querySelectorAll('#doc .part-rule').length,
        failed: document.querySelector('#part-2 .part-error').textContent,
        read: document.querySelectorAll('#doc mark.q').length,
        notices: document.querySelector('#notices').textContent,
      })`);
      assert.equal(partial.rules, 2);
      assert.match(partial.failed, /could not be reached/);
      assert.ok(partial.read > 10, 'part 1 is shown, read');
      assert.match(partial.notices, /One part could not be read \(part 2\)/);
      await page.evaluate(`document.querySelector('#part-2 .retry').click(), true`);
      await page.waitFor(`document.querySelector('#part-2') && !document.querySelector('#part-2.failed')`, 60000);
      const healed = await page.evaluate(`({
        label: document.querySelector('#part-2 .part-label').textContent,
        provenance: document.querySelector('#provenance').textContent,
        ref: [...document.querySelectorAll('#doc .note-ref')].pop().textContent,
      })`);
      assert.match(healed.label, /read by Claude Opus 5$/);
      assert.match(healed.provenance, /Claude Opus 5\.5 \(part 1\) and Claude Opus 5 \(part 2\)/);
      assert.match(healed.provenance, /fallback model answered/);
      assert.match(healed.ref, /read by Claude Opus 5$/, 'a note in part 2 names the model that read it');
      results.partial = { ...partial, healed: healed.label };
    }

    assert.deepEqual(page.problems, [], `${plan.name}: console errors / exceptions`);
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
  try {
    await phases(chrome);
  } finally {
    if (chrome) await chrome.close(); // on failure too: never leave Chrome running
  }
  console.log('e2e: all checks passed');
}

async function phases(chrome) {
  const report = {};

  console.log('1. demo mode (no API key)');
  let w = await startWorker({});
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
    await inputLimits(w.base);
    const home = await fetch(`${w.base}/`);
    assert.equal(home.status, 200);
    const demoJson = await (await fetch(`${w.base}/demo/nimbus-locker.json`)).json();
    assert.equal(demoJson.analysis.verified, 46);
    log('API: status demo, clause map without readings, 422 on empty text, 400 on an empty body, headings-only answered 200, exhibits served');
    if (chrome) {
      report.demo = await browserChecks(chrome, w.base, { live: false });
      log('browser: exhibits, j/k with a strip tick, HTML paste read on the device by the rule set, bad input caught with no console errors, 1280 px and 400 px');
    }
    assert.ok(!w.output().includes(MOCK_KEY));
    // A chunked upload larger than the cap is refused while it is read.
    // Last request to this Worker on purpose: the local dev proxy cannot
    // reuse its connection after the Worker answers before reading the
    // whole body (it logs "Network connection lost" on the next request).
    const chunked = await chunkedPost(w.base, 640 * 1024);
    assert.equal(chunked.status, 413);
    assert.equal(chunked.json.error, 'too_large');
    log('API: a chunked upload over the cap is refused with 413 while it is read');
  } finally {
    stopWorker(w);
  }

  console.log('2. live mode against a local mock of the Messages API (streamed)');
  const mock = await startMock();
  const mockUrl = `http://127.0.0.1:${mock.address().port}`;
  const LIMIT = 7;
  w = await startWorker({ ANTHROPIC_API_KEY: MOCK_KEY, ANTHROPIC_BASE_URL: mockUrl, DAILY_BUDGET: String(LIMIT), PER_VISITOR_DAILY: String(LIMIT) });
  try {
    const status = await api(w.base, '/api/status');
    assert.equal(status.json.mode, 'live');
    assert.deepEqual([status.json.budget.limit, status.json.budget.used], [LIMIT, 0]);
    assert.ok(status.json.limits.timeout_s > 400, 'the page waits longer than the Worker can');

    const first = await api(w.base, '/api/analyze', demo('nimbus-locker'));
    const a = first.json.analysis;
    assert.ok(a, `no analysis: ${JSON.stringify(first.json.notice ?? first.json)}\n${w.output().split('Ready on')[1]?.slice(-2000)}`);
    assert.equal(a.source, 'model');
    assert.equal(a.model, 'claude-opus-5-5');
    assert.equal(a.received, 46);
    assert.equal(a.dropped, 1);
    assert.deepEqual(a.dropped_reasons, { not_found: 1 });
    assert.equal(a.verified, 45);
    for (const x of a.readings) assert.equal([...first.json.text].slice(x.quote_start, x.quote_end).join(''), x.quote);
    assert.equal(first.json.budget.used, 1);

    const call = mockCalls[0];
    assert.equal(call.body.stream, true, 'the request is streamed');
    assert.equal(call.body.model, 'claude-opus-5-5');
    assert.equal(call.body.output_config.effort, 'medium');
    assert.equal(call.body.output_config.format.type, 'json_schema');
    assert.equal(call.body.fallbacks, 'default');
    assert.equal(call.headers['anthropic-beta'], 'server-side-fallback-2026-07-01');
    assert.equal(call.headers['x-api-key'], MOCK_KEY);
    assert.ok(!('thinking' in call.body) && !('tool_choice' in call.body));
    log('SDK request from inside workerd: streamed; model, effort, schema, fallbacks, headers all as specified');

    const again = await api(w.base, '/api/analyze', `${demo('nimbus-locker')}\n\n\n`);
    assert.equal(again.json.analysis.source, 'cache');
    assert.equal(mockCalls.length, 1, 'cache hit makes no model call');
    assert.equal(again.json.budget.used, 1);
    log('D1 cache: same text (different whitespace) served from cache, no model call, no budget');

    let calls = mockCalls.length;
    const outage = await api(w.base, '/api/analyze', `${UPSTREAM_503}. We may change these terms whenever we like.`);
    assert.equal(outage.json.notice.kind, 'model_error');
    assert.equal(outage.json.budget.used, 1, 'a 5xx before the stream opened is refunded');
    assert.equal(mockCalls.length, calls + 1, 'and not retried');
    calls = mockCalls.length;
    const cut = await api(w.base, '/api/analyze', `${CUT_STREAM}. We may keep your files after you leave.`);
    assert.equal(cut.json.notice.kind, 'model_interrupted');
    assert.match(cut.json.notice.message, /counted against today's budget/);
    assert.equal(cut.json.budget.used, 2, 'a stream that broke after opening may be billed, so it counts');
    assert.equal(mockCalls.length, calls + 1, 'and is not retried');
    log('budget: a pre-generation 5xx is refunded; a stream cut mid-reply is counted; neither is retried');

    if (chrome) {
      report.live = await browserChecks(chrome, w.base, { live: true });
      log('browser: pasted HTML read via the mocked model, 1 dropped quote reported; a failed part shown with a retry that reads it via the fallback model');
    }
    // Spend the rest of the budget, then one more.
    const used = (await api(w.base, '/api/status')).json.budget.used;
    const texts = [
      demo('pacewren'),
      'You agree to pay a fee of ten euros every month for as long as you live.',
      'We may keep your data forever. You may not ask us to delete it.',
      'We may sell the service to anyone, and these terms go with it.',
      'You waive every right to a jury trial for any dispute with us.',
      'We can close your account without telling you why or when.',
    ];
    let spent = null;
    for (const t of texts) {
      const r = await api(w.base, '/api/analyze', t);
      if (r.json.notice?.kind === 'budget_spent') { spent = r.json; break; }
    }
    assert.ok(spent, `budget should run out (used ${used} of ${LIMIT} before)`);
    assert.equal(spent.analysis, null);
    assert.equal(spent.budget.remaining, 0);
    const callsAtLimit = mockCalls.length;
    const cachedAfter = await api(w.base, '/api/analyze', demo('nimbus-locker'));
    assert.equal(cachedAfter.json.analysis.source, 'cache', 'cached documents still work when the budget is spent');
    assert.equal(mockCalls.length, callsAtLimit);
    log(`Durable Object budget: stopped at ${LIMIT} of ${LIMIT} model calls; cached document still served`);

    // Runtime logs only: wrangler's own start-up banner lists --var values,
    // which is local tooling (in production the key is a secret and is never
    // printed). Everything the Worker itself logs comes after "Ready on".
    const all = w.output();
    const runtime = all.slice(all.indexOf('Ready on'));
    const leaks = runtime.split('\n').filter((l) => l.includes(MOCK_KEY) || l.includes('You keep ownership of everything you upload'));
    assert.deepEqual(leaks.map((l) => l.replaceAll(MOCK_KEY, '<key>')), [], 'Worker logs must not contain the key or document text');
    assert.ok(/"event": "analyze"/.test(runtime), 'the count-only log line is there');
    assert.ok(/"error": "interrupted"/.test(runtime) && /"error": "upstream"/.test(runtime), 'failures are logged with their kind');
    log('Worker logs: count summaries with the failure kind only, no key, no document text');
    report.modelCalls = mockCalls.length;
  } finally {
    stopWorker(w);
    mock.close();
  }
  writeFileSync(join(STATE, 'report.json'), JSON.stringify(report, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
