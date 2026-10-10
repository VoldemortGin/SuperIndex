"""Index Azure DI Markdown into a SuperIndexClient-compatible local store.

Input is Markdown from Azure Document Intelligence — either as written by
`extractors.azure_di` (a ``<!-- page: N -->`` line where each PDF page begins)
or DI's native output (``<!-- PageBreak -->`` between pages). Output is the
exact on-disk shape `SuperIndexClient` (local mode) reads, so its chat agent and
its tools (`get_document_structure`, `get_page_content`) work unchanged:

    <store>/docs/<doc_id>/pages.json   [{"page_index": 1, "markdown": ...}, ...]
    <store>/docs/<doc_id>/tree.json    [{"title", "node_id", "start_index",
                                         "end_index", "summary"?, "nodes"?}]
    <store>/docs/<doc_id>/doc.json     document metadata
    <store>/docs/<doc_id>/bm25.json    keyword index (`superindex.bm25`)
    <store>/docs/<doc_id>/page_tags.json  per-page tags (`superindex.page_images`)
    <store>/manifest.json

Page numbers: ``page_index`` is always the physical PDF page, so citations and
`get_page_content` agree. With ``<!-- page: N -->`` markers (which win when both
kinds are present; DI's own PageBreak comments are then redundant) N is the
page; pages with no marker (e.g. a partial extraction starting at page 5) are
kept as empty pages to hold that numbering. With only ``<!-- PageBreak -->``
pages are counted from 1. DI's printed page number (``<!-- PageNumber="x" -->``)
is kept as a label only (doc metadata ``page_labels``). A Markdown file with no
page marks at all is split into pseudo-pages of roughly ``page_chars``
characters at heading or blank-line boundaries (a short file becomes a single
page).

Every HTML comment — PageHeader / PageFooter / PageNumber / PageBreak and any
other — is dropped from the page text, headings and summaries. An HTML
``<table>`` cut by a page break is closed on its page and reopened, with its
header rows, on the next one; a table that ends one page and restarts
header-less on the next gets the header repeated. Text inside ``<table>`` and
``<figure>`` is never taken for a heading.

Only light modules are imported here: headings come from `nav.build`, storage
from `superindex.engine.local_store`. The PDF stack (PyPDF2 / pypdfium2 / flash) is not
touched, except `superindex.page_render` to count a linked PDF's pages;
`superindex.engine.utils` (which imports PyPDF2 at module level) is imported only when
LLM summaries are requested.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from superindex import bm25, page_images, tree_rules
from superindex.engine.errors import is_rate_limit_error
from superindex.engine.local_store import DocStore
from superindex.engine.naming import sanitize_filename
from superindex.nav.build import markdown_chapters
from superindex.nav.policy import YEAR_RE
from superindex.nav.store import Chapter

# Same marker `extractors.azure_di.PAGE_MARKER` writes ("<!-- page: {n} -->"),
# matched leniently on whitespace.
PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->", re.IGNORECASE)
COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
MARKER_BODY_RE = re.compile(r"\s*page:\s*(\d+)\s*", re.IGNORECASE)
PAGE_BREAK_BODY_RE = re.compile(r"\s*PageBreak\s*", re.IGNORECASE)
META_BODY_RE = re.compile(r'\s*(PageHeader|PageFooter|PageNumber)\s*=\s*"(.*)"\s*',
                          re.IGNORECASE | re.DOTALL)
TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
BLOCK_TAG_RE = re.compile(r"<(/?)(table|figure)\b[^>]*>", re.IGNORECASE)
TABLE_TAG_RE = re.compile(r"<(/?)(table|thead|tbody|tfoot|tr|th|td|caption)\b[^>]*>",
                          re.IGNORECASE)
DEFAULT_PAGE_CHARS = 4000
MD_SUFFIXES = {".md", ".markdown"}
TREE_SOURCES = ("flash", "markdown")   # chapter tree from the PDF layout, or from Markdown headings


# ───────────────────────────────────────────────────────────── pages
@dataclass
class PageMeta:
    """DI page furniture, kept out of the page text."""
    headers: list[str] = field(default_factory=list)
    footers: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)   # PageNumber labels

    @property
    def label(self) -> str | None:
        return self.numbers[0] if self.numbers else None


@dataclass
class ParsedMarkdown:
    lines: list[str]          # the document with comments removed
    line_pages: list[int]     # 1-based page of each line in `lines`
    pages: list[str]          # pages[i] is the content of page i + 1
    has_markers: bool
    page_mode: str = "pseudo"   # "marker" | "pagebreak" | "pseudo"
    page_meta: list[PageMeta] = field(default_factory=list)   # per page

    @property
    def page_labels(self) -> dict[int, str]:
        """Printed page label by physical page, where DI reported one."""
        return {i + 1: m.label for i, m in enumerate(self.page_meta) if m.label}


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def parse_pages(markdown: str, page_chars: int = DEFAULT_PAGE_CHARS) -> ParsedMarkdown:
    """Split Markdown into pages by its ``<!-- page: N -->`` markers, else by
    DI's ``<!-- PageBreak -->``, else into pseudo-pages. Comments are dropped."""
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if PAGE_MARKER_RE.search(text):
        mode = "marker"
    elif any(PAGE_BREAK_BODY_RE.fullmatch(m.group(1)) for m in COMMENT_RE.finditer(text)):
        mode = "pagebreak"
    else:
        mode = "pseudo"
    lines, line_pages, metas = _split_on_comments(text, mode)
    if mode == "pseudo":
        line_pages = _pseudo_pages(lines, page_chars)
    page_count = max(line_pages, default=1)
    page_meta = [PageMeta() for _ in range(page_count)]
    if mode != "pseudo":
        for page, kind, value in metas:
            if 1 <= page <= page_count:
                getattr(page_meta[page - 1], kind).append(value)
    pages = _repair_html_tables(_assemble_pages(lines, line_pages, page_count))
    return ParsedMarkdown(lines, line_pages, pages, mode != "pseudo", mode, page_meta)


