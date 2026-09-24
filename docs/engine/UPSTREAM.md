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

## Extension points added for the superindex side (0.1.1)

The superindex modules used to wrap or monkeypatch engine internals; they now
use formal engine parameters instead:

- `SuperIndexClient(tools=[AgentTool(...)])` (`agent_tools.AgentTool`): extra
  local agent tools served after the built-in ones, bound to the chat's
  document scope; each tool's `guidance` is appended to the system prompt
  after the client's `instructions`. `superindex/agent_search.py` registers
  `search_pages` and `calculate` this way.
- `chat(..., stream=True, extras=ChatExtras(...))` (`local_chat.ChatExtras`):
  a multimodal last user message, appended instructions, extra Agents SDK
  tools and a `call_model_input_filter` for one run.
  `superindex/image_chat.py` sends PDF page screenshots and the
  `get_page_image` tool this way.
- `SuperIndexClient(page_text_extractor=...)`: replaces the PDF text-layer
  extractor (`local_api.extract_page_texts`, PyPDF2).
  `superindex/extractors/backend.page_text_extractor()` returns the Azure DI
  one.
- `agent_tools.resolve_document` (was `_resolve_document`) is public.

## Pulling a newer upstream

There is no automatic sync any more. To port an upstream fix, diff the upstream
`pageindex/` at the new commit against `71714e8`, apply the relevant hunks under
`superindex/engine/` (renaming as above), then run `uv run pytest tests -q`.
