"""Retrieval prefetch: keyword-search a question before the agent starts.

The top-k BM25 pages (`superindex.bm25`, same `--match` and document scope as
`search_pages`) are put in front of the question as a clearly delimited
"检索线索" block in the user message — not in the system prompt, which stays
the same for every question. The agent still has to read the pages; the
block only tells it where to look first. No hit, no block.

On by default; ``--prefetch/--no-prefetch`` and ``--prefetch-k`` on ask /
serve / batch, env ``SUPERINDEX_PREFETCH`` (0/false/off disables) and
``SUPERINDEX_PREFETCH_K`` (default 5).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from superindex import bm25
from superindex.runtime import ConfigError

ENV = "SUPERINDEX_PREFETCH"
K_ENV = "SUPERINDEX_PREFETCH_K"
DEFAULT_K = 5
MAX_K = 20
SNIPPET_CHARS = 200
_OFF = {"0", "false", "no", "off"}

HEADER = "【检索线索】"
FOOTER = "【检索线索结束】"
NOTE = ("以下为关键词检索得到的候选页，仅供参考；请用 get_page_content 读取原文核实，"
        "若不相关再用 get_document_structure / search_pages 自行导航。")


def resolve_k(enabled: bool | None = None, k: int | None = None) -> int:
    """Pages to prefetch, 0 when off: `enabled`/`k` (the CLI flags), else
    ``SUPERINDEX_PREFETCH`` / ``SUPERINDEX_PREFETCH_K``, else on with 5."""
    if enabled is None:
        enabled = os.environ.get(ENV, "").strip().lower() not in _OFF
    if not enabled:
        return 0
    if k is None:
        raw = os.environ.get(K_ENV, "").strip()
        try:
            k = int(raw) if raw else DEFAULT_K
        except ValueError:
            raise ConfigError(f"{K_ENV}: expected a whole number, got {raw!r}") from None
    if k < 0:
        raise ConfigError(f"prefetch k must be 0 or more, got {k}")
    return min(k, MAX_K)


def _scope(doc_ids: str | list[str] | None) -> list[str] | None:
    if doc_ids is None:
        return None
    return [doc_ids] if isinstance(doc_ids, str) else [str(d) for d in doc_ids]


def search(store: str | os.PathLike[str], question: str,
           doc_ids: str | list[str] | None, k: int) -> list[bm25.Hit]:
    """The top `k` pages for `question` within `doc_ids` (None: all)."""
    if k <= 0:
        return []
    return bm25.search(Path(store), question, doc_ids=_scope(doc_ids), top_k=k).hits


def _short(snippet: str, limit: int = SNIPPET_CHARS) -> str:
    if len(snippet) <= limit:
        return snippet
    piece = snippet[:limit].rstrip()
    if piece.count("**") % 2:
        piece += "**"
    return piece + "…"


def candidates(hits: list[bm25.Hit]) -> list[dict[str, Any]]:
    """The hits as records (for batch results, the web UI, `ask -v`)."""
    out = []
    for h in hits:
        rec: dict[str, Any] = {"doc_name": h.doc_name, "page": h.page}
        if h.page_label:
            rec["page_label"] = h.page_label
        rec.update(section=h.section, score=round(h.score, 3), snippet=_short(h.snippet))
        out.append(rec)
    return out


def block(hits: list[bm25.Hit]) -> str:
    """The "检索线索" block for `hits`, or "" when there are none."""
    if not hits:
        return ""
    lines = [HEADER, NOTE]
    for i, c in enumerate(candidates(hits), start=1):
        label = f"（PageNumber {c['page_label']}）" if c.get("page_label") else ""
        section = f" — 章节：{c['section']}" if c["section"] else ""
        lines.append(f"{i}. {c['doc_name']} 第 {c['page']} 页{label}{section}")
        lines.append(f"   {c['snippet']}")
    lines.append(FOOTER)
    return "\n".join(lines)


def augment(question: str, hits: list[bm25.Hit]) -> str:
    """`question` with the candidates block in front; unchanged without hits."""
    text = block(hits)
    return f"{text}\n\n问题：{question}" if text else question


def prepare(store: str | os.PathLike[str], question: str,
            doc_ids: str | list[str] | None, k: int) -> tuple[str, list[bm25.Hit]]:
    """(message for the agent, the hits it names)."""
    hits = search(store, question, doc_ids, k)
    return augment(question, hits), hits