def _split_on_comments(text: str, mode: str
                       ) -> tuple[list[str], list[int], list[tuple[int, str, str]]]:
    """Drop every HTML comment and tag each remaining line with its page.

    Page marks (``page: N`` in "marker" mode, ``PageBreak`` in "pagebreak"
    mode) move to a new page; a mark that shares a line with text splits that
    line: text before it stays on the old page, text after moves on. A line
    left blank by removing its comments disappears; a line that also holds
    text is kept, stripped. PageHeader/PageFooter/PageNumber values are
    returned as (page, kind, value) instead of text."""
    lines: list[str] = []
    pages: list[int | None] = []
    metas: list[tuple[int | None, str, str]] = []
    current: int | None = 1 if mode == "pagebreak" else None
    buf: list[str] = []
    touched = False    # the line under construction held a comment

    def end_line() -> None:
        nonlocal touched
        line = "".join(buf)
        buf.clear()
        if touched:
            if line.strip():
                lines.append(line.strip())
                pages.append(current)
        else:
            lines.append(line)
            pages.append(current)
        touched = False

    def add_text(chunk: str) -> None:
        parts = chunk.split("\n")
        for part in parts[:-1]:
            buf.append(part)
            end_line()
        buf.append(parts[-1])

    pos = 0
    for m in COMMENT_RE.finditer(text):
        add_text(text[pos:m.start()])
        pos = m.end()
        body = m.group(1)
        marker = MARKER_BODY_RE.fullmatch(body)
        touched = True
        if mode == "marker" and marker:
            end_line()
            touched = True
            current = max(1, int(marker.group(1)))
        elif mode == "pagebreak" and PAGE_BREAK_BODY_RE.fullmatch(body):
            end_line()
            touched = True
            current = (current or 1) + 1
        elif meta := META_BODY_RE.fullmatch(body):
            kind = {"pageheader": "headers", "pagefooter": "footers",
                    "pagenumber": "numbers"}[meta.group(1).lower()]
            metas.append((current, kind, html.unescape(meta.group(2).strip())))
    add_text(text[pos:])
    end_line()
    # Text ahead of the first marker belongs to the first marked page.
    first = next((p for p in pages if p is not None), 1)
    return (lines, [first if p is None else p for p in pages],
            [(first if p is None else p, k, v) for p, k, v in metas])


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


_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<(t[hd])\b([^>]*)>", re.IGNORECASE)
_COLSPAN_RE = re.compile(r"colspan\s*=\s*[\"']?(\d+)", re.IGNORECASE)
_THEAD_RE = re.compile(r"<thead\b[^>]*>.*?</thead\s*>", re.IGNORECASE | re.DOTALL)
_TABLE_OPEN_RE = re.compile(r"<table\b[^>]*>", re.IGNORECASE)


def _row_width(row: str) -> int:
    width = 0
    for cell in _CELL_RE.finditer(row):
        span = _COLSPAN_RE.search(cell.group(2))
        width += max(1, int(span.group(1))) if span else 1
    return width


def _is_header_row(row: str) -> bool:
    kinds = {c.group(1).lower() for c in _CELL_RE.finditer(row)}
    return kinds == {"th"}


def _table_header(table: str) -> str | None:
    """A table's header: its ``<thead>`` element, else its leading all-``<th>``
    rows; None when it has neither (or the header itself is cut off)."""
    opener = _TABLE_OPEN_RE.match(table)
    body = table[opener.end():] if opener else table
    thead = _THEAD_RE.search(body)
    first_row = _ROW_RE.search(body)
    if thead and (first_row is None or thead.start() <= first_row.start()):
        return thead.group(0)
    rows = []
    for row in _ROW_RE.finditer(body):
        if not _is_header_row(row.group(0)):
            break
        rows.append(row.group(0))
    return "\n".join(rows) or None


def _header_width(header: str) -> int:
    rows = _ROW_RE.findall(header)
    return _row_width(rows[-1]) if rows else 0


def _has_own_header(table_body: str, header: str) -> bool:
    """Whether a continuation table already starts with a header."""
    if _THEAD_RE.search(table_body[:2000]):
        return True
    first = _ROW_RE.search(table_body)
    if first is None:
        return False
    if _is_header_row(first.group(0)):
        return True
    def cells(row: str) -> str:
        return " ".join(re.sub(r"<[^>]+>", " ", row).split())

    head_rows = _ROW_RE.findall(header)
    return bool(head_rows) and cells(first.group(0)) == cells(head_rows[0])


def _open_table_state(page: str, stack: list[str]) -> tuple[list[str], int | None]:
    """Run the table-tag stack over one page. Returns the stack left open at
    the page end and the offset where the outermost still-open table began
    on this page (None if it was opened on an earlier page)."""
    stack = list(stack)
    start: int | None = None
    for m in TABLE_TAG_RE.finditer(page):
        tag = m.group(2).lower()
        if not m.group(1):
            if tag == "table" and "table" not in stack:
                start = m.start()
            stack.append(tag)
        elif tag in stack:
            while stack and stack.pop() != tag:
                pass
            if "table" not in stack:
                start = None
    return stack, start


def _repair_html_tables(pages: list[str]) -> list[str]:
    """Keep every page's HTML tables self-contained across page breaks.

    - A table left open at a page end is closed there and reopened at the top
      of the next page — ``<table>``, its header rows, then whatever row/cell
      was open — so both halves are valid tables and the second is readable.
    - A table that closes the page, followed by a header-less table opening
      the next page with the same column count, is taken as DI's split of one
      table: the header is repeated at the top of the continuation.
    """
    out = list(pages)
    carry: list[str] = []        # tags still open from the previous page
    header: str | None = None    # header of the table the previous page ended with
    for i, page in enumerate(pages):
        text = page
        if carry:
            inner = [t for t in carry[carry.index("table") + 1:] if t != "thead"]
            reopen = "<table>" + ("\n" + header if header and "thead" not in carry else "")
            reopen += "".join(f"<{t}>" for t in inner)
            text = reopen + "\n" + text
        elif header and text.lstrip().lower().startswith("<table"):
            lead = len(text) - len(text.lstrip())
            opener = _TABLE_OPEN_RE.match(text, lead)
            if opener:
                rest = text[opener.end():]
                first = _ROW_RE.search(rest)
                if (first and not _has_own_header(rest, header)
                        and _row_width(first.group(0)) == _header_width(header)):
                    text = text[:opener.end()] + "\n" + header + rest
        stack, start = _open_table_state(page, carry)
        if "table" in stack:
            table_src = text if start is None else page[start:]
            if start is not None or header is None:
                header = _table_header(table_src)
            text = text.rstrip() + "\n" + "".join(f"</{t}>" for t in reversed(stack))
            carry = stack
        else:
            carry = []
            stripped = text.rstrip()
            if stripped.lower().endswith("</table>"):
                last = stripped.lower().rfind("<table")
                header = _table_header(stripped[last:]) if last >= 0 else None
            else:
                header = None
        if not page.strip():   # an empty page (numbering gap) breaks continuity
            carry, header, text = [], None, page
        out[i] = text
    return out


