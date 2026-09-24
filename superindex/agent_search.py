"""The `search_pages` tool for PageIndex's local chat agent.

PageIndex builds the agent's tools from `pageindex.agent_tools._tool_specs`
(the local tool set served as an in-process MCP server, see
`pageindex.integrations.openai_agents.build_mcp_server`, which looks the
function up at call time). `install()` wraps that function so local clients
also get `search_pages`, bound to the same document scope (`doc_ids`) as the
built-in tools; PageIndex's own source stays untouched.
"""
from __future__ import annotations

import json
from typing import Any

from superindex import bm25

TOOL_NAME = "search_pages"
MAX_TOP_K = 20

DESCRIPTION = (
    "Keyword (BM25) search over the pages of the documents. Returns the best "
    "matching pages with document name, page number, section title, a short "
    "snippet (matches in **bold**) and a score. Use it first to find candidate "
    "pages for a question — especially for specific terms, names, figures or "
    "years — then verify with get_page_content(). Works across all documents "
    "unless doc_name is given."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Keywords to look for, e.g. \"final dividend 2021\" or "
                           "\"新业务价值 增长\". Short keyword lists work best.",
        },
        "doc_name": {
            "type": "string",
            "description": "Optional: restrict to one document; copy its `name` "
                           "verbatim from browse_documents().",
        },
        "top_k": {
            "type": "integer",
            "description": f"Number of pages to return (default 5, max {MAX_TOP_K}).",
        },
    },
    "required": ["query"],
}

GUIDANCE = (
    "KEYWORD SEARCH:\n"
    "- search_pages(query) finds the pages that mention your keywords, across all "
    "documents (or one, with doc_name). For a question about specific facts, figures, "
    "names or terms, call it first to locate candidate pages.\n"
    "- Then verify: read the hit pages with get_page_content() (use "
    "get_document_structure() for context) and answer only from what you read. "
    "Snippets are previews, not evidence.\n"
    "- No useful hits: retry with fewer or alternative keywords (synonyms, other "
    "language, the table's row label), or fall back to get_document_structure()."
)


def _failure(error: str, code: str, options: list[str]) -> tuple[str, bool]:
    return json.dumps({"error": error, "errorCode": code,
                       "next_steps": {"options": options}}, ensure_ascii=False), True


def run_search(client: Any, arguments: dict[str, Any],
               doc_ids: str | list[str] | None = None) -> tuple[str, bool]:
    """Execute one `search_pages` call; returns (JSON envelope, is_error)."""
    query = str(arguments.get("query") or "").strip()
    if not query:
        return _failure("query is required", "INVALID_INPUT",
                        ["Pass the keywords to search for as `query`"])
    try:
        top_k = int(arguments.get("top_k") or 5)
    except (TypeError, ValueError):
        top_k = 5
    top_k = max(1, min(top_k, MAX_TOP_K))

    scope = None if doc_ids is None else (
        [doc_ids] if isinstance(doc_ids, str) else [str(d) for d in doc_ids])
    doc_name = arguments.get("doc_name")
    if doc_name:
        from pageindex.agent_tools import _resolve_document

        allowed = frozenset(scope) if scope is not None else None
        entry, error = _resolve_document(client, str(doc_name), allowed_ids=allowed)
        if error is not None:
            return json.dumps(error[0], ensure_ascii=False), True
        assert entry is not None
        scope = [entry["id"]]

    result = bm25.search(client.storage_path, query, doc_ids=scope, top_k=top_k)
    hits = [{k: v for k, v in h.to_dict().items() if k != "doc_id"} for h in result.hits]
    payload = {
        "success": True,
        "query": query,
        "match": result.match,
        "documents_searched": result.searched,
        "results": hits,
        "next_steps": {
            "summary": ("Read the most relevant hit pages with get_page_content(doc_name, "
                        "pages) before answering." if hits else
                        "No page matched. Try other keywords, or browse the outline "
                        "with get_document_structure()."),
        },
    }
    return json.dumps(payload, ensure_ascii=False), False


def _spec(client: Any, doc_ids: Any) -> tuple[str, str, dict[str, Any], Any]:
    def invoke(arguments: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        try:
            text, is_error = run_search(client, arguments or {}, doc_ids)
        except Exception as exc:  # noqa: BLE001 - tool calls never raise into the agent
            text, is_error = _failure(f"{TOOL_NAME} failed: {exc}", "INTERNAL_ERROR",
                                      ["Use get_document_structure() instead"])
        return [{"type": "text", "text": text}], is_error

    return TOOL_NAME, DESCRIPTION, json.loads(json.dumps(SCHEMA)), invoke


def install() -> None:
    """Add `search_pages` to every local PageIndex client's agent tools.
    Idempotent."""
    from pageindex import agent_tools

    original = agent_tools._tool_specs
    if getattr(original, "_superindex_search", False):
        return

    def tool_specs(client: Any, include_management: bool = False, doc_ids: Any = None
                   ) -> list[tuple[str, str, dict[str, Any], Any]]:
        specs = original(client, include_management, doc_ids)
        if getattr(client, "api_key", None) or not getattr(client, "storage_path", None):
            return specs
        return [*specs, _spec(client, doc_ids)]

    tool_specs._superindex_search = True  # type: ignore[attr-defined]
    agent_tools._tool_specs = tool_specs
