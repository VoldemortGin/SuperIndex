#!/usr/bin/env node
/**
 * Render the diagrams that README.md shares with docs/ArchitectureIntro.html.
 *
 * Why this exists
 * ---------------
 * README.md cannot reuse the page's `<svg>` blocks directly: GitHub strips
 * inline SVG out of Markdown, so the only thing that survives is an `<img>`
 * pointing at a real file. Rasterising is therefore unavoidable — but redrawing
 * the diagrams by hand would create a second copy that drifts.
 *
 * So the PNGs here are *derived* from the page. The hand-written SVG in
 * ArchitectureIntro.html stays the single source of truth; re-run this script
 * after editing a figure and the README follows.
 *
 * Both colour schemes are rendered. GitHub serves the dark variant to readers
 * whose system is in dark mode, via
 * `<picture><source media="(prefers-color-scheme: dark)">`.
 *
 * Usage
 * -----
 *   # 1. start Chrome with a debugging port
 *   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
 *     --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
 *     --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-profile about:blank
 *
 *   # 2. render (from anywhere; paths are resolved from this file)
 *   node docs/diagrams/render.mjs
 *
 * Needs Node 18+ (global fetch and WebSocket) and Chrome. No npm packages.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PAGE = path.resolve(HERE, '..', 'ArchitectureIntro.html');
const PORT = 9222;

/**
 * Which figures the README uses, and what to call the file.
 *
 * `match` is a distinctive fragment of the figure's caption, deliberately not a
 * figure number: numbers shift when the page is reordered, and a silent
 * re-point of a README image at the wrong diagram would be worse than a crash.
 * A `match` that does not hit exactly one figure is a hard error.
 */
const FIGURES = [
  { match: 'two retrieval strategies', name: 'vector-versus-structure' },
  { match: 'a score versus a path', name: 'score-versus-path' },
  { match: 'degrades in a useful order', name: 'extraction-and-build' },
  { match: 'Three model decisions', name: 'addressing-funnel' },
  { match: 'adds a layer above', name: 'layer-not-a-fork' },
  { match: 'bounded number of model calls', name: 'retrieval-sequence' },
  { match: 'its own pass/fail counter', name: 'test-suite' },
  { match: 'Measured, not estimated', name: 'measured-latency' },
];

// Clip scale 2 against deviceScaleFactor 1 — a 2x raster. The page is 1440 CSS
// px wide and figures are ~900, so this lands near 1800 px: sharp on a HiDPI
// display, and small enough that GitHub's own downscale stays crisp.
const SCALE = 2;
const SCHEMES = [
  { emulated: 'light', suffix: '' },
  { emulated: 'dark', suffix: '-dark' },
];

function die(msg) {
  console.error(msg);
  process.exit(1);
}

async function connect() {
  const url = `http://127.0.0.1:${PORT}/json/new?about:blank`;
  let target;
  try {
    target = await (await fetch(url, { method: 'PUT' })).json();
  } catch {
    die(
      `Cannot reach Chrome on 127.0.0.1:${PORT}.\n\n` +
        'Start it first:\n' +
        '  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\\n' +
        '    --headless=new --disable-gpu --no-sandbox --hide-scrollbars \\\n' +
        `    --remote-debugging-port=${PORT} --user-data-dir=/tmp/chrome-profile about:blank`
    );
  }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  const pending = new Map();
  let id = 0;
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error('WebSocket to Chrome failed'));
  });
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    const p = pending.get(m.id);
    if (!p) return;
    pending.delete(m.id);
    m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
  };
  return { send, close: () => ws.close() };
}

/**
 * Figure boxes in document order, each with its caption.
 *
 * The box is the whole `<figure>`, not just the `<svg>`: the card background,
 * border and gradient rule all live on the figure element, so clipping the svg
 * alone would drop the diagram onto whatever background the reader's client
 * uses — unreadable in dark mode.
 */
const FIGURE_BOXES = `(() => {
  return [...document.querySelectorAll('figure')].map((f, i) => {
    const r = f.getBoundingClientRect();
    const cap = f.querySelector('figcaption');
    return {
      i,
      caption: cap ? cap.textContent.replace(/\\s+/g, ' ').trim() : '',
      x: r.left + scrollX, y: r.top + scrollY,
      w: r.width, h: r.height,
    };
  });
})()`;

/**
 * Hide the captions before capturing.
 *
 * The page's captions are written for the page: they carry its figure numbers
 * and refer to "everything else on this page", both of which dangle inside a
 * README. README.md supplies its own captions instead. Hiding rather than
 * cropping keeps the figure's border, radius and gradient rule intact — the
 * card simply ends after the diagram.
 *
 * `display:none` leaves textContent readable, so caption matching still works.
 */
const HIDE_CAPTIONS = `(() => {
  const s = document.createElement('style');
  s.textContent = 'figure figcaption{display:none}';
  document.head.appendChild(s);
})()`;


async function main() {
  if (!fs.existsSync(PAGE)) die(`Page not found: ${PAGE}`);
  const { send, close } = await connect();

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440,
    height: 1100,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await send('Page.navigate', { url: pathToFileURL(PAGE).href });
  await new Promise((r) => setTimeout(r, 1200));
  await send('Runtime.evaluate', { expression: HIDE_CAPTIONS });

  // Resolve every `match` against the page *before* writing anything. A stale
  // match must not leave a half-updated set of images behind: the failure mode
  // we care about is a README that quietly points at the wrong diagram.
  const probe = (await send('Runtime.evaluate', {
    expression: FIGURE_BOXES,
    returnByValue: true,
  })).result.value;

  const hits = new Map(FIGURES.map((f) => [f.name, 0]));
  for (const box of probe) {
    const figure = FIGURES.find((f) => box.caption.includes(f.match));
    if (figure) hits.set(figure.name, hits.get(figure.name) + 1);
  }
  const unresolved = [...hits].filter(([, n]) => n !== 1);
  if (unresolved.length) {
    die(
      'Figure matching failed — nothing written. Fix FIGURES[] first:\n' +
        unresolved
          .map(([n, c]) =>
            c === 0
              ? `  ${n}: no figure caption contains that text (was the caption edited?)`
              : `  ${n}: matched ${c} figures — the fragment is not distinctive enough`
          )
          .join('\n')
    );
  }

  const written = [];
  for (const scheme of SCHEMES) {
    await send('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-color-scheme', value: scheme.emulated }],
    });
    // Let the stylesheet's media query settle before measuring.
    await new Promise((r) => setTimeout(r, 150));

    const boxes = (await send('Runtime.evaluate', {
      expression: FIGURE_BOXES,
      returnByValue: true,
    })).result.value;

    for (const box of boxes) {
      const figure = FIGURES.find((f) => box.caption.includes(f.match));
      if (!figure) continue;

      const shot = await send('Page.captureScreenshot', {
        format: 'png',
        captureBeyondViewport: true,
        clip: {
          x: box.x - 8,
          y: box.y - 8,
          width: box.w + 16,
          height: box.h + 16,
          scale: SCALE,
        },
      });
      const file = path.join(HERE, `${figure.name}${scheme.suffix}.png`);
      fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
      written.push({
        name: `${figure.name}${scheme.suffix}.png`,
        scheme: scheme.emulated,
        kb: Math.round(fs.statSync(file).size / 1024),
      });
    }
  }

  close();

  console.log(`${written.length} files written to ${path.relative(process.cwd(), HERE)}/`);
  for (const w of written) console.log(`  ${w.name.padEnd(38)} ${w.scheme.padEnd(6)} ${w.kb} KB`);
}

await main();
