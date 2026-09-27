// A static server for dist/ that behaves like Cloudflare Pages where it
// matters to the tests: it applies the headers in dist/_headers (so the real
// Content-Security-Policy is enforced), serves .mjs, .wasm and .zip with the
// right types, and answers 404 for unknown paths. Listens on a random port.
import { createServer } from 'node:http';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { extname, join, normalize } from 'node:path';

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.txt': 'text/plain; charset=utf-8',
  '.wasm': 'application/wasm',
  '.zip': 'application/zip',
};

// dist/_headers: blocks of "<path pattern>" then indented "Name: value".
// Supported patterns: an exact path, or a prefix ending in "*".
export function parseHeaders(text) {
  const rules = [];
  let current = null;
  for (const line of text.split('\n')) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    if (!/^\s/.test(line)) {
      current = { pattern: line.trim(), headers: {} };
      rules.push(current);
    } else if (current) {
      const i = line.indexOf(':');
      current.headers[line.slice(0, i).trim().toLowerCase()] = line.slice(i + 1).trim();
    }
  }
  return (path) => {
    const out = {};
    for (const r of rules) {
      const hit = r.pattern.endsWith('*') ? path.startsWith(r.pattern.slice(0, -1)) : path === r.pattern;
      if (hit) Object.assign(out, r.headers);
    }
    return out;
  };
}

// `slow`: stream files over 1 MB in 256 kB chunks, `slow` ms apart, so a
// test can watch download progress.
export function serveDist(dir, { log, slow = 0 } = {}) {
  const headersFile = join(dir, '_headers');
  const headersFor = existsSync(headersFile) ? parseHeaders(readFileSync(headersFile, 'utf8')) : () => ({});
  const server = createServer((req, res) => {
    const path = new URL(req.url, 'http://x').pathname;
    log?.(path, req.method);
    let file = join(dir, normalize(decodeURIComponent(path)).replace(/^(\.\.[/\\])+/, ''));
    if (existsSync(file) && statSync(file).isDirectory()) file = join(file, 'index.html');
    if (!file.startsWith(dir) || !existsSync(file) || file.endsWith('_headers')) {
      res.writeHead(404, { 'content-type': 'text/plain' });
      res.end('not found');
      return;
    }
    const body = readFileSync(file);
    res.writeHead(200, { 'content-type': TYPES[extname(file)] || 'application/octet-stream', ...headersFor(path) });
    if (!slow || body.length < 1e6) {
      res.end(body);
      return;
    }
    let at = 0;
    const pump = () => {
      if (res.destroyed) return;
      res.write(body.subarray(at, at + 262144));
      at += 262144;
      if (at < body.length) setTimeout(pump, slow);
      else res.end();
    };
    pump();
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}
