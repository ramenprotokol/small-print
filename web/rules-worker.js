// small print: the on-device reader. A module worker that runs the Python
// rule set (src/small_print/rules.py) in Pyodide, which is CPython compiled
// to WebAssembly. Pasted text is read here, in the visitor's browser; this
// worker makes no network request except for its own runtime files.
//
// In:  { type: 'read', id, text }
// Out: { type: 'progress', loaded }           bytes of the runtime received so far
//      { type: 'ready', python, pyodide, ms } the runtime is up
//      { type: 'result', id, json }           local.analyze_json(text)
//      { type: 'failed', id, stage, message } the runtime could not start ('start'), or Python raised ('read')
//
// The build (scripts/build.py) rewrites the three constants below to the
// content-hashed runtime folder, Python bundle and file sizes.
const RUNTIME = "./pyodide/";
const BUNDLE = "./small_print.zip";
const SIZES = { "pyodide.mjs": 0, "pyodide.asm.mjs": 0 };

let loaded = 0;
let lastPost = 0;

function progress(force = false) {
  const now = Date.now();
  if (!force && now - lastPost < 80) return;
  lastPost = now;
  self.postMessage({ type: 'progress', loaded });
}

// Count bytes as they arrive, for the progress bar: Pyodide fetches the
// WebAssembly binary, the standard library and its lock file with fetch().
const realFetch = self.fetch.bind(self);
self.fetch = async (input, init) => {
  const res = await realFetch(input, init);
  if (!res.ok || !res.body) return res;
  const counter = new TransformStream({
    transform(chunk, controller) {
      loaded += chunk.byteLength;
      progress();
      controller.enqueue(chunk);
    },
  });
  return new Response(res.body.pipeThrough(counter), { status: res.status, statusText: res.statusText, headers: res.headers });
};

async function start() {
  const t0 = performance.now();
  loaded = 0;
  // The two JavaScript modules are imported, not fetched: count them as
  // they land. Pyodide's own import of the second one reuses this copy.
  const { loadPyodide, version } = await import(`${RUNTIME}pyodide.mjs`);
  loaded += SIZES['pyodide.mjs'];
  await import(`${RUNTIME}pyodide.asm.mjs`);
  loaded += SIZES['pyodide.asm.mjs'];
  progress(true);
  const pyodide = await loadPyodide({ indexURL: new URL(RUNTIME, import.meta.url).href });
  const bundle = await (await fetch(new URL(BUNDLE, import.meta.url))).arrayBuffer();
  progress(true);
  pyodide.unpackArchive(bundle, 'zip', { extractDir: '/home/pyodide/sp' });
  const python = pyodide.runPython([
    'import sys',
    "sys.path.insert(0, '/home/pyodide/sp')",
    'from small_print import local',
    'sys.version.split()[0]',
  ].join('\n'));
  const analyze = pyodide.globals.get('local').analyze_json;
  self.postMessage({ type: 'ready', python, pyodide: version, ms: Math.round(performance.now() - t0) });
  return analyze;
}

let ready = null;

self.onmessage = async (event) => {
  const { type, id, text } = event.data || {};
  if (type !== 'read') return;
  let analyze;
  try {
    if (!ready) ready = start();
    analyze = await ready;
  } catch (err) {
    ready = null; // the next read starts the runtime again (its files are cached)
    self.postMessage({ type: 'failed', id, stage: 'start', message: String(err?.message || err).slice(0, 300) });
    return;
  }
  try {
    self.postMessage({ type: 'result', id, json: analyze(String(text)) });
  } catch (err) {
    self.postMessage({ type: 'failed', id, stage: 'read', message: String(err?.message || err).slice(0, 300) });
  }
};
