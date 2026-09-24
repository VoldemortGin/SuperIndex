"""The `search_pages` and `calculate` tools for the engine's local chat agent.

`tools()` returns them as engine `AgentTool`s, registered with
`SuperIndexClient(tools=...)`: the engine serves them after its built-in tools,
bound to the same document scope (`doc_ids`), and appends each tool's guidance
to the agent's system prompt.
"""
from __future__ import annotations

import json
from typing import Any

from superindex import bm25, calc
from superindex.engine.agent_tools import AgentTool, resolve_document

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
    "names or terms, call it first — before browse_documents() — to locate candidate "
    "pages.\n"
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
        allowed = frozenset(scope) if scope is not None else None
        entry, error = resolve_document(client, str(doc_name), allowed_ids=allowed)
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


def tools() -> list[AgentTool]:
    """`search_pages` and `calculate`, for `SuperIndexClient(tools=...)`."""
    return [
        AgentTool(TOOL_NAME, DESCRIPTION, SCHEMA, run_search,
                  "Use get_document_structure() instead", GUIDANCE),
        AgentTool(calc.TOOL_NAME, calc.DESCRIPTION, calc.SCHEMA,
                  lambda client, arguments, doc_ids: calc.run_calculate(arguments),
                  "Fix the expression and call calculate again", calc.GUIDANCE),
    ]
