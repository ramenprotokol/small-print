// WCAG contrast of the theme tokens in web/style.css, both themes: body and
// caption text must reach AA (4.5:1), including text on every highlighter
// ink and on each clause wash (washes are composited over the paper).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const css = readFileSync(join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'web', 'style.css'), 'utf8');

function block(selector) {
  const i = css.indexOf(selector);
  assert.ok(i >= 0, selector);
  const start = css.indexOf('{', i);
  return css.slice(start + 1, css.indexOf('}', start));
}

function tokens(body) {
  const out = {};
  for (const m of body.matchAll(/--([\w-]+):\s*([^;]+);/g)) out[m[1]] = m[2].trim();
  return out;
}

const light = tokens(block(':root {'));
const dark = { ...light, ...tokens(block(':root[data-theme="dark"]')) };
const darkMedia = { ...light, ...tokens(block(':root:not([data-theme="light"])')) };

function parse(c) {
  let m = /^#([0-9a-f]{6})$/i.exec(c);
  if (m) return [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)).concat(1);
  m = /^rgba?\(([^)]+)\)$/.exec(c);
  if (m) {
    const [r, g, b, a = '1'] = m[1].split(',').map((s) => s.trim());
    return [Number(r), Number(g), Number(b), Number(a)];
  }
  throw new Error(`unparsed colour ${c}`);
}

const over = (top, bottom) => {
  const a = top[3];
  return [0, 1, 2].map((i) => top[i] * a + bottom[i] * (1 - a)).concat(1);
};
const lum = ([r, g, b]) => {
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
};
const ratio = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
};

for (const [name, t] of [['light', light], ['dark', dark], ['dark (system)', darkMedia]]) {
  test(`${name}: text tokens reach AA`, () => {
    const paper = parse(t.paper);
    const desk = parse(t.desk);
    const pairs = {
      'ink on paper': [parse(t.ink), paper],
      'muted on paper': [parse(t.muted), paper],
      'ink on desk': [parse(t.ink), desk],
      'muted on desk': [parse(t.muted), desk],
      'notice on paper': [parse(t.notice), paper],
      'paper on ink (primary button)': [paper, parse(t.ink)],
    };
    for (const k of ['them', 'you', 'unclear', 'neutral']) {
      pairs[`quote text on ${k} ink`] = [parse(t['on-mark']), over(parse(t[`mk-${k}`]), paper)];
      pairs[`clause text on ${k} wash`] = [parse(t.ink), over(parse(t[`wash-${k}`]), paper)];
      pairs[`muted clause number on ${k} wash`] = [parse(t.muted), over(parse(t[`wash-${k}`]), paper)];
    }
    for (const [label, [fg, bg]] of Object.entries(pairs)) {
      const r = ratio(fg, bg);
      assert.ok(r >= 4.5, `${name}: ${label} is ${r.toFixed(2)}:1`);
    }
  });
}

test('dark tokens are identical under the media query and the manual toggle', () => {
  assert.deepEqual(tokens(block(':root[data-theme="dark"]')), tokens(block(':root:not([data-theme="light"])')));
});
