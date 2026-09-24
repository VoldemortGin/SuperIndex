"""Index Azure DI Markdown into a PageIndexClient-compatible local store.

Input is Markdown as written by `extractors.azure_di` — one document, with a
``<!-- page: N -->`` line where each PDF page begins. Output is the exact
on-disk shape `PageIndexClient` (local mode) reads, so its chat agent and its
tools (`get_document_structure`, `get_page_content`) work unchanged:

    <store>/docs/<doc_id>/pages.json   [{"page_index": 1, "markdown": ...}, ...]
    <store>/docs/<doc_id>/tree.json    [{"title", "node_id", "start_index",
                                         "end_index", "summary"?, "nodes"?}]
    <store>/docs/<doc_id>/doc.json     document metadata
    <store>/manifest.json

Page numbers: ``page_index`` N is the page named by the ``<!-- page: N -->``
marker, i.e. the original PDF page, so citations point at real pages. Pages
with no marker (e.g. a partial extraction starting at page 5) are kept as
empty pages to hold that numbering. A Markdown file with no markers at all is
split into pseudo-pages of roughly ``page_chars`` characters at heading or
blank-line boundaries (a short file becomes a single page).

Only light modules are imported here: headings come from `nav.build`, storage
from `pageindex.local_store`. The PDF stack (PyPDF2 / pypdfium2 / flash) is not
touched; `pageindex.utils` (which imports PyPDF2 at module level) is imported
only when LLM summaries are requested.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pageindex.local_store import DocStore
from pageindex.naming import sanitize_filename

from nav.build import markdown_chapters
from nav.store import Chapter

# Same marker `extractors.azure_di.PAGE_MARKER` writes ("<!-- page: {n} -->"),
# matched leniently on whitespace.
PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.IGNORECASE)
TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
DEFAULT_PAGE_CHARS = 4000
MD_SUFFIXES = {".md", ".markdown"}


# ───────────────────────────────────────────────────────────── pages
@dataclass
class ParsedMarkdown:
    lines: list[str]          # the document with marker lines removed
    line_pages: list[int]     # 1-based page of each line in `lines`
    pages: list[str]          # pages[i] is the content of page i + 1
    has_markers: bool


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def parse_pages(markdown: str, page_chars: int = DEFAULT_PAGE_CHARS) -> ParsedMarkdown:
    """Split Markdown into pages using its ``<!-- page: N -->`` markers, or
    into pseudo-pages when it carries none."""
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if PAGE_MARKER_RE.search(text):
        lines, line_pages = _split_on_markers(text)
        has_markers = True
    else:
        lines = text.split("\n")
        line_pages = _pseudo_pages(lines, page_chars)
        has_markers = False
    page_count = max(line_pages, default=1)
    return ParsedMarkdown(lines, line_pages,
                          _assemble_pages(lines, line_pages, page_count),
                          has_markers)


def _split_on_markers(text: str) -> tuple[list[str], list[int]]:
    """Drop every marker and tag each remaining line with its page. A marker
    that shares a line with text (not what Azure writes, but tolerated) splits
    that line: text before it stays on the old page, text after moves on."""
    lines: list[str] = []
    pages: list[int | None] = []
    current: int | None = None
    for raw in text.split("\n"):
        if not PAGE_MARKER_RE.search(raw):
            lines.append(raw)
            pages.append(current)
            continue
        pos = 0
        for m in PAGE_MARKER_RE.finditer(raw):
            before = raw[pos:m.start()]
            if before.strip():
                lines.append(before.strip())
                pages.append(current)
            current = max(1, int(m.group(1)))
            pos = m.end()
        after = raw[pos:]
        if after.strip():
            lines.append(after.strip())
            pages.append(current)
    # Text ahead of the first marker belongs to the first marked page.
    first = next((p for p in pages if p is not None), 1)
    return lines, [first if p is None else p for p in pages]


def _pseudo_pages(lines: list[str], page_chars: int) -> list[int]:
    """Greedy pseudo-pagination for marker-less Markdown: once a page holds
    `page_chars` characters, break before the next heading or blank line —
    never inside a fenced code block or a table. Past twice the budget, break
    at the next line outside a code block even if it is mid-table."""
    budget = max(1, page_chars)
    out: list[int] = []
    page, size, in_code = 1, 0, False
    for line in lines:
        stripped = line.strip()
        if size >= budget and not in_code:
            soft = not stripped or HEADING_RE.match(stripped) is not None
            if soft or size >= 2 * budget:
                page, size = page + 1, 0
        if stripped.startswith("```"):
            in_code = not in_code
        out.append(page)
        size += len(line) + 1
    return out


def _assemble_pages(lines: list[str], line_pages: list[int],
                    page_count: int) -> list[str]:
    """Page texts, with blank edges trimmed. A table cut by a page break gets
    its header row repeated at the top of the continuation page, so that page
    is still a readable table on its own."""
    buckets: list[list[str]] = [[] for _ in range(page_count)]
    header: list[str] | None = None
    prev_page: int | None = None
    for i, line in enumerate(lines):
        page = line_pages[i]
        stripped = line.strip()
        if stripped and not _is_table_line(line):
            header = None
        elif (_is_table_line(line) and i + 1 < len(lines)
              and TABLE_SEP_RE.match(lines[i + 1].strip())):
            header = [line, lines[i + 1]]
        if (page != prev_page and header is not None and _is_table_line(line)
                and not any(s.strip() for s in buckets[page - 1])
                and line != header[0]):
            buckets[page - 1].extend(header)
        if stripped or buckets[page - 1]:
            buckets[page - 1].append(line)
        if stripped:
            prev_page = page
    return ["\n".join(b).strip("\n") for b in buckets]


# ───────────────────────────────────────────────────────────── tree
def _span_pages(parsed: ParsedMarkdown, start_line: int, end_line: int) -> tuple[int, int]:
    """(first, last) page covered by the 1-based inclusive line span, judged by
    its non-blank lines; a blank-only span sits on its first line's page."""
    idx = range(start_line - 1, min(end_line, len(parsed.lines)))
    pages = [parsed.line_pages[i] for i in idx if parsed.lines[i].strip()]
    if not pages:
        page = parsed.line_pages[start_line - 1] if parsed.lines else 1
        return page, page
    return min(pages), max(pages)


