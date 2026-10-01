# docs/diagrams

PNG copies of the figures that `README.md` shares with
[`../ArchitectureIntro.html`](../ArchitectureIntro.html).

## Why these exist

GitHub strips inline `<svg>` out of Markdown, so a README cannot embed the
diagrams directly — the only thing that survives sanitising is an `<img>`
pointing at a real file. Rather than redraw them (which would create a second
copy to keep in sync), they are **rendered from the page**: the hand-written SVG
in `ArchitectureIntro.html` stays the single source of truth.

Each figure exists twice, `name.png` and `name-dark.png`. The README picks
between them with `<picture><source media="(prefers-color-scheme: dark)">`, so
the diagrams follow the reader's theme the same way the page does.

## Regenerating

After editing a figure in `ArchitectureIntro.html`, re-render:

```bash
# 1. Chrome, with a debugging port
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless=new --disable-gpu --no-sandbox --hide-scrollbars \
  --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-profile about:blank

# 2. Render both colour schemes (needs Node 18+, no npm packages)
node docs/diagrams/render.mjs
```

Figures are matched by a fragment of their **caption text**, not by figure
number — numbers shift when the page is reordered, and an image that silently
points at the wrong diagram is worse than a failed build. If a caption is edited
so a fragment no longer matches, the script exits non-zero and writes nothing.

Add or remove a figure by editing `FIGURES` in `render.mjs` and updating the
`<picture>` blocks in `README.md`. `README.md` doubles as the PyPI page, so it
references the PNGs by absolute `raw.githubusercontent.com` URLs and currently
embeds only `addressing-funnel` and `retrieval-sequence`; the other PNGs were
rendered before the page was adapted to this repository's layout
(`superindex/engine/`, `superindex/nav/`) and should be re-rendered before use.

## Not a product artefact

Nothing here is read at runtime. It exists so the README can show the design
without a second, drifting copy of it.
