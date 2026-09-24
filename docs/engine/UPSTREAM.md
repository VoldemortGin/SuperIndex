# Engine provenance: PageIndex

`superindex/engine/` is derived from the upstream PageIndex engine. It started
as a vendored, unmodified copy (`PageIndex/pageindex/`) and was merged into the
`superindex` package for the PyPI release (0.1.0).

## Provenance

| | |
|---|---|
| Upstream | https://github.com/VectifyAI/PageIndex |
| Version / commit | 0.2.10 / `71714e8` |
| License | MIT — Copyright (c) 2025 Vectify AI (full text in the repository-root `LICENSE`) |
| Vendored on | 2026-09-23 |
| Merged into `superindex.engine` | 2026-09-24 |

## What changed against upstream

- Package moved and renamed: `pageindex` → `superindex.engine` (all internal
  imports were already relative; nothing else in the tree moved).
- Product name in code and prompts: `PageIndex` → `SuperIndex`
  (`PageIndexClient` → `SuperIndexClient`, `PageIndexLocalClient` →
  `SuperIndexLocalClient`, `PageIndexAPIError` → `SuperIndexAPIError`, the
  chat agent's name and system prompt header).
- Kept as-is because they name VectifyAI's hosted service: `PageIndexCloudClient`,
  "PageIndex cloud" / MCP server wording, `PAGEINDEX_API_KEY`,
  `"pageindex-cloud"`, `api.pageindex.ai`, the cloud MCP server name `pageindex`.
- Kept for on-disk / output compatibility: the default local storage folder
  `.pageindex`, `pi-` document ids, `pageindex-citation-NN` anchors.
- `version("pageindex")` → `version("superindex")`; the `[anthropic]` /
  `[claude]` install hints point at `superindex` extras.
- Dropped upstream files: `run_pageindex.py`, its `pyproject.toml`,
  `requirements.txt`, `README.md`. The flash README's CLI section went with
  `run_pageindex.py`. `naming-rules.md` lives next to this file.

## Customisations applied from the superindex side

These still wrap engine internals instead of editing them (candidates for a
later simplification now that the engine is ours):

- `superindex/agent_search.py` wraps `agent_tools._tool_specs` to add the
  `search_pages` and `calculate` tools.
- `superindex/image_chat.py` reuses `local_chat` private helpers to send PDF
  page screenshots.
- `superindex/extractors/backend.py` replaces `LocalAPI._extract_page_texts`
  for the Azure DI PDF path.

## Pulling a newer upstream

There is no automatic sync any more. To port an upstream fix, diff the upstream
`pageindex/` at the new commit against `71714e8`, apply the relevant hunks under
`superindex/engine/` (renaming as above), then run `uv run pytest tests -q`.
