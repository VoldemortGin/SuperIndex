# Vendored upstream: PageIndex

This directory is **not our code**. It is a vendored copy of the upstream
PageIndex engine, included so that a single `git clone` yields a runnable tree.

## Provenance

| | |
|---|---|
| Upstream | https://github.com/VectifyAI/PageIndex |
| Commit | `71714e8` |
| License | MIT — Copyright (c) 2025 Vectify AI (see `LICENSE`) |
| Vendored on | 2026-09-23 |

## What was kept and what was dropped

Kept — everything needed to `pip install -e` and run:

```
PageIndex/
├── LICENSE              upstream licence (kept — required by MIT)
├── README.md            upstream readme (for reference)
├── UPSTREAM.md          this file
├── pyproject.toml       package metadata
├── requirements.txt     pinned dependency set
├── run_pageindex.py     upstream CLI
└── pageindex/           the package itself
```

Dropped — upstream's own sample material, ~56 MB of the 58 MB checkout:

| Dropped | Size | Why |
|---|---:|---|
| `.git/` | 26 MB | Vendoring, not a submodule — we don't want upstream history here |
| `examples/` | 26 MB | Sample PDFs and notebooks; not needed to run |
| `assets/` | 1.3 MB | Images for the upstream README |
| `cookbook/` | 1.1 MB | Jupyter tutorials |
| `tests/` | 668 KB | Upstream test suite (fetch from upstream if you need it) |
| `.github/` | 52 KB | Upstream CI workflows, would misfire in this repo |

All of the above are listed in the repository-root `.gitignore`, so they stay
on disk locally if you have them but are never committed.

## Do not edit files in here

Every customisation this project makes lives in `scripts/`, `webapp/` and
`nav/` — never in `PageIndex/`. That keeps the diff against upstream clean and
makes the update procedure below trivial.

Two runtime customisations are applied by monkeypatching from our side, so they
survive updates:

- `pageindex.utils.SUMMARY_CONCURRENCY` — lowered from the default 64 to 8,
  because a 64-way burst trips DeepSeek's rate/balance guard. See
  `scripts/02_qa_test.py` (`apply_concurrency`).
- `reasoning_effort` — passed per call, never baked into the engine.

## Installing

The venv's `.pth` stores an **absolute path**, so a fresh clone must reinstall:

```bash
~/.workbuddy/binaries/python/envs/pageindex/bin/pip install -r PageIndex/requirements.txt
~/.workbuddy/binaries/python/envs/pageindex/bin/pip install -e PageIndex --no-deps
```

The `--no-deps` is deliberate: installing straight from `pyproject.toml` makes
pip backtrack badly on the unpinned `litellm` / `openai-agents` ranges. The
pinned `requirements.txt` resolves in seconds.

## Updating to a newer upstream commit

```bash
# 1. fetch upstream into a scratch clone (keeps our vendored copy untouched)
git clone https://github.com/VectifyAI/PageIndex.git /tmp/pi-new
cd /tmp/pi-new && git log --oneline -1

# 2. sync only the files we keep
rsync -a --delete \
  --exclude '.git/' --exclude '.github/' --exclude 'examples/' \
  --exclude 'assets/' --exclude 'cookbook/' --exclude 'tests/' \
  --exclude '__pycache__/' \
  /tmp/pi-new/ ./PageIndex/

# 3. update the commit hash in this file, then
#    git diff --stat PageIndex/   # review what actually changed
#    run the verification checklist in docs/HANDOVER.md §9
```

## Version pinning matters

This project was built and measured against `71714e8`. Behaviours that our
code and docs depend on — the `flash` layout pipeline, the `md_to_tree` API
shape, the five local agent tools, the `_CHAR_BUDGET` pagination — can all
change upstream. If you update, re-run the QA set before trusting the results.