def _heading_lines(lines: list[str]) -> list[str]:
    """`lines` with the inside of every closed ``<table>``/``<figure>`` blanked
    (same length, so line numbers hold), so no cell or figure text is taken
    for a heading. An element that never closes is left alone rather than
    swallow the rest of the document."""
    masked = list(lines)
    stack: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        for m in BLOCK_TAG_RE.finditer(line):
            tag = m.group(2).lower()
            if not m.group(1):
                stack.append((tag, i))
                continue
            while stack:
                open_tag, start = stack.pop()
                if open_tag == tag:
                    if not stack:
                        for j in range(start, i + 1):
                            masked[j] = ""
                    break
    return masked


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


def build_tree(parsed: ParsedMarkdown, doc_title: str, bold: bool = True) -> list[dict[str, Any]]:
    """Heading tree with page ranges. Each node's range covers its whole
    subtree — the heading's page through the last page before the next heading
    of the same or a higher level — as in the engine's PDF trees. Text before
    the first heading becomes a "Preface" node. A document with no headings
    gets one root node with a child per page. `bold`: a ``**bold**`` line
    counts as a heading."""
    chapters = markdown_chapters(_heading_lines(parsed.lines), bold=bold)
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
    """Same ids as `superindex.engine.utils.write_node_id`: preorder, zero-padded."""
    for i, node in enumerate(_preorder(tree)):
        node["node_id"] = str(i).zfill(4)


