// The on-device reader, main-thread side. It owns the module worker that
// runs Python (Pyodide) with the rule set, and bounds it: the runtime
// download may be slow but must keep moving, and a reading must finish in
// READ_TIMEOUT_MS. A worker that breaks either limit is stopped, so a hostile
// paste can never hang the page; the next reading starts a fresh worker
// (the runtime files then come from the browser's cache).
const READ_TIMEOUT_MS = 20000;
const STALL_MS = 60000;

export function canReadLocally() {
  return typeof Worker === 'function' && typeof WebAssembly === 'object';
}

export function createLocalReader(url = new URL("./rules-worker.js", import.meta.url)) {
  let worker = null;
  let nextId = 0;
  let runtime = null; // { python, pyodide, ms } once the runtime is up
  let job = null; // the one reading in flight

  function stop() {
    if (worker) worker.terminate();
    worker = null;
    runtime = null;
  }

  function finish(err, value) {
    if (!job) return;
    const { resolve, reject, timer } = job;
    clearTimeout(timer);
    job = null;
    if (err) reject(err);
    else resolve(value);
  }

  function watchdog(ms, message) {
    clearTimeout(job.timer);
    job.timer = setTimeout(() => {
      stop();
      finish(new Error(message));
    }, ms);
  }

  function spawn() {
    worker = new Worker(url, { type: 'module' });
    worker.onmessage = (event) => {
      const m = event.data || {};
      if (!job) return;
      if (m.type === 'progress') {
        job.onProgress?.({ stage: 'download', loaded: m.loaded });
        watchdog(STALL_MS, 'The Python runtime stopped downloading. Check the connection and try again.');
      } else if (m.type === 'ready') {
        runtime = { python: m.python, pyodide: m.pyodide, ms: m.ms };
        job.onProgress?.({ stage: 'reading', runtime });
        watchdog(READ_TIMEOUT_MS, 'Reading this text took too long, so it was stopped.');
      } else if (m.type === 'result' && m.id === job.id) {
        let payload;
        try {
          payload = JSON.parse(m.json);
        } catch {
          finish(new Error('The on-device reader returned something unreadable.'));
          return;
        }
        if (payload.error) finish(new Error(payload.message || 'The text could not be read.'));
        else finish(null, { payload, runtime });
      } else if (m.type === 'failed' && m.id === job.id) {
        if (m.stage === 'start') stop();
        finish(new Error(m.stage === 'start'
          ? 'The Python runtime could not start in this browser.'
          : 'The rule set failed on this text.'));
      }
    };
    worker.onerror = (event) => {
      event.preventDefault?.();
      stop();
      finish(new Error('The on-device reader could not start in this browser.'));
    };
  }

  return {
    get runtime() {
      return runtime;
    },
    read(text, onProgress) {
      if (job) return Promise.reject(new Error('A reading is already running.'));
      return new Promise((resolve, reject) => {
        const id = ++nextId;
        job = { id, resolve, reject, onProgress, timer: 0 };
        try {
          if (!worker) spawn();
        } catch {
          stop();
          finish(new Error('The on-device reader could not start in this browser.'));
          return;
        }
        if (runtime) {
          onProgress?.({ stage: 'reading', runtime });
          watchdog(READ_TIMEOUT_MS, 'Reading this text took too long, so it was stopped.');
        } else {
          onProgress?.({ stage: 'download', loaded: 0 });
          watchdog(STALL_MS, 'The Python runtime did not start downloading. Check the connection and try again.');
        }
        worker.postMessage({ type: 'read', id, text });
      });
    },
  };
}
