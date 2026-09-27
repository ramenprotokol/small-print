// The Python the browser runs gives the same answers as the Python the
// tests run. The built bundle (dist/assets/small_print.*.zip) is loaded into
// Pyodide under Node, from node_modules, and its output is compared with
// CPython's (through uv) for the same inputs, offsets included. Timings are
// reported as diagnostics, not asserted.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadPyodide } from 'pyodide';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const DIST = join(ROOT, 'dist');
const read = (...p) => readFileSync(join(ROOT, ...p), 'utf8');

const nimbus = read('demo', 'nimbus-locker.txt');
const maxSize = Array.from({ length: Math.floor(60000 / (nimbus.length + 2)) }, () => nimbus).join('\n\n');
const hostile = 'we may suspend you agree to indemnify share your personal data with partners at any time '.repeat(700).slice(0, 60000);
const INPUTS = {
  quillfeather: read('tests', 'fixtures', 'quillfeather-terms.txt'),
  'nimbus-locker': read('demo', 'nimbus-locker.txt'),
  pacewren: read('demo', 'pacewren.txt'),
  brothbike: read('demo', 'brothbike.txt'),
  'max-size': maxSize,
  unicode: 'Terms \u{1F600} “for you”. We may sell your personal information to data brokers.\r\nYou can​ cancel at any time.',
  hostile,
};

function comparable(json) {
  const out = JSON.parse(json);
  if (out.analysis) {
    delete out.analysis.engine.python;
    delete out.analysis.engine.elapsed_ms;
  }
  return out;
}

const uv = spawnSync('uv', ['--version']).status === 0;

test('Pyodide and CPython read every input identically', { skip: !uv && 'uv not found', timeout: 120000 }, async (t) => {
  const bundle = readdirSync(join(DIST, 'assets')).find((f) => /^small_print\.[0-9a-f]{10}\.zip$/.test(f));
  assert.ok(bundle, 'run npm run build first');

  const t0 = performance.now();
  const pyodide = await loadPyodide({ indexURL: join(ROOT, 'node_modules', 'pyodide') + '/' });
  pyodide.unpackArchive(new Uint8Array(readFileSync(join(DIST, 'assets', bundle))), 'zip', { extractDir: '/home/pyodide/sp' });
  pyodide.runPython("import sys; sys.path.insert(0, '/home/pyodide/sp'); from small_print import local");
  const analyze = pyodide.globals.get('local').analyze_json;
  t.diagnostic(`Pyodide started in ${Math.round(performance.now() - t0)} ms (Node, files on local disk)`);

  const cpython = spawnSync('uv', ['run', '--quiet', 'python', '-c', [
    'import json, sys',
    "sys.path.insert(0, 'src')",
    'from small_print import local',
    'inputs = json.load(sys.stdin)',
    'print(json.dumps({k: local.analyze_json(v) for k, v in inputs.items()}))',
  ].join('\n')], { cwd: ROOT, input: JSON.stringify(INPUTS), encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  assert.equal(cpython.status, 0, cpython.stderr);
  const reference = JSON.parse(cpython.stdout);

  for (const [name, text] of Object.entries(INPUTS)) {
    const t1 = performance.now();
    const got = analyze(text);
    const ms = performance.now() - t1;
    const out = JSON.parse(got);
    t.diagnostic(`${name}: ${out.analysis?.readings.length ?? 0} readings, ${Math.round(ms)} ms in Pyodide (Python's own timer: ${out.analysis?.engine.elapsed_ms} ms)`);
    assert.deepEqual(comparable(got), comparable(reference[name]), name);
    assert.match(out.analysis.engine.python, /^3\.14\./);
  }
});
