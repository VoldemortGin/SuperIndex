"""Retrieval prefetch: keyword-search a question before the agent starts.

The top-k BM25 pages (`superindex.bm25`, same `--match` and document scope as
`search_pages`) are put in front of the question as a clearly delimited
"检索线索" block in the user message — not in the system prompt, which stays
the same for every question. Each candidate carries its full page text (the
same text `get_page_content` returns, from the store's ``pages.json``), so the
agent can answer from it without deciding to read the page first. Pages are
added in rank order up to a total character budget: the page that does not fit
is cut to what is left (and says so), later ones only get their snippet. No
hit, no block.

On by default; ``--prefetch/--no-prefetch``, ``--prefetch-k``,
``--prefetch-chars`` and ``--prefetch-content page|snippet`` on ask / serve /
batch, env ``SUPERINDEX_PREFETCH`` (0/false/off disables),
``SUPERINDEX_PREFETCH_K`` (default 5), ``SUPERINDEX_PREFETCH_CHARS`` (page
text budget, default 60000; 0 or less: snippets only) and
``SUPERINDEX_PREFETCH_CONTENT`` (``page``, the default, or ``snippet`` for
the old snippet-only block).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from superindex import bm25
from superindex.runtime import ConfigError

ENV = "SUPERINDEX_PREFETCH"
K_ENV = "SUPERINDEX_PREFETCH_K"
CHARS_ENV = "SUPERINDEX_PREFETCH_CHARS"
CONTENT_ENV = "SUPERINDEX_PREFETCH_CONTENT"
DEFAULT_K = 5
DEFAULT_CHARS = 60000
CONTENT_MODES = ("page", "snippet")
MAX_K = 20
SNIPPET_CHARS = 200
_OFF = {"0", "false", "no", "off"}

HEADER = "【检索线索】"
FOOTER = "【检索线索结束】"
NOTE = ("以下为关键词检索得到的候选页，仅供参考；请用 get_page_content 读取原文核实，"
        "若不相关再用 get_document_structure / search_pages 自行导航。")
PAGE_NOTE = ("以下为关键词检索得到的候选页，已附上每页完整原文（与 get_page_content 读到的一致），"
             "可直接据此作答，并注明来源文档与页码；若这些页里没有答案，或某页已截断 / 超出预算只有片段，"
             "再用 get_document_structure / search_pages / get_page_content 自行导航。")
TRUNCATED = "（本页已截断，其余内容请用 get_page_content 读取）"
OVER_BUDGET = "（超出预算未附全文）"


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


def resolve_chars(chars: int | None = None) -> int:
    """Page text budget: `chars` (the CLI flag), else ``SUPERINDEX_PREFETCH_CHARS``,
    else 60000; 0 or less means snippets only."""
    if chars is not None:
        return chars
    raw = os.environ.get(CHARS_ENV, "").strip()
    try:
        return int(raw) if raw else DEFAULT_CHARS
    except ValueError:
        raise ConfigError(f"{CHARS_ENV}: expected a whole number, got {raw!r}") from None


def resolve_content(content: str | None = None) -> str:
    """``page`` (full page text) or ``snippet``: `content`, else
    ``SUPERINDEX_PREFETCH_CONTENT``, else ``page``."""
    value = (content or os.environ.get(CONTENT_ENV) or "page").strip().lower()
    if value not in CONTENT_MODES:
        raise ConfigError(f"{CONTENT_ENV}: expected one of {', '.join(CONTENT_MODES)}, "
                          f"got {value!r}")
    return value


@dataclass
class Page:
    """A candidate page as sent: `text` is its full (or cut) page text when
    `injected`, else empty (snippet only)."""
    doc_name: str
    page: int
    text: str = ""
    injected: bool = False
    truncated: bool = False

    @property
    def key(self) -> str:
        """`doc:page`, the `pages_read` format."""
        return f"{self.doc_name}:{self.page}"


@dataclass
class Prefetch:
    hits: list[bm25.Hit]
    pages: list[Page]
    message: str

    def injected(self) -> list[str]:
        """`doc:page` of the pages sent with their text (cut ones included)."""
        return [p.key for p in self.pages if p.injected]


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


def candidates(hits: list[bm25.Hit],
               pages: list[Page] | None = None) -> list[dict[str, Any]]:
    """The hits as records (for batch results, the web UI, `ask -v`); with
    `pages`, each also says whether its text was sent (`injected`, `truncated`)."""
    out = []
    for i, h in enumerate(hits):
        rec: dict[str, Any] = {"doc_name": h.doc_name, "page": h.page}
        if h.page_label:
            rec["page_label"] = h.page_label
        rec.update(section=h.section, score=round(h.score, 3), snippet=_short(h.snippet))
        if pages is not None:
            rec["injected"] = pages[i].injected
            if pages[i].truncated:
                rec["truncated"] = True
        out.append(rec)
    return out


def read_pages(store: str | os.PathLike[str], hits: list[bm25.Hit], *,
               chars: int | None = None, content: str | None = None) -> list[Page]:
    """The page text to send for each hit, in rank order, within `chars`
    (`resolve_chars`); none in ``snippet`` mode (`resolve_content`)."""
    from superindex.engine.local_store import DocStore

    budget = resolve_chars(chars)
    if resolve_content(content) == "snippet" or budget <= 0:
        return [Page(h.doc_name, h.page) for h in hits]
    docs = DocStore(str(store))
    texts: dict[str, list[str]] = {}
    out = []
    for h in hits:
        if budget <= 0:
            out.append(Page(h.doc_name, h.page))
            continue
        if h.doc_id not in texts:
            texts[h.doc_id] = bm25._page_texts(docs.get_pages(h.doc_id) or [])
        doc = texts[h.doc_id]
        text = doc[h.page - 1] if 0 < h.page <= len(doc) else ""
        cut = len(text) > budget
        text = text[:budget] if cut else text
        budget -= len(text)
        out.append(Page(h.doc_name, h.page, text, injected=True, truncated=cut))
    return out


def block(hits: list[bm25.Hit], pages: list[Page] | None = None) -> str:
    """The "检索线索" block for `hits` — with the text of `pages` (one per hit,
    `read_pages`) where injected, else their snippets — or "" without hits."""
    if not hits:
        return ""
    full = pages is not None and any(p.injected for p in pages)
    lines = [HEADER, PAGE_NOTE if full else NOTE]
    for i, c in enumerate(candidates(hits), start=1):
        label = f"（PageNumber {c['page_label']}）" if c.get("page_label") else ""
        section = f" — 章节：{c['section']}" if c["section"] else ""
        lines.append(f"{i}. {c['doc_name']} 第 {c['page']} 页{label}{section}")
        page = pages[i - 1] if pages is not None else None
        if page is not None and page.injected:
            lines.append(f"<<<候选页 {i} 原文开始>>>")
            lines.append(page.text)
            if page.truncated:
                lines.append(TRUNCATED)
            lines.append(f"<<<候选页 {i} 原文结束>>>")
        else:
            lines.append(f"   {c['snippet']}" + (OVER_BUDGET if full else ""))
    lines.append(FOOTER)
    return "\n".join(lines)


def augment(question: str, hits: list[bm25.Hit], pages: list[Page] | None = None) -> str:
    """`question` with the candidates block in front; unchanged without hits."""
    text = block(hits, pages)
    return f"{text}\n\n问题：{question}" if text else question


def prepare(store: str | os.PathLike[str], question: str,
            doc_ids: str | list[str] | None, k: int, *,
            chars: int | None = None, content: str | None = None) -> Prefetch:
    """Search, read the candidates' page text and build the agent's message."""
    hits = search(store, question, doc_ids, k)
    pages = read_pages(store, hits, chars=chars, content=content)
    return Prefetch(hits, pages, augment(question, hits, pages))
