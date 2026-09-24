# PageIndex × AIA Reports — Local Test Bench

A local test harness for [PageIndex](https://github.com/VectifyAI/PageIndex)
(VectifyAI's vectorless, reasoning-based RAG engine) using **AIA Group's
financial reports as the corpus**. PageIndex is designed exactly for documents
like these: long, table-heavy financial reports where vector similarity search
tends to retrieve the wrong page.

The corpus is the five most recent **annual reports** plus the five matching
**interim reports**, taken from AIA's
[results & presentations archive](https://www.aia.com/en/investor-relations/overview/results-presentations).

---

## Corpus — 2,640 pages across 10 documents

### Annual reports

| Report | Period | Pages | Tree nodes | Depth | TOC source |
|---|---|---:|---:|---:|---|
| `AIA_Annual_Report_FY2021.pdf` | FY2021 | 304 | 488 | 6 | bookmarks |
| `AIA_Annual_Report_FY2022.pdf` | FY2022 | 312 | 469 | 5 | bookmarks |
| `AIA_Annual_Report_FY2023.pdf` | FY2023 | 376 | 639 | 6 | bookmarks |
| `AIA_Annual_Report_FY2024.pdf` | FY2024 | 371 | 597 | 6 | bookmarks |
| `AIA_Annual_Report_FY2025.pdf` | FY2025 | 363 | 549 | 5 | bookmarks |
| **subtotal** | | **1,726** | **2,742** | | |

### Interim reports

| Report | Period | Pages | Tree nodes | Depth | TOC source |
|---|---|---:|---:|---:|---|
| `AIA_Interim_Report_1H2021.pdf` | 1H2021 | 150 | 254 | 5 | bookmarks |
| `AIA_Interim_Report_1H2022.pdf` | 1H2022 | 154 | 245 | 6 | bookmarks |
| `AIA_Interim_Report_1H2023.pdf` | 1H2023 | 230 | 333 | 5 | bookmarks |
| `AIA_Interim_Report_1H2024.pdf` | 1H2024 | 190 | 309 | 5 | bookmarks |
| `AIA_Interim_Report_1H2025.pdf` | 1H2025 | 190 | 299 | 5 | bookmarks |
| **subtotal** | | **914** | **1,440** | | |

**Total: 2,640 pages, 4,182 tree nodes.**

Every document carries a text layer and a proper embedded outline, so PageIndex
resolves the hierarchy from the bookmarks (`toc_source: "bookmarks"`) rather
than inferring it from layout statistics. That is the best case for this
engine, and it means no OCR is required. The 1H2023 report is sourced from
HKEX rather than AIA's own site, as AIA's archive links it there.

---

## Layout

```
.
├── PageIndex/                  # the cloned upstream repo (editable install)
├── data/aia_reports/           # the 10 source PDFs
├── scripts/
│   ├── 01_build_trees.py       # stage 1 — offline tree build (no LLM)
│   ├── 02_qa_test.py           # stage 2 — index + retrieval QA (needs LLM key)
│   ├── extract_text.py         # ground-truth helper (pypdfium2)
│   ├── monitor.sh              # progress logger for long runs
│   ├── questions.json          # 20 questions — full 10-report corpus
│   └── questions_3docs.json    # 18 questions — scoped to the 3 indexed reports
├── webapp/
│   ├── server.py               # chat server (stdlib only, streaming SSE)
│   └── static/index.html       # the chat UI
├── results/
│   ├── trees/                  # stage-1 output: one <doc>_structure.json each
│   └── pageindex_store/        # stage-2 output: the client's local doc store
└── .env.example                # provider configuration template
```

---

## Web chat UI

A live query interface over whatever is currently indexed.

```bash
~/.workbuddy/binaries/python/envs/pageindex/bin/python webapp/server.py
# -> http://127.0.0.1:8787
```

No extra dependencies — it is Python's stdlib `http.server` plus a single
static HTML file.

What it does:

- **Document scope picker** — tick which reports a question may draw on; the
  list refreshes every 20 s, so reports still being indexed appear as
  "索引中" and become selectable as soon as they finish.
- **Streaming answers** — the answer is streamed token by token over SSE, so
  you watch it being written rather than waiting on a spinner.
- **Retrieval trace** — each answer carries a collapsible panel showing the
  model's *thinking* and every tool call it made. This is the interesting
  part: you can see it call `get_document_structure` to read the tree, then
  `get_page_content` on a specific page range, before it answers. That is the
  traceability PageIndex claims over vector RAG, made visible.
- **Suggested questions** — a few of the verified test questions, one click away.

### API

| Method | Path | Body / notes |
|---|---|---|
| `GET` | `/` | the chat UI |
| `GET` | `/api/status` | indexed docs, pending docs, model names |
| `POST` | `/api/ask` | `{"question": str, "doc_ids": [...]}` → `text/event-stream` |

SSE event names: `answer` (delta), `thinking` (delta), `tool_call`
(`name`, `arguments`), `tool_result` (`name`, `output`), `error`, `done`.

Chat runs are serialized server-side, so two tabs cannot interleave runs on
the same PageIndex client.

---

## How PageIndex works here

Two independent halves, and only the second one costs money:

1. **Index** — build a hierarchical *tree* instead of a vector index.
   `page_index_flash()` derives the skeleton from the PDF layout/bookmarks
   with **no LLM at all**; an LLM is then used only to write a short summary
   for each node and to refine the tree for search cost.
2. **Retrieve** — the chat model *reasons* over that tree, deciding which
   nodes to open, rather than matching embeddings. That is what makes the
   result traceable to a page instead of "vibe retrieval".

Because step 1's skeleton is LLM-free, the whole corpus can be parsed and its
structure inspected offline before spending anything.

---

## Two ways to get text out of a PDF

**Azure Document Intelligence becomes the default extractor as soon as
`AZURE_DI_ENDPOINT` and `AZURE_DI_KEY` are set in `.env`.** Every entry point
takes it up automatically — `scripts/02_qa_test.py` (PageIndex indexing) and
`nav/build.py` (the two-level navigator) both print which extractor they used
at startup. With nothing configured you get the built-in text layer, exactly as
before.

The fallback reads the PDF's own **text layer** with PyPDF2. That is free and
works well on running prose, but it has two hard limits on financial reports:

| | text layer | Azure Document Intelligence (default when configured) |
|---|---|---|
| Cost | free | per page |
| Tables | **mangled** — a bar-chart page comes out as `175230`, two numbers fused, label-to-value association gone | real Markdown tables |
| Scanned / image-only PDFs | **refused outright** (no text layer, no OCR) | OCR'd |
| Running prose | good | good |
| Page anchors | native (page ranges) | injected as `<!-- page: N -->` |

Both land in the same place — Markdown with `#` headings — so `nav.build` and
PageIndex's Markdown path consume either without changes.

**Normally you don't invoke the extractor at all** — configure `.env` and run
the pipeline as usual:

```bash
# 1. check the config (and analyse page 1 of one PDF as a cheap smoke test)
python scripts/06_azure_extract.py data/aia_reports --check --only FY2021

# 2. build the two-level index straight over the PDFs — Azure DI is picked up
#    automatically, no extra flag needed
python -m nav.build data/aia_reports --out corpus_index --summarize-files

# 3. or run the PageIndex QA pipeline, which takes it up the same way
python scripts/02_qa_test.py --docs FY2021
```

`06_azure_extract.py` exists for the case where you want the Markdown **on disk
as an artefact** (to inspect it, diff it, or feed it to something else):

```bash
python scripts/06_azure_extract.py data/aia_reports --out corpus_md
python -m nav.build corpus_md --out corpus_index --summarize-files
```

Both entry points accept `--extractor {auto,azure-di,text-layer}` to force a
backend — useful for measuring what Azure DI actually buys you on your corpus.

If a configured Azure call fails, the run **aborts** rather than silently
degrading to the text layer (a broken key would otherwise quietly change index
quality). Set `AZURE_DI_FALLBACK=1` to opt into the soft behaviour.

Configuration lives in `.env` (see the Azure section in `.env.example`) —
endpoint, key, model, output format, features. The page anchors are injected by
reading `analyzeResult.pages[].spans[].offset` and splicing a marker at each
page boundary, which is why `AZURE_DI_STRING_INDEX_TYPE` must stay
`unicodeCodePoint` (it keeps offsets aligned with Python string indices).

`extractors/azure_di.py` is plain REST over httpx — no Azure SDK dependency.
`extractors/backend.py` decides which extractor is active and takes over
PageIndex's `LocalAPI._extract_page_texts` when Azure is configured, so nothing
inside `PageIndex/` is ever edited. Offline tests:
`tests/test_azure_di.py` (28 assertions) and `tests/test_backend.py` (27).

---

## Running it

### 0. Get the source PDFs

The engine is **vendored** — `PageIndex/` ships with this repo, pinned to
upstream commit `71714e8`, so a plain `git clone` gives you a runnable tree.
(Provenance, what was stripped, and how to update: `PageIndex/UPSTREAM.md`.)

The only thing not committed is the source PDFs (~27 MB, publicly downloadable):

```bash
bash data/aia_reports/download.sh
```

> `PageIndex/` is vendored rather than added as a submodule so that one clone
> is enough — no `--recursive`, no missing-directory surprises. All of this
> project's own code lives in `scripts/`, `webapp/` and `nav/`; nothing in
> `PageIndex/` is ever modified, which keeps the upstream diff clean.

### 1. Environment

```bash
python3 -m venv ~/.workbuddy/binaries/python/envs/pageindex
~/.workbuddy/binaries/python/envs/pageindex/bin/pip install -r PageIndex/requirements.txt
~/.workbuddy/binaries/python/envs/pageindex/bin/pip install -e PageIndex --no-deps
```

> Note: installing straight from `pyproject.toml` makes pip backtrack badly on
> the unpinned `litellm`/`openai-agents` ranges. The repo's pinned
> `requirements.txt` resolves in seconds, so it is installed first and the
> package itself added with `--no-deps`.

Optional — only needed by `scripts/05_similarity_probe.py`, which downloads a
small ONNX embedding model:

```bash
~/.workbuddy/binaries/python/envs/pageindex/bin/pip install fastembed
export HF_ENDPOINT=https://hf-mirror.com      # huggingface.co is unreachable from CN
```

### 2. Stage 1 — offline tree build (no API key, ~45 s total)

```bash
~/.workbuddy/binaries/python/envs/pageindex/bin/python scripts/01_build_trees.py
```

Writes `results/trees/<doc>_structure.json` plus a `manifest.json`.

### 3. Stage 2 — retrieval QA (needs an LLM key)

```bash
cp .env.example .env      # then put your key in it
~/.workbuddy/binaries/python/envs/pageindex/bin/python scripts/02_qa_test.py
```

Useful flags:

```bash
--index-model gpt-5-mini          # model for summaries (cheap is fine)
--chat-model  gpt-5               # model that answers (use a strong one)
--base-url    https://...         # OpenAI-compatible gateway
--skip-index                      # reuse the existing local store
--force-index                     # re-index from scratch
--only Q05 Q20                    # run a subset of questions
```

Results land in `results/qa_results.json`.

---

## Current corpus state

Indexing was stopped deliberately part-way, so only **3 of the 10** reports carry a
full retrieval index. The offline structural trees exist for all 10, but only these
three can answer questions:

| Indexed report | Pages |
|---|---:|
| `AIA_Interim_Report_1H2021.pdf` | 150 |
| `AIA_Annual_Report_FY2021.pdf` | 304 |
| `AIA_Annual_Report_FY2022.pdf` | 312 |

Indexing is keyed on document name and reuses what is already stored, so resuming
picks up from where it stopped:

```bash
nohup ~/.workbuddy/binaries/python/envs/pageindex/bin/python -u \
  scripts/02_qa_test.py --concurrency 8 > results/qa_run.log 2>&1 &
```

---

## Test question sets

Two sets, both with **independently verified** expected answers (ground truth read
straight from the PDF text layer, never from PageIndex itself):

| File | Scope | Questions |
|---|---|---:|
| `scripts/questions_3docs.json` | the 3 currently indexed reports | 18 |
| `scripts/questions.json` | the full 10-report corpus | 20 |

Run either set with `--questions`:

```bash
# the set that matches what is actually indexed right now
~/.workbuddy/binaries/python/envs/pageindex/bin/python scripts/02_qa_test.py \
    --skip-index --questions questions_3docs.json --out qa_results_3docs.json

# a quick subset
... --questions questions_3docs.json --only A01 A05 A17
```

### `questions_3docs.json` — the 18-question set

**Interim 1H2021**

| ID | Fact | Expected |
|---|---|---|
| A01 | Interim dividend per share | 38.00 HK cents, +8.6% |
| A02 | VONB for 1H2021 | US$1,814m, +22% |
| A03 | ANP / VONB margin / OPAT | US$3,060m (+13%) / 59.0% (+4.2pps) / US$3,182m (+5%) |
| A04 | Free surplus and EV Equity | US$17.9bn (+US$4.4bn) / US$70.1bn (+5%) |

**Annual FY2021**

| ID | Fact | Expected |
|---|---|---|
| A05 | Total dividend per share | 146.00 HK cents = final 108.00 (+8%) + interim 38.00 |
| A06 | VONB / OPAT / UFSG | US$3,366m (+18%) / US$6,409m (+6%) / US$6,451m (+8%) |
| A07 | EV Equity and LCSM ratio | US$75.0bn new high; LCSM 399% |
| A08 | Group CFO | Mr. Garth Jones |
| A09 | Partnership distribution VONB | US$695m; VONB margin 39.1% |
| A10 | MDRT ranking | No.1 globally, 7 consecutive years |

**Annual FY2022**

| ID | Fact | Expected |
|---|---|---|
| A11 | Total and final dividend per share | 153.68 HK cents (+5.3%); final 113.40 (+5%) |
| A12 | VONB and 2H trend | US$3,092m, −5% for the year but +6% in 2H |
| A13 | EV and EV Equity | EV US$74,694m (+5%); EV Equity US$77,031m (+6%) |
| A14 | Share buy-back programme | three-year, US$10.0bn |
| A15 | LCSM cover ratio | 283%, on the PCR basis |
| A16 | MDRT ranking | 8 consecutive years |

**Cross-document**

| ID | Fact | Expected |
|---|---|---|
| A17 | FY2021 vs FY2022 VONB and dividend | VONB down (3,366 → 3,092, −5% CER) while dividend rose (146.00 → 153.68, +5.3%) |
| A18 | 2021 interim + final = total | 38.00 + 108.00 = 146.00 HK cents |

---

## The full-corpus set (`questions.json`)

20 questions spanning all five annual reports and all five interim reports,
covering dividend series across five years, segment VONB, officers, and a
five-year CAGR calculation. See the file itself for the full table; it becomes
usable once all 10 reports are indexed.

---

Both sets follow the same rules: expected answers were read from the PDF text
layer with `scripts/extract_text.py` (pypdfium2), **never** from PageIndex
itself, so the tests are not circular. They mix four question shapes:

- **needle lookups** — a single labelled figure on a specific page
- **reasoning lookups** — a figure plus its driver (growth *and* the resulting margin)
- **entity lookups** — officers named in the governance section
- **cross-document work** — aggregation across reports (Q12/Q19 ask for the
  five-year dividend series and CAGR; Q20 and A18 split a year's total dividend
  into interim and final, which needs two documents)

### Full-corpus ground truth

**Annual reports**

| ID | Fact | Expected |
|---|---|---|
| Q01 | FY2025 total dividend per share | 193.08 HK cents, +10% |
| Q02 | FY2025 final dividend per share | 144.08 HK cents |
| Q03 | FY2025 share buy-back programme | US$1.7bn (US$0.7bn payout ratio + US$1.0bn regular return) |
| Q04 | Group CEO & CFO | Lee Yuan Siong; Garth Jones |
| Q05 | AIA Hong Kong VONB 2025 | +28%; VONB margin 68.5% (+3.0pps) |
| Q06 | Other Markets VONB 2025 | US$485m, +7% |
| Q07 | FY2024 total dividend per share | 175.48 HK cents, +9% |
| Q08 | FY2024 final dividend per share | 130.98 HK cents, +10% |
| Q09 | FY2023 total dividend per share | 161.36 HK cents |
| Q10 | FY2022 total dividend per share | 153.68 HK cents, +5.3% |
| Q11 | FY2021 total dividend per share | 146.00 HK cents |
| Q12 | 5-year annual dividend series + CAGR | 146.00 → 153.68 → 161.36 → 175.48 → 193.08; ≈7.2% CAGR |

**Interim reports**

| ID | Fact | Expected |
|---|---|---|
| Q13 | 1H2025 interim dividend per share | 49.00 HK cents, +10% |
| Q14 | 1H2025 headline growth rates | VONB +14%, OPAT/share +12%, UFSG/share +10%, interim DPS +10% |
| Q15 | 1H2024 interim dividend & buy-back | 44.50 HK cents, +5.2%; +US$2.0bn to the programme, total US$12.0bn |
| Q16 | 1H2023 interim dividend per share | 42.29 HK cents, +5% |
| Q17 | 1H2022 interim dividend per share | 40.28 HK cents, +6% |
| Q18 | 1H2021 interim dividend per share | 38.00 HK cents, +8.6% |
| Q19 | 5-year interim dividend series + CAGR | 38.00 → 40.28 → 42.29 → 44.50 → 49.00; ≈6.6% CAGR |
| Q20 | FY2025 interim + final = total | 49.00 + 144.08 = 193.08 HK cents |

---

## Notes

- Local mode handles **text-based PDFs only** — no OCR, no image understanding,
  no folders. Those are PageIndex Cloud features.
- The local client stores documents under `storage_path` (`results/pageindex_store/`
  by default), one directory per document holding `tree.json`, `pages.json`
  and `doc.json`.
- Indexing is synchronous in local mode, so `submit_document()` blocks.
- A 1H2026 interim report is also published and can be dropped into
  `data/aia_reports/` — both scripts pick up new PDFs automatically.
