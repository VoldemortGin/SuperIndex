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
- Kept for on-disk / output compatibility: the default local storage folder
  `.pageindex`, `pi-` document ids, `pageindex-citation-NN` anchors.
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

## Trimmed to the local mode (0.1.1)

superindex only uses the local mode (store on disk, own chat model), so the
hosted-service client and the unused integrations were removed. Local
behavior is unchanged: tool names, descriptions, schemas and results, the
system prompt, the targeting blocks and the request bodies sent to the model
are byte-identical (checked by capturing every request against a fake OpenAI
server before and after).

Removed modules:

- `cloud_api.py` (the hosted REST client), `mcp_bridge.py` (the streamable
  HTTP MCP client for the hosted MCP server), `_version.py` (only they
  reported the SDK version).
- `integrations/anthropic_sdk.py`, `integrations/claude_agent_sdk.py`.

Removed from `client.py`:

- `PageIndexCloudClient`; `SuperIndexClient`'s `api_key` argument, the
  `"cloud"` / `"pageindex-cloud"` values of `index=` / `chat=` / `mode=`
  (they now raise instead of reading as a model name), `BASE_URL`, the
  managed chat (the `chat_completions` endpoint branch and the woven cloud
  stream), `_wait_until_ready`, `_require_cloud` and the cloud-only methods
  `get_block`, `get_page_image`, `get_document_image`, `submit_query`,
  `get_retrieval`, `create_folder`, `list_folders`, `get_folder_path`,
  `get_folder_id`, `folder_context`.
- `chat(protocol="messages")` and `_messages`, `as_anthropic_tools`,
  `anthropic_runner_config`, `as_claude_mcp`, `claude_agent_config`;
  `as_openai_tools(hosted=...)` (the `HostedMCPTool` branch).
- `SuperIndexLocalClient` stays as a plain subclass (the older name);
  `get_document_path` returns the document name (no folders).

Removed elsewhere:

- `local_chat.py`: `run_messages` and its Anthropic helpers (`_require_anthropic`,
  `_anthropic_*`, `_cache_marks`, `_dump_block`, `_dump_message`,
  `_default_max_tokens`), `run_cloud_chat_stream`, `_cloud_chunk_events`, the
  cloud hint in `_model_backend_error`. `_litellm_claude_marks` (prompt-cache
  marks for Claude models routed through LiteLLM) stays.
- `agent_tools.py`: `_cloud_bridge`, `_bridge_invoker`, `_raise_account_limit`,
  the live cloud instructions and `cited_answer` fetch, the cloud branch of
  `folder_targeting_block`. `render_text` moved here from `mcp_bridge.py`
  (as `_render_text`; `agent_tools()` still uses it).
- `integrations/openai_agents.py`: the `hosted` argument.
- `types.py`: `CloudIndexConfig`, `PAGEINDEX_CLOUD`; `IndexConfig` is
  `LocalIndexConfig`.

Kept on purpose: the tool-result and tool-description texts that mention
"PageIndex cloud" / `PageIndexCloudClient` (e.g. the no-folders and
semantic-ranking next steps). They reach the model in local mode, so they
stay byte-identical. `mcp` stays a dependency: the Agents SDK receives the
local tools as an in-process `MCPServer` built with `mcp.types`.

Dependencies: `requests` and `urllib3` are no longer direct dependencies
(they only served the cloud client and the MCP bridge; `requests` still comes
in through `openai-agents` / `tiktoken`); the `[anthropic]` and `[claude]`
extras are gone.

## Pulling a newer upstream

There is no automatic sync any more. To port an upstream fix, diff the upstream
`pageindex/` at the new commit against `71714e8`, apply the relevant hunks under
`superindex/engine/` (renaming as above; skip the removed modules), then run `uv run pytest tests -q`.