def build_doc_tree(parsed: ParsedMarkdown, doc_title: str, pdf: Path | None,
                   tree_source: str = "flash", pdf_pages: int | None = None,
                   complete: Callable[[str], str] | None = None,
                   ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The document's chapter tree and how it was built (``tree_source``
    asked, ``tree_builder`` used, ``tree_fallback`` reason, ``tree_group_llm``
    whether `complete` was given, and `tree_rules.flash_tree`'s log).

    ``"flash"`` with a linked `pdf`: the PDF layout tree
    (`tree_rules.flash_tree`), page text from the Markdown. A broken flash
    tree, flash failing, or a PDF whose page count (`pdf_pages`) differs from
    the Markdown's falls back to the Markdown headings
    — bold lines count only when the Markdown has no ``#`` heading, and fake
    titles are dropped (`tree_rules.drop_fake_titles`). ``"markdown"``, or no
    PDF: `build_tree` as is. `complete` (prompt -> reply) lets an over-flat
    flash top level be regrouped by the LLM (`tree_rules.group_top_level`)."""
    if tree_source not in TREE_SOURCES:
        raise ValueError(f"tree_source must be one of {TREE_SOURCES}, got {tree_source!r}")
    info: dict[str, Any] = {"tree_source": tree_source, "tree_builder": "markdown",
                            "tree_group_llm": complete is not None}
    if tree_source != "flash" or pdf is None:
        return build_tree(parsed, doc_title), info
    try:
        if pdf_pages is not None and pdf_pages != len(parsed.pages):
            tree, reason = None, f"page count differs: PDF {pdf_pages}, Markdown {len(parsed.pages)}"
        else:
            tree, reason = tree_rules.flash_tree(pdf, parsed.pages, _markdown_headings(parsed),
                                                 complete, info)
    except Exception as exc:  # noqa: BLE001 - the Markdown headings still give a tree
        tree, reason = None, f"flash failed: {type(exc).__name__}: {exc}"
    if tree is not None:
        _write_node_ids(tree)
        return tree, {**info, "tree_builder": "flash"}
    headings = any(HEADING_RE.match(line.strip()) for line in _heading_lines(parsed.lines))
    tree = build_tree(parsed, doc_title, bold=not headings)
    tree_rules.drop_fake_titles(tree)
    _write_node_ids(tree)
    return tree, {**info, "tree_fallback": str(reason)[:300]}


def _markdown_headings(parsed: ParsedMarkdown) -> list[tuple[int, str]]:
    """(page, title) of every Markdown heading, in document order."""
    return [(parsed.line_pages[c.start - 1], c.title)
            for ch in markdown_chapters(_heading_lines(parsed.lines)) for c, _ in ch.walk()]


def _needs_new_tree(info: dict[str, Any], tree_source: str, pdf: Path | None,
                    group_llm: bool) -> bool:
    """Whether a stored document's tree is from another `tree_source` setting
    and rebuilding would change it — to flash needs a PDF, back to Markdown
    only matters for a tree flash built; documents from before the setting
    count as "markdown" — or is a flash tree that wanted LLM regrouping
    (``tree_group_needed``) built with another `group_llm` setting."""
    if info.get("tree_source", "markdown") != tree_source:
        return pdf is not None or info.get("tree_builder") == "flash"
    return bool(info.get("tree_builder") == "flash" and info.get("tree_group_needed")
                and bool(info.get("tree_group_llm")) != group_llm)


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


# ───────────────────────────────────────────────────────────── rate limits
@dataclass
class RateLimitPolicy:
    """Waiting on rate limits, shared by all LLM calls of a run: sleep `wait`
    seconds and retry, at most `max_waits` times per call (0: no waiting).
    Once one call has used them all and is still limited the quota is taken as
    spent: `exhausted` is set and later calls do not wait (nor recover)."""
    wait: float = 60.0
    max_waits: int = 0
    exhausted: bool = False


def _wait_on_rate_limit(call: Callable[[], Any], policy: RateLimitPolicy | None) -> Any:
    """`call()`; on a rate-limit / quota error, wait and call again as
    `policy` says — then the error raises."""
    policy = policy or RateLimitPolicy()
    max_waits = 0 if policy.exhausted else policy.max_waits
    for attempt in range(max_waits + 1):
        try:
            return call()
        except Exception as exc:
            if not is_rate_limit_error(exc):
                raise
            if attempt >= max_waits:
                if max_waits and not policy.exhausted:
                    policy.exhausted = True
                    print(f"\n⚠ 已连续等待 {max_waits} 次仍被限流，判断为额度耗尽：本次运行后续文档不再等待，"
                          "失败的元数据/摘要下次运行重试", flush=True)
                raise
            brief = " ".join(f"{type(exc).__name__}: {exc}".split())[:160]
            print(f"\nLLM 额度不足/被限流，等待 {policy.wait:g}s 后重试（第 {attempt + 1}/{max_waits} 次）：{brief}",
                  flush=True)
            time.sleep(policy.wait)
    raise AssertionError("unreachable")


# ───────────────────────────────────────────────────────────── summaries
def _own_texts(tree: list[dict[str, Any]], parsed: ParsedMarkdown) -> list[str]:
    """Each node's own text in preorder: from its heading to the next heading
    of any level (its children excluded). Page nodes use their page text;
    flash nodes (``_anchor``) their pages cut at the titles (`tree_rules.own_text`)."""
    nodes = _preorder(tree)
    texts = []
    for i, node in enumerate(nodes):
        if "_anchor" in node:
            texts.append(tree_rules.own_text(node, nodes[i + 1] if i + 1 < len(nodes) else None,
                                             parsed.pages))
            continue
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
              describe: bool = True,
              rate_limit: RateLimitPolicy | None = None) -> str | None:
    """Fill `summary` on every node with the engine's own `summarize_tree`, and
    return a one-line document description (`generate_doc_description`).

    `summarize_tree` reads text by page range. Headings share pages, so page
    text would give sibling sections identical summaries; instead it runs on a
    shadow tree whose "pages" are the nodes' own section texts, in preorder.
    Leaves are then summarized from exactly their section, and a parent from
    its opening text plus its children's summaries — the engine's semantics,
    at section rather than page granularity. A rate-limit error waits and
    retries (`_wait_on_rate_limit`); nodes summarized before it are kept."""
    from superindex.engine import utils

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
        _wait_on_rate_limit(lambda: asyncio.run(utils.summarize_tree(
            shadow_tree, virtual_pages, model=model, concurrency=concurrency)), rate_limit)
        for node, twin in zip(nodes, _preorder(shadow_tree)):
            node["summary"] = twin.get("summary", "")
        if not describe:
            return None
        structure = utils.create_clean_structure_for_description(_public_tree(tree))
        return _wait_on_rate_limit(lambda: utils.generate_doc_description(structure, model=model),
                                   rate_limit) or None
    finally:
        utils._llm_backend.reset(token)


# ───────────────────────────────────────────────────────────── document metadata
DOC_META_FIELDS = ("company", "region", "period", "report_type", "description")
REPORT_TYPES = ("annual", "interim", "quarterly", "other")
DOC_META_RULES = 3   # version of the rule-based fields; older doc_meta is refreshed without LLM
_NULLS = {"", "null", "none", "n/a", "na", "unknown", "未知", "无", "不详"}
_HALF_RE = re.compile(r"(?i)(?<![a-z])H1(?!\d)|(?<![a-z0-9])1H(?![a-z])|(?<![a-z])HY(?![a-z])"
                      r"|上半年|半年|中期|interim|half[- ]?year|six months|6 months")
_H2_RE = re.compile(r"(?i)(?<![a-z])H2(?!\d)|(?<![a-z0-9])2H(?![a-z])|下半年|second half")
_QUARTER_RE = re.compile(r"(?i)(?<![a-z])Q([1-4])(?!\d)|(?<![a-z0-9])([1-4])Q(?![a-z])"
                         r"|第([一二三四1-4])季")
_FY_SHORT_RE = re.compile(r"(?i)(?<![a-z])FY\s?'?(\d{2})(?!\d)")
# A 2-digit year only counts next to a period token: FY22, YE22, HY22, 3Q22, Q3'22, 1H22, H1 22.
_SHORT_YEAR_RE = re.compile(r"(?i)(?<![a-z])(?:FY|YE|HY|Q[1-4]|[1-4]Q|[12]H|H[12])\s?['’_-]?\s?(\d{2})(?!\d)")
_MONTH_NAME = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
               r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# (year, month) spellings: Mar 2022 / Mar-22, 2022 Mar, 2022M03 / 2022-03 / 202203, 2022年3月, 03/2022
_MONTH_ABBR = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_RES = (
    (re.compile(rf"(?i)(?<![a-z]){_MONTH_NAME}(?![a-z])\.?\s?['’_-]?\s?((?:19|20)\d{{2}}|\d{{2}})(?!\d)"), 2, 1),
    (re.compile(rf"(?i)(?<!\d)((?:19|20)\d{{2}})\s?[_-]?\s?{_MONTH_NAME}(?![a-z])"), 1, 2),
    (re.compile(r"(?i)(?<!\d)((?:19|20)\d{2})(?:M(0?[1-9]|1[0-2])|[-_/.]?(0[1-9]|1[0-2]))(?!\d)"), 1, 2),
    (re.compile(r"(?<!\d)((?:19|20)\d{2})\s*年\s*(0?[1-9]|1[0-2])\s*月"), 1, 2),
    (re.compile(r"(?<!\d)(0[1-9]|1[0-2])[-/.]((?:19|20)\d{2})(?!\d)"), 2, 1),
)
_FY_WORD_RE = re.compile(r"(?i)(?<![a-z])(?:FY|YE)(?![a-z])|full[- ]?year|annual|全年|年度|年报")
_INTERIM_NAME_RE = re.compile(r"(?i)(?<![a-z])(?:IR|interim|H1|1H)(?![a-z])|中期|半年")
_ANNUAL_NAME_RE = re.compile(r"(?i)(?<![a-z])(?:AR|annual)(?![a-z])|年报|年度报告")
DOC_META_PROMPT = (
    "你在为财报文档库建立文档级元数据。根据文件名、章节大纲和开头内容，判断这份文档【本身】的报告主体与主报告期。\n"
    "注意：报告正文里会出现上年对比数、五年摘要、分部/地区章节、未来年份的计划等，这些年份和地区都不是文档本身的"
    "报告期或主体；以封面、标题、文件名体现的主报告期与发布主体为准。集团/合并报告的 region 写\"集团\"，"
    "不要因为有分部或地区章节就写成某个地区。\n"
    "只输出一个 JSON 对象，不要解释：\n"
    '{"company": "发布报告的公司名称", "region": "报告主体覆盖的地区（集团报告写\\"集团\\"）", '
    '"period": "主报告期：年度写 FY2024，上半年/中期写 1H2024", '
    '"report_type": "annual | interim | other", '
    '"description": "一句中文概括：主体、报告期、报告类型"}\n'
    "无法判断的字段填 null。\n\n"
    "文件名: {name}\n"
    "章节大纲:\n{outline}\n\n"
    "开头内容:\n{head}"
)


def _clean(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = " ".join(str(value).split())
    return None if text.lower() in _NULLS else text


def _months(raw: str) -> list[tuple[int, int]]:
    """(year, month) of every month spelling in `raw` (`_MONTH_RES`)."""
    out = []
    for pattern, year_group, month_group in _MONTH_RES:
        for m in pattern.finditer(raw):
            year = m.group(year_group)
            month = m.group(month_group) or m.group(month_group + 1)
            if not month.isdigit():
                month = str(_MONTH_ABBR.index(month[:3].lower()) + 1)
            out.append((int(year) if len(year) == 4 else 2000 + int(year), int(month)))
    return out


def normalize_period(text: str | None, report_type: str | None = None,
                     quarter_as_half: bool = False) -> str | None:
    """A reporting period in one spelling: ``FY2024`` (a year; also YE),
    ``1H2024`` (first half: H1 / 1H / HY / interim), ``2H2024``,
    ``2024Q3`` (a quarter), ``2024M03`` (a month); None without a year, or
    when it names two different years, quarters or months (ambiguous). Explicit
    period words win over a month, a month over a bare year. The year is the
    4-digit one in `text`, else a 2-digit one next to a period token
    (FY22, 3Q22, Q3'22, Mar-22); `report_type` "interim" implies 1H.
    `quarter_as_half` (a type published by half year, e.g. Factbook) reads
    Q2 as 1H and Q4 as FY."""
    raw = _clean(text)
    if raw is None:
        return None
    months = _months(raw)
    if m := YEAR_RE.search(raw):
        year = m.group(0)
    elif m := _SHORT_YEAR_RE.search(raw):
        year = "20" + m.group(1)
    elif months:
        year = str(months[0][0])
    else:
        return None
    quarters = {int(d) if d.isdigit() else "一二三四".index(d) + 1
                for q in _QUARTER_RE.finditer(raw) for d in q.groups() if d}
    if len(quarters) > 1 or len(set(months)) > 1 or len(set(YEAR_RE.findall(raw))) > 1:
        return None
    if quarters:
        q = quarters.pop()
        if quarter_as_half and q in (2, 4):
            return f"1H{year}" if q == 2 else f"FY{year}"
        return f"{year}Q{q}"
    if _H2_RE.search(raw):
        return f"2H{year}"
    if _HALF_RE.search(raw) or report_type == "interim":
        return f"1H{year}"
    if months and not _FY_WORD_RE.search(raw):
        return f"{months[0][0]}M{months[0][1]:02d}"
    return f"FY{year}"


def period_year(period: str | None) -> int | None:
    m = YEAR_RE.search(period or "")
    return int(m.group(0)) if m else None


def _load_policy() -> Any:
    from superindex.batch import _routing_policy

    return _routing_policy()


def normalize_report_type(value: str | None, period: str | None = None,
                          policy: Any = None) -> str | None:
    """annual / interim / quarterly / other, or a configured type (its key,
    or a text naming one of its aliases, e.g. "QMR"); from the period when
    not given."""
    raw = (_clean(value) or "").lower()
    if raw in REPORT_TYPES:
        return raw
    if raw:
        policy = _load_policy() if policy is None else policy
        if found := policy.report_type_from_name(raw):
            return found
        if _HALF_RE.search(raw):
            return "interim"
        if "quarter" in raw or "季" in raw:
            return "quarterly"
        if "annual" in raw or "年报" in raw or "年度" in raw:
            return "annual"
        return "other"
    if period and period.startswith("FY"):
        return "annual"
    if period and period.startswith("1H"):
        return "interim"
    return "quarterly" if period and re.fullmatch(r"\d{4}Q[1-4]", period) else None


def doc_meta_from_filename(name: str, source_path: str | None = None,
                           policy: Any = None) -> dict[str, Any]:
    """Rule-based metadata from a file name and its source folder alone:
    period (``AIA_AR2024.md`` -> FY2024, ``QMR Q3 2022.pdf`` -> 2022Q3) and
    report type — a configured type from the folder of `source_path` (path
    relative to the PDF root) or from the file name's aliases
    (`RoutingPolicy.report_type_for`), else annual / interim from AR / IR;
    `*_source` says where each came from. Company, region and description
    stay None."""
    policy = _load_policy() if policy is None else policy
    stem = Path(name).stem
    kind, kind_source = policy.report_type_for(source_path, name)
    if kind is None:
        kind = ("interim" if _INTERIM_NAME_RE.search(stem)
                else "annual" if _ANNUAL_NAME_RE.search(stem) else None)
    period = normalize_period(stem, kind, policy.quarter_as_half(kind))
    meta: dict[str, Any] = {"company": None, "region": None, "period": period,
                            "report_type": normalize_report_type(kind, period, policy),
                            "description": None}
    if kind_source:
        meta["report_type_source"] = kind_source
    if period:
        meta["period_source"] = "filename"
    if source_path:
        meta["source_path"] = source_path
        meta["source_folder"] = source_path.split("/", 1)[0] if "/" in source_path else None
    return meta


def parse_doc_meta_reply(reply: str | None) -> dict[str, Any]:
    """The JSON object in an LLM reply (fenced or bare), fields cleaned;
    {} when there is none."""
    text = reply or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {k: _clean(data.get(k)) for k in DOC_META_FIELDS}
    out["report_type"] = normalize_report_type(out["report_type"])
    out["period"] = normalize_period(out["period"], out["report_type"])
    if out["report_type"] is None:
        out["report_type"] = normalize_report_type(None, out["period"])
    return {k: v for k, v in out.items() if v}


def doc_meta_prompt(name: str, lines: list[str], head_lines: int = 60) -> str:
    """The extraction prompt: file name, `#`/`##` outline and the opening
    text (as `nav.build.summarize_files` builds its input)."""
    chapters = markdown_chapters(_heading_lines(lines))
    outline = [f"{'  ' * (depth - 1)}- {c.title}"
               for ch in chapters for c, depth in ch.walk() if depth <= 2][:40]
    body = [ln for ln in lines if ln.strip()][:head_lines]
    head = " ".join(" ".join(body).split())[:1200]
    return (DOC_META_PROMPT.replace("{name}", name)
            .replace("{outline}", "\n".join(outline) or "（无标题）")
            .replace("{head}", head))


def _complete(model: str, prompt: str, backend: dict[str, str] | None) -> str:
    from superindex.engine import utils

    token = utils._llm_backend.set(backend)
    try:
        return utils.llm_completion(model, prompt) or ""
    finally:
        utils._llm_backend.reset(token)


def _apply_rules(meta: dict[str, Any], fallback: dict[str, Any], policy: Any) -> dict[str, Any]:
    """`meta` with the rule-based fields of `fallback` (`doc_meta_from_filename`)
    on top: a report type from the folder or a configured alias, and a period
    the file name gives, win over the LLM's; another period is re-normalized
    (older spellings such as ``3Q2024``). Marks the rules version."""
    out = {**meta}
    if fallback.get("report_type_source"):
        out["report_type"] = fallback["report_type"]
        out["report_type_source"] = fallback["report_type_source"]
    elif out.pop("report_type_source", None) or not out.get("report_type"):
        out["report_type"] = fallback.get("report_type")
    if fallback.get("period"):
        out["period"], out["period_source"] = fallback["period"], "filename"
    elif out.get("period_source") == "filename" or out.get("source") == "filename":
        out["period"] = None          # the file name no longer gives one (e.g. now ambiguous)
        out.pop("period_source", None)
    else:
        out["period"] = normalize_period(out.get("period"), out.get("report_type"),
                                         policy.quarter_as_half(out.get("report_type")))
    if out["report_type"] == "interim" and (out["period"] or "").startswith("FY"):
        out["period"] = "1H" + out["period"][2:]
    for key in ("source_path", "source_folder"):
        if fallback.get(key):
            out[key] = fallback[key]
    out["rules"] = DOC_META_RULES
    return out


def extract_doc_meta(name: str, lines: list[str], model: str | None = None,
                     backend: dict[str, str] | None = None,
                     rate_limit: RateLimitPolicy | None = None,
                     source_path: str | None = None, policy: Any = None) -> dict[str, Any]:
    """Document metadata (`DOC_META_FIELDS`) from one LLM call when `model` is
    given; a field the LLM leaves out (or every field, when the call fails or
    there is no model) comes from `doc_meta_from_filename`, whose folder /
    configured report type and file-name period win over the LLM's
    (`_apply_rules`; `source_path` is the PDF's path relative to the PDF
    root). `source` says which: "llm", "llm+filename" or "filename"; `model`
    is the model tried. A rate-limit error waits and retries
    (`_wait_on_rate_limit`); a call that still fails sets `llm_failed`, so
    `_needs_doc_meta` tries it again."""
    policy = _load_policy() if policy is None else policy
    fallback = doc_meta_from_filename(name, source_path, policy)
    found: dict[str, Any] = {}
    error = None
    failed = False
    if model:
        prompt = doc_meta_prompt(name, lines)
        try:
            found = parse_doc_meta_reply(_wait_on_rate_limit(
                lambda: _complete(model, prompt, backend), rate_limit))
            if not found:
                error = "no JSON object in the reply"
        except Exception as exc:  # noqa: BLE001 - the file name still gives a period
            error = f"{type(exc).__name__}: {exc}"
            failed = True
    meta: dict[str, Any] = {k: found.get(k) or fallback.get(k) for k in DOC_META_FIELDS}
    if found.get("period"):
        meta["period_source"] = "llm"
    meta = _apply_rules(meta, fallback, policy)
    from_name = [k for k in DOC_META_FIELDS if meta.get(k) and meta[k] != found.get(k)]
    meta["source"] = ("filename" if not found
                      else "llm+filename" if from_name else "llm")
    meta["model"] = model
    if error:
        meta["error"] = error[:300]
    if failed:
        meta["llm_failed"] = True
    return meta


def refresh_doc_meta(current: dict[str, Any], name: str, source_path: str | None = None,
                     policy: Any = None) -> dict[str, Any]:
    """Stored metadata with the rule-based fields redone (`_apply_rules`) for
    the current rules and `source_path` — no LLM call; the LLM's company,
    region and description are kept."""
    policy = _load_policy() if policy is None else policy
    source_path = source_path or current.get("source_path")
    return _apply_rules(current, doc_meta_from_filename(name, source_path, policy), policy)


def _needs_doc_meta(info: dict[str, Any], model: str | None, force: bool = False) -> bool:
    """Whether a stored document should get (new) metadata: it has none, or
    `model` is set and has not been tried on it yet, or its call failed."""
    current = info.get("doc_meta")
    if force or not isinstance(current, dict):
        return True
    return bool(model) and (current.get("model") != model or bool(current.get("llm_failed")))


def _needs_rules(current: Any, source_path: str | None) -> bool:
    """Whether stored metadata lacks the current rules (`DOC_META_RULES`) or
    a newly known `source_path` — a rule-only refresh, no LLM call."""
    if not isinstance(current, dict):
        return False
    return (current.get("rules") != DOC_META_RULES
            or bool(source_path) and current.get("source_path") != source_path)


def _with_doc_meta(meta: dict[str, Any], doc_meta: dict[str, Any]) -> dict[str, Any]:
    """`meta` with `metadata.doc_meta` set; an empty description (or one that
    came from the previous doc_meta) takes the extracted one."""
    info = meta.get("metadata") or {}
    old = (info.get("doc_meta") or {}).get("description")
    updated = {**meta, "metadata": {**info, "doc_meta": doc_meta}}
    if doc_meta.get("description") and (not meta.get("description")
                                        or meta.get("description") == old):
        updated["description"] = doc_meta["description"]
    return updated


def _store_doc_meta(store: DocStore, meta: dict[str, Any], doc_meta: dict[str, Any]) -> None:
    doc_id = meta["id"]
    with store.lock():
        tree, pages = store.get_tree(doc_id), store.get_pages(doc_id)
        if tree is None or pages is None:
            return
        store.save_document(doc_id, _with_doc_meta(meta, doc_meta), tree, pages)


def _stored_lines(store: DocStore, doc_id: str) -> list[str]:
    pages = store.get_pages(doc_id) or []
    return "\n".join(str(p.get("markdown") or "") for p in pages).splitlines()


def _relative_source(pdf: Any, pdf_dir: Path) -> str | None:
    """`pdf` relative to `pdf_dir` (POSIX form), or None when it is not under it."""
    if not pdf:
        return None
    try:
        return Path(str(pdf)).expanduser().resolve().relative_to(
            Path(pdf_dir).expanduser().resolve()).as_posix()
    except (ValueError, OSError):
        return None


def pdf_source_path(md_path: Path, pdf_dir: Path, md_dir: Path | None = None,
                    pdfs: dict[str, list[Path]] | None = None) -> str | None:
    """The source PDF of a Markdown file, relative to `pdf_dir`: the
    ``.meta.json`` sidecar's ``source_path`` (or its ``source`` under
    `pdf_dir`), else the same relative path under `pdf_dir` as `md_path` has
    under `md_dir`, else the only PDF of that stem in `pdfs`
    (`page_images.pdf_index`); None when not found or ambiguous."""
    try:
        sidecar = json.loads(md_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sidecar = {}
    if isinstance(sidecar, dict):
        if sidecar.get("source_path"):
            return str(sidecar["source_path"])
        if found := _relative_source(sidecar.get("source"), pdf_dir):
            return found
    if md_dir is not None:
        try:
            rel = md_path.relative_to(md_dir).with_suffix(".pdf")
        except ValueError:
            rel = None
        if rel is not None and (Path(pdf_dir) / rel).is_file():
            return rel.as_posix()
    matches = (pdfs or {}).get(md_path.stem.lower()) or []
    return _relative_source(matches[0], pdf_dir) if len(matches) == 1 else None


def _stored_source_path(info: dict[str, Any], name: str, pdf_dir: Path,
                        pdfs: dict[str, list[Path]]) -> str | None:
    """A stored document's source PDF relative to `pdf_dir`: its linked
    ``pdf_path``, else the only PDF of its stem; None when not found or
    ambiguous."""
    if found := _relative_source(info.get("pdf_path"), pdf_dir):
        return found
    matches = pdfs.get(Path(name).stem.lower()) or []
    return _relative_source(matches[0], pdf_dir) if len(matches) == 1 else None


def same_name_sources(source_paths: list[str]) -> dict[str, list[str]]:
    """Source PDFs (relative paths) whose Markdown gets the same document
    name — the store keeps one document per name, so they replace each
    other: {document name: [paths]} for every name with more than one."""
    by_name: dict[str, list[str]] = {}
    for path in source_paths:
        name = sanitize_filename(Path(path).with_suffix(".md").name)
        by_name.setdefault(name, []).append(path)
    return {k: v for k, v in sorted(by_name.items()) if len(v) > 1}


def clash_names(source_paths: list[str]) -> dict[str, str]:
    """{source path: document name} for the source PDFs that share a
    document name (`same_name_sources`): the name gets its folder (relative
    to the PDF root) as a prefix, ``Factbook__Pack 2022.md`` — more of the
    folder path, ``2022__Factbook__Pack 2022.md``, when the folder itself
    is shared. Every other PDF keeps its plain name and is not listed."""
    out: dict[str, str] = {}
    for name, paths in same_name_sources(source_paths).items():
        parents = {p: PurePosixPath(p).parent.parts for p in paths}
        names: dict[str, str] = {}
        for depth in range(1, max(len(v) for v in parents.values()) + 1):
            names = {p: sanitize_filename("__".join((*parents[p][-depth:], name)))
                     for p in paths}
            if len(set(names.values())) == len(paths):
                break
        out.update(names)
    return out


def drop_clash_leftovers(store_path: Path, source_paths: list[str]) -> list[str]:
    """Delete the stored documents still under the plain name of a clash
    (`clash_names`): from before the prefixes, the PDF last indexed of the
    same name. Returns the deleted documents' names."""
    stale = set(same_name_sources(source_paths)) - set(clash_names(source_paths).values())
    store = DocStore(str(store_path))
    gone = [m for m in store.list_metas() if m.get("name") in stale]
    with store.lock():
        for meta in gone:
            store.delete_document(meta["id"])
    return [str(m["name"]) for m in gone]


def backfill_doc_meta(store_path: Path, *, model: str | None = None,
                      backend: dict[str, str] | None = None, force: bool = False,
                      rate_limit: RateLimitPolicy | None = None,
                      pdf_dir: Path | None = None,
                      ) -> list[tuple[str, dict[str, Any]]]:
    """Add document metadata to an existing store without rebuilding trees:
    every completed document that has none — or, with `model`, that `model`
    has not been tried on — gets `extract_doc_meta` from its stored text
    (`force`: every document). Any other document whose metadata predates the
    current rules, or lacks its source path, gets `refresh_doc_meta` (no LLM
    call). With `pdf_dir` the source path (folder → report type) is found
    from the linked PDF or the file name (`_stored_source_path`). Returns
    (name, doc_meta) per updated document."""
    store = DocStore(str(store_path))
    policy = _load_policy()
    pdfs = page_images.pdf_index(Path(pdf_dir)) if pdf_dir else {}
    done: list[tuple[str, dict[str, Any]]] = []
    for meta in sorted(store.list_metas(), key=lambda m: str(m.get("name"))):
        info = meta.get("metadata") or {}
        if meta.get("status") != "completed":
            continue
        name = str(info.get("source_file") or meta.get("name") or "")
        source = (_stored_source_path(info, name, Path(pdf_dir), pdfs) if pdf_dir else None) \
            or (info.get("doc_meta") or {}).get("source_path")
        if _needs_doc_meta(info, model, force):
            doc_meta = extract_doc_meta(name, _stored_lines(store, meta["id"]), model, backend,
                                        rate_limit, source, policy)
        elif _needs_rules(info.get("doc_meta"), source):
            doc_meta = refresh_doc_meta(info["doc_meta"], name, source, policy)
        else:
            continue
        _store_doc_meta(store, meta, doc_meta)
        done.append((str(meta.get("name")), doc_meta))
    return done


def doc_meta_stats(store_path: Path) -> str:
    """One line over the store's completed documents: documents per report
    type, with a period, and whose source folder gives no type."""
    metas = [m for m in DocStore(str(store_path)).list_metas() if m.get("status") == "completed"]
    found = [(m.get("metadata") or {}).get("doc_meta") or {} for m in metas]
    kinds: dict[str, int] = {}
    for d in found:
        kinds[d.get("report_type") or "未知"] = kinds.get(d.get("report_type") or "未知", 0) + 1
    no_source = sum(1 for d in found if not d.get("source_path"))
    unknown_folder = sum(1 for d in found if d.get("source_path")
                         and d.get("report_type_source") != "folder")
    return (f"{len(metas)} 个文档；报告类型 "
            + ("，".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "-")
            + f"；有期间 {sum(1 for d in found if d.get('period'))}"
            + f"；来源文件夹无法识别类型 {unknown_folder}，来源路径未知 {no_source}")


# ───────────────────────────────────────────────────────────── store
@dataclass
class IndexResult:
    doc_id: str
    name: str
    pages: int
    nodes: int
    has_markers: bool
    skipped: bool = False
    pdf: str | None = None
    warnings: list[str] = field(default_factory=list)
    doc_meta: dict[str, Any] | None = None
    doc_meta_added: bool = False    # skipped document, metadata extracted this call


def _now_iso() -> str:
    """Same timestamp shape as `superindex.engine.local_api._now_iso`."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000).isoformat()


def _link_pdf(pdf: Path | None, md_pages: int, page_mode: str,
              warnings: list[str]) -> dict[str, Any]:
    """Metadata fields for the source PDF (`superindex.page_images`), or {}
    when there is none or it cannot be used."""
    if pdf is None:
        return {}
    if page_mode == "pseudo":
        warnings.append(f"PDF {pdf.name} not linked: the Markdown has no page "
                        "markers, so its pages are not PDF pages")
        return {}
    try:
        info = page_images.pdf_metadata(pdf)
    except Exception as exc:  # noqa: BLE001 - no images, the text still indexes
        warnings.append(f"PDF {pdf} not linked: {type(exc).__name__}: {exc}")
        return {}
    if info["pdf_pages"] != md_pages:
        warnings.append(f"page count differs: PDF {info['pdf_pages']}, "
                        f"Markdown {md_pages}")
    return info


def index_markdown(md_path: Path, store_path: Path, *, summary_model: str | None = None,
                   backend: dict[str, str] | None = None, concurrency: int = 8,
                   page_chars: int = DEFAULT_PAGE_CHARS,
                   force: bool = False, pdf: Path | None = None,
                   doc_meta: bool = True, doc_meta_model: str | None = None,
                   rate_limit: RateLimitPolicy | None = None,
                   source_path: str | None = None, name: str | None = None,
                   tree_source: str = "flash", tree_group_llm: bool = True) -> IndexResult:
    """Index one Markdown file into the store. `summary_model=None` builds the
    tree without any LLM call (no summaries, no description). `pdf` is the
    PDF the Markdown was extracted from (see `superindex.page_images`).
    `doc_meta` stores document metadata (`extract_doc_meta`: company, region,
    period, report type) in ``metadata.doc_meta`` — with one `doc_meta_model`
    call when given, else from the file name — and fills an empty description.
    `source_path` is the source PDF's path relative to the PDF root (its
    folder gives the report type, `RoutingPolicy.report_type_for`). `name`
    is the document name when not the file name (`clash_names`).
    `tree_source` (`TREE_SOURCES`) builds the chapter tree from the PDF layout
    (flash, needs `pdf`) or from the Markdown headings (`build_doc_tree`);
    `tree_group_llm` lets the summary model regroup an over-flat flash top
    level — only when summaries are written (`summary_model`).

    A document is identified by its name: re-indexing replaces the stored
    copy, and is skipped when the content is unchanged and the stored copy
    already has what was asked for (summaries, a tree from `tree_source` —
    `_needs_new_tree`), unless `force`. A skipped
    document still gets a new or changed `pdf` linked, missing document
    metadata added and outdated rule-based metadata redone (`refresh_doc_meta`,
    no LLM call), and other local copies of the same name deleted. Every LLM call waits and retries on a
    rate-limit error as `rate_limit` says (None: fail as before)."""
    raw = md_path.read_bytes()
    markdown = raw.decode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    name = sanitize_filename(name or md_path.name)
    want_summary = summary_model is not None
    store = DocStore(str(store_path))

    previous = [m for m in store.list_metas() if m.get("name") == name]
    if not force:
        for meta in previous:
            info = meta.get("metadata") or {}
            if (meta.get("status") == "completed" and info.get("sha256") == digest
                    and (info.get("summary") or not want_summary)
                    and not _needs_new_tree(info, tree_source, pdf,
                                            tree_group_llm and want_summary)):
                if extra := [m for m in previous if m["id"] != meta["id"]]:
                    # e.g. a store restored from PERSIST_DIR next to a newer local copy
                    with store.lock():
                        for old in extra:
                            store.delete_document(old["id"])
                    print(f"{name}: 本地库有 {len(previous)} 份同名文档，已删除其余 {len(extra)} 份"
                          f"（保留内容一致的 {meta['id']}）", flush=True)
                bm25.ensure_index(store, meta["id"])   # stores from before bm25.json
                page_images.load_tags(store_path, meta["id"])
                warnings: list[str] = []
                linked = _link_pdf(pdf, meta.get("pageNum", 0),
                                   info.get("page_mode") or "marker", warnings)
                if linked and any(info.get(k) != v for k, v in linked.items()):
                    _relink(store, meta, linked)
                found = info.get("doc_meta")
                added = False
                if doc_meta and _needs_doc_meta(info, doc_meta_model):
                    found = extract_doc_meta(md_path.name, parse_pages(
                        markdown, page_chars=page_chars).lines, doc_meta_model, backend,
                        rate_limit, source_path or (found or {}).get("source_path"))
                    _store_doc_meta(store, store.get_meta(meta["id"]) or meta, found)
                    added = True
                elif doc_meta and _needs_rules(found, source_path):
                    found = refresh_doc_meta(found, md_path.name, source_path)
                    _store_doc_meta(store, store.get_meta(meta["id"]) or meta, found)
                    added = True
                return IndexResult(meta["id"], name, meta.get("pageNum", 0),
                                   int(info.get("node_count", 0)),
                                   bool(info.get("page_markers")), skipped=True,
                                   pdf=linked.get("pdf_path") or info.get("pdf_path"),
                                   warnings=warnings, doc_meta=found,
                                   doc_meta_added=added)

    parsed = parse_pages(markdown, page_chars=page_chars)
    if not any(p.strip() for p in parsed.pages):
        raise ValueError(f"{md_path.name}: document has no content")
    warnings: list[str] = []
    linked = _link_pdf(pdf, len(parsed.pages), parsed.page_mode, warnings)
    complete: Callable[[str], str] | None = None
    if tree_group_llm and summary_model is not None:
        model = summary_model
        complete = lambda prompt: _wait_on_rate_limit(
            lambda: _complete(model, prompt, backend), rate_limit)
    tree, tree_info = build_doc_tree(parsed, md_path.stem, pdf if linked else None, tree_source,
                                     linked.get("pdf_pages"), complete)
    if tree_info.get("tree_fallback") and not tree_info["tree_fallback"].startswith("page count"):
        warnings.append(f"tree from Markdown headings, not flash: {tree_info['tree_fallback']}")
    description = None
    if want_summary:
        assert summary_model is not None
        description = summarize(tree, parsed, summary_model, backend=backend,
                                concurrency=concurrency, rate_limit=rate_limit)
    found = extract_doc_meta(md_path.name, parsed.lines, doc_meta_model, backend,
                             rate_limit, source_path) if doc_meta else None
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
            "page_mode": parsed.page_mode,
            "page_labels": {str(k): v for k, v in parsed.page_labels.items()},
            "summary": want_summary,
            "node_count": node_count,
            **tree_info,
            **linked,
        },
        "mode": "markdown",
    }
    if found is not None:
        meta = _with_doc_meta(meta, found)
    # The keyword index and page tags go in first: doc.json, written last by
    # save_document, is what makes the document visible.
    doc_dir = Path(store_path).expanduser() / "docs" / doc_id
    bm25.write_index(doc_dir, parsed.pages)
    page_images.write_tags(doc_dir, parsed.pages)
    with store.lock():
        store.save_document(doc_id, meta, public, pages)
        for old in previous:
            store.delete_document(old["id"])
    return IndexResult(doc_id, name, len(pages), node_count, parsed.has_markers,
                       pdf=linked.get("pdf_path"), warnings=warnings, doc_meta=found)


def _relink(store: DocStore, meta: dict[str, Any], linked: dict[str, Any]) -> None:
    """Record a new or changed PDF on an indexed document, text untouched."""
    doc_id = meta["id"]
    with store.lock():
        tree, pages = store.get_tree(doc_id), store.get_pages(doc_id)
        if tree is None or pages is None:
            return
        updated = {**meta, "metadata": {**(meta.get("metadata") or {}), **linked}}
        page_images.clear_images(store._root, doc_id)
        store.save_document(doc_id, updated, tree, pages)


def find_markdown(target: Path) -> list[Path]:
    """The Markdown files named by a path: the file itself, or every Markdown
    file under a directory (recursive, sorted)."""
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(p for p in target.rglob("*")
                      if p.is_file() and p.suffix.lower() in MD_SUFFIXES)
    raise FileNotFoundError(f"No such file or directory: {target}")