def _chapter_node(ch: Chapter, parsed: ParsedMarkdown) -> dict[str, Any]:
    start, end = _span_pages(parsed, ch.start, ch.end)
    node: dict[str, Any] = {"title": ch.title, "start_index": start, "end_index": end,
                            "_line": ch.start}
    if ch.children:
        node["nodes"] = [_chapter_node(c, parsed) for c in ch.children]
    return node


def build_tree(parsed: ParsedMarkdown, doc_title: str) -> list[dict[str, Any]]:
    """Heading tree with page ranges. Each node's range covers its whole
    subtree — the heading's page through the last page before the next heading
    of the same or a higher level — as in PageIndex's PDF trees. Text before
    the first heading becomes a "Preface" node. A document with no headings
    gets one root node with a child per page."""
    chapters = markdown_chapters(parsed.lines)
    tree = [_chapter_node(ch, parsed) for ch in chapters]
    first_heading = chapters[0].start if chapters else len(parsed.lines) + 1
    if chapters and any(s.strip() for s in parsed.lines[:first_heading - 1]):
        start, end = _span_pages(parsed, 1, first_heading - 1)
        tree.insert(0, {"title": "Preface", "start_index": start, "end_index": end,
                        "_line": 1})
    if not chapters:
        total = len(parsed.pages)
        root: dict[str, Any] = {"title": doc_title, "start_index": 1, "end_index": total}
        if total > 1:
            root["nodes"] = [{"title": f"Page {p}", "start_index": p, "end_index": p}
                             for p in range(1, total + 1)]
        tree = [root]
    _write_node_ids(tree)
    return tree


def _preorder(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in tree:
        out.append(node)
        out.extend(_preorder(node.get("nodes") or []))
    return out


def _write_node_ids(tree: list[dict[str, Any]]) -> None:
    """Same ids as `pageindex.utils.write_node_id`: preorder, zero-padded."""
    for i, node in enumerate(_preorder(tree)):
        node["node_id"] = str(i).zfill(4)


def _public_tree(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop bookkeeping keys and fix the key order PDF trees use."""
    order = ("title", "node_id", "start_index", "end_index", "summary", "nodes")
    out = []
    for node in tree:
        clean = {k: node[k] for k in order if k in node and k != "nodes"}
        if node.get("nodes"):
            clean["nodes"] = _public_tree(node["nodes"])
        out.append(clean)
    return out


# ───────────────────────────────────────────────────────────── summaries
def _own_texts(tree: list[dict[str, Any]], parsed: ParsedMarkdown) -> list[str]:
    """Each node's own text in preorder: from its heading to the next heading
    of any level (its children excluded). Page nodes use their page text."""
    nodes = _preorder(tree)
    texts = []
    for i, node in enumerate(nodes):
        if "_line" not in node:
            texts.append(parsed.pages[node["start_index"] - 1]
                         if node["start_index"] == node["end_index"] else "")
            continue
        start = node["_line"] - 1
        nxt = next((n["_line"] for n in nodes[i + 1:] if "_line" in n), None)
        end = (nxt - 1) if nxt is not None else len(parsed.lines)
        texts.append("\n".join(parsed.lines[start:end]).strip())
    return texts


def summarize(tree: list[dict[str, Any]], parsed: ParsedMarkdown, model: str,
              backend: dict[str, str] | None = None, concurrency: int = 8,
              describe: bool = True) -> str | None:
    """Fill `summary` on every node with PageIndex's own `summarize_tree`, and
    return a one-line document description (`generate_doc_description`).

    `summarize_tree` reads text by page range. Headings share pages, so page
    text would give sibling sections identical summaries; instead it runs on a
    shadow tree whose "pages" are the nodes' own section texts, in preorder.
    Leaves are then summarized from exactly their section, and a parent from
    its opening text plus its children's summaries — PageIndex's semantics,
    at section rather than page granularity."""
    from pageindex import utils

    nodes = _preorder(tree)
    virtual_pages = [(text + "\n", 0) for text in _own_texts(tree, parsed)]
    position = {id(n): i + 1 for i, n in enumerate(nodes)}

    def shadow(node: dict[str, Any]) -> dict[str, Any]:
        children = node.get("nodes") or []
        last = node
        while last.get("nodes"):
            last = last["nodes"][-1]
        out: dict[str, Any] = {"title": node["title"],
                               "start_index": position[id(node)],
                               "end_index": position[id(last)]}
        if children:
            out["nodes"] = [shadow(c) for c in children]
        return out

    shadow_tree = [shadow(n) for n in tree]
    token = utils._llm_backend.set(backend)
    try:
        asyncio.run(utils.summarize_tree(shadow_tree, virtual_pages, model=model,
                                         concurrency=concurrency))
        for node, twin in zip(nodes, _preorder(shadow_tree)):
            node["summary"] = twin.get("summary", "")
        if not describe:
            return None
        return utils.generate_doc_description(
            utils.create_clean_structure_for_description(_public_tree(tree)),
            model=model) or None
    finally:
        utils._llm_backend.reset(token)


# ───────────────────────────────────────────────────────────── store
@dataclass
class IndexResult:
    doc_id: str
    name: str
    pages: int
    nodes: int
    has_markers: bool
    skipped: bool = False


def _now_iso() -> str:
    """Same timestamp shape as `pageindex.local_api._now_iso`."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000).isoformat()


def index_markdown(md_path: Path, store_path: Path, *, summary_model: str | None = None,
                   backend: dict[str, str] | None = None, concurrency: int = 8,
                   page_chars: int = DEFAULT_PAGE_CHARS,
                   force: bool = False) -> IndexResult:
    """Index one Markdown file into the store. `summary_model=None` builds the
    tree without any LLM call (no summaries, no description).

    A document is identified by its file name: re-indexing replaces the stored
    copy, and is skipped when the content is unchanged and the stored copy
    already has what was asked for (summaries), unless `force`."""
    raw = md_path.read_bytes()
    markdown = raw.decode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    name = sanitize_filename(md_path.name)
    want_summary = summary_model is not None
    store = DocStore(str(store_path))

    previous = [m for m in store.list_metas() if m.get("name") == name]
    if not force:
        for meta in previous:
            info = meta.get("metadata") or {}
            if (meta.get("status") == "completed" and info.get("sha256") == digest
                    and (info.get("summary") or not want_summary)):
                return IndexResult(meta["id"], name, meta.get("pageNum", 0),
                                   int(info.get("node_count", 0)),
                                   bool(info.get("page_markers")), skipped=True)

    parsed = parse_pages(markdown, page_chars=page_chars)
    if not any(p.strip() for p in parsed.pages):
        raise ValueError(f"{md_path.name}: document has no content")
    tree = build_tree(parsed, doc_title=md_path.stem)
    description = None
    if want_summary:
        assert summary_model is not None
        description = summarize(tree, parsed, summary_model, backend=backend,
                                concurrency=concurrency)
    public = _public_tree(tree)
    node_count = len(_preorder(public))
    pages = [{"page_index": i + 1, "markdown": text}
             for i, text in enumerate(parsed.pages)]

    doc_id = "pi-" + uuid.uuid4().hex
    meta = {
        "id": doc_id,
        "name": name,
        "description": description,
        "status": "completed",
        "createdAt": _now_iso(),
        "pageNum": len(pages),
        "folderId": None,
        "metadata": {
            "source": "markdown",
            "source_file": md_path.name,
            "sha256": digest,
            "page_markers": parsed.has_markers,
            "summary": want_summary,
            "node_count": node_count,
        },
        "mode": "markdown",
    }
    with store.lock():
        store.save_document(doc_id, meta, public, pages)
        for old in previous:
            store.delete_document(old["id"])
    return IndexResult(doc_id, name, len(pages), node_count, parsed.has_markers)


def find_markdown(target: Path) -> list[Path]:
    """The Markdown files named by a path: the file itself, or every Markdown
    file under a directory (recursive, sorted)."""
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*")
                      if p.is_file() and p.suffix.lower() in MD_SUFFIXES)
    raise FileNotFoundError(f"No such file or directory: {target}")
