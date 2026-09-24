"""Keyword search over a superindex store: Okapi BM25, standard library only.

The unit is the page — what `get_page_content` reads and what answers cite —
so a hit hands the agent a page to open directly; the section it sits in comes
from the document tree (the deepest node whose page range holds the page).

Two ways to score a page (``match``, env ``SUPERINDEX_BM25_MATCH``):

* ``page`` (default) — BM25 over whole pages.
* ``passage`` — BM25 over passages of about 300–800 characters cut from each
  page (`split_passages`); a page scores its best passage plus
  ``page_weight`` × its whole-page score. A few matching lines on an
  otherwise long page are then not diluted by the page's length. Hits are
  still pages; the snippet comes from the best passage.

One index per document, next to its other files:

    <store>/docs/<doc_id>/bm25.json
        {"version", "lengths": [page 1 length, ...],
         "postings": {term: [page, tf, page, tf, ...]},
         "passages": {"pages": [page of passage 0, ...], "lengths": [...],
                      "postings": {term: [passage, tf, ...]}}}

Only raw counts are stored; IDF and the average page (passage) length are
computed at query time over the documents being searched, so a cross-document
search ranks all pages on one scale. A store written before this index (or
this version of it) existed is indexed lazily on first search (and on the next
`index` run).

Tokens: NFKC-normalized and lower-cased; runs of letters/digits as words
(``hk$``/``us$`` keep their dollar sign, ``1,234.5`` becomes ``1234.5``, a
trailing ``%`` is dropped), CJK as single characters plus bigrams. HTML tags
and comments are stripped first. Common English function words are dropped.
"""
from __future__ import annotations

import bisect
import html
import json
import math
import os
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pageindex.local_store import DocStore

INDEX_FILE = "bm25.json"
VERSION = 2            # 2: adds "passages"
K1 = 1.5
B = 0.75
SNIPPET_CHARS = 300
MATCH_ENV = "SUPERINDEX_BM25_MATCH"
MATCH_MODES = ("page", "passage")
PASSAGE_MIN = 300      # characters of plain text a passage grows to before a heading ends it
PASSAGE_MAX = 800      # blocks are merged up to this; longer blocks are cut
PAGE_WEIGHT = 0.0      # passage mode: score = best passage + PAGE_WEIGHT × page score

_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"   # CJK ideographs (+ Ext A, compat)
_TOKEN_RE = re.compile(
    rf"[a-z]{{1,3}}\$"                      # currency prefix: hk$, us$, rmb$
    r"|\d+(?:[.,]\d+)*"                    # numbers: 2023, 12.5, 1,234.5
    r"|[a-z0-9]+"                           # words, alnum codes: fy2023, q3
    rf"|[{_CJK}]+"
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CELL_END_RE = re.compile(r"</t[hd]\s*>", re.IGNORECASE)
_ROW_END_RE = re.compile(r"</tr\s*>|<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does", "for", "from",
    "how", "in", "is", "it", "its", "of", "on", "or", "the", "to", "was", "were", "what",
    "when", "where", "which", "who", "why", "with"})


# ───────────────────────────────────────────────────────────── text
def plain_text(text: str) -> str:
    """Page Markdown without comments or HTML tags; table cells become
    `` | ``-separated, rows become lines."""
    text = _COMMENT_RE.sub(" ", text)
    text = _CELL_END_RE.sub(" | ", text)
    text = _ROW_END_RE.sub("\n", text)
    return html.unescape(_TAG_RE.sub(" ", text))


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def tokenize(text: str, *, html_input: bool = True) -> list[str]:
    if html_input:
        text = plain_text(text)
    tokens: list[str] = []
    for m in _TOKEN_RE.finditer(_normalize(text)):
        tok = m.group(0)
        if not tok[0].isascii():                 # a CJK run
            tokens.extend(tok)
            tokens.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        elif tok[0].isdigit():
            tokens.append(tok.replace(",", ""))
        elif tok not in STOPWORDS:
            tokens.append(tok)
    return tokens


# ───────────────────────────────────────────────────────────── passages
_HTML_TABLE_RE = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr\b.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th\b", re.IGNORECASE)
_PIPE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?$")
_SENTENCE_END_RE = re.compile(r"(?<=[。！？；!?;])|(?<=\.)(?=\s)")


def _size(text: str) -> int:
    """Length of a block as read: plain text, whitespace runs as one."""
    return len(" ".join(plain_text(text).split()))


def _blocks(text: str) -> list[tuple[int, int, str]]:
    """(start, end, kind) of the page's blocks: ``table`` (a whole HTML or
    pipe table), ``heading`` (one heading line) or ``text`` (a paragraph up
    to a blank line)."""
    blocks: list[tuple[int, int, str]] = []
    para: list[int] | None = None          # [start, end] of the open paragraph

    def close() -> None:
        nonlocal para
        if para is not None:
            blocks.append((para[0], para[1], "text"))
            para = None

    def lines(lo: int, hi: int) -> None:
        nonlocal para
        pos = lo
        for line in text[lo:hi].splitlines(keepends=True):
            start, end = pos, pos + len(line.rstrip("\r\n"))
            pos += len(line)
            stripped = line.strip()
            if not stripped:
                close()
            elif stripped.startswith("|"):
                close()
                if blocks and blocks[-1][2] == "table" and \
                        not text[blocks[-1][1]:start].strip():
                    blocks[-1] = (blocks[-1][0], end, "table")
                else:
                    blocks.append((start, end, "table"))
            elif _HEADING_RE.fullmatch(line.rstrip("\r\n")):
                close()
                blocks.append((start, end, "heading"))
            elif para is None:
                para = [start, end]
            else:
                para[1] = end
        close()

    pos = 0
    for m in _HTML_TABLE_RE.finditer(text):
        lines(pos, m.start())
        blocks.append((m.start(), m.end(), "table"))
        pos = m.end()
    lines(pos, len(text))
    return blocks


def _cut_table(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """A table longer than PASSAGE_MAX as groups of rows, each group prefixed
    with the table's header rows."""
    block = text[start:end]
    if block.lstrip().startswith("|"):
        rows, pos = [], start
        for line in block.splitlines(keepends=True):
            if line.strip():
                rows.append((pos, pos + len(line.rstrip("\r\n")), line.strip()))
            pos += len(line)
        n_head = 2 if len(rows) > 1 and _PIPE_SEP_RE.match(rows[1][2]) else 0
        head, body, sep = [r[2] for r in rows[:n_head]], rows[n_head:], "\n"
        wrap = ("", "")
    else:
        found = [(start + m.start(), start + m.end(), m.group(0))
                 for m in _TR_RE.finditer(block)]
        n_head = next((i for i, r in enumerate(found) if not _TH_RE.search(r[2])), len(found))
        n_head = n_head or 1                         # no <th>: the first row is the header
        if n_head >= len(found):
            n_head = 0
        head, body, sep = [r[2] for r in found[:n_head]], found[n_head:], ""
        wrap = ("<table>", "</table>")
    if not body:
        return [(start, end, block)]
    pieces: list[tuple[int, int, str]] = []
    group: list[tuple[int, int, str]] = []
    budget = PASSAGE_MAX - _size(sep.join(head))

    def emit() -> None:
        text_ = wrap[0] + sep.join(head + [r[2] for r in group]) + wrap[1]
        pieces.append((group[0][0], group[-1][1], text_))

    size = 0
    for row in body:
        row_size = _size(row[2]) + 1
        if group and size + row_size > budget:
            emit()
            group, size = [], 0
        group.append(row)
        size += row_size
    emit()
    return pieces


def _cut_text(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """A paragraph longer than PASSAGE_MAX in pieces at sentence ends (hard
    cuts only inside an over-long sentence)."""
    pieces: list[tuple[int, int, str]] = []
    lo = hi = start
    for part in _SENTENCE_END_RE.split(text[start:end]):
        if not part:
            continue
        if hi > lo and _size(text[lo:hi + len(part)]) > PASSAGE_MAX:
            pieces.append((lo, hi, text[lo:hi]))
            lo = hi
        hi += len(part)
        while hi - lo > PASSAGE_MAX and _size(text[lo:hi]) > PASSAGE_MAX:
            pieces.append((lo, lo + PASSAGE_MAX, text[lo:lo + PASSAGE_MAX]))
            lo += PASSAGE_MAX
    if hi > lo:
        pieces.append((lo, hi, text[lo:hi]))
    return pieces


def split_passages(page_markdown: str) -> list[tuple[int, int, str]]:
    """(start, end, text) of the page's passages, in page order.

    Blocks (paragraphs, headings, whole tables) are merged in order up to
    PASSAGE_MAX characters of plain text; a heading starts a new passage once
    the open one holds PASSAGE_MIN characters, and always stays with what
    follows it. A block longer than PASSAGE_MAX is cut: a table into row
    groups that each repeat the header, text at sentence ends."""
    text = page_markdown
    out: list[tuple[int, int, str]] = []
    cur: list[int] | None = None        # [start, end] of the open passage
    cur_size, cur_body = 0, False

    def flush() -> None:
        nonlocal cur, cur_size, cur_body
        if cur is not None and text[cur[0]:cur[1]].strip():
            out.append((cur[0], cur[1], text[cur[0]:cur[1]]))
        cur, cur_size, cur_body = None, 0, False

    for start, end, kind in _blocks(text):
        size = _size(text[start:end])
        if kind == "heading":
            if cur_body and cur_size >= PASSAGE_MIN:
                flush()
        elif size > PASSAGE_MAX:
            cut = _cut_table if kind == "table" else _cut_text
            pieces = cut(text, start, end)
            if cur is not None and not cur_body:     # headings ride on the first piece
                _, e0, t0 = pieces[0]
                pieces[0] = (cur[0], e0, text[cur[0]:start] + t0)
                cur, cur_size = None, 0
            else:
                flush()
            out.extend(pieces)
            continue
        elif cur_body and cur_size + size > PASSAGE_MAX:
            flush()
        if cur is None:
            cur = [start, end]
        cur[1] = end
        cur_size += size
        cur_body = cur_body or kind != "heading"
    flush()
    return out


# ───────────────────────────────────────────────────────────── index
def _count(texts: list[str]) -> tuple[list[int], dict[str, list[int]]]:
    lengths: list[int] = []
    postings: dict[str, list[int]] = {}
    for number, text in enumerate(texts):
        counts = Counter(tokenize(text))
        lengths.append(sum(counts.values()))
        for term, tf in counts.items():
            postings.setdefault(term, []).extend((number, tf))
    return lengths, postings


def build_index(pages: list[str]) -> dict[str, Any]:
    """BM25 index of one document; ``pages[i]`` is page i + 1."""
    lengths, postings = _count(pages)
    for posting in postings.values():
        posting[::2] = [p + 1 for p in posting[::2]]
    passage_pages: list[int] = []
    passage_texts: list[str] = []
    for number, text in enumerate(pages, start=1):
        for _, _, piece in split_passages(text):
            passage_pages.append(number)
            passage_texts.append(piece)
    p_lengths, p_postings = _count(passage_texts)
    return {"version": VERSION, "lengths": lengths, "postings": postings,
            "passages": {"pages": passage_pages, "lengths": p_lengths,
                         "postings": p_postings}}


def write_index(doc_dir: Path, pages: list[str]) -> dict[str, Any]:
    """Build and atomically write ``bm25.json`` into a document folder."""
    index = build_index(pages)
    doc_dir.mkdir(parents=True, exist_ok=True)
    path = doc_dir / INDEX_FILE
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return index


_CACHE: dict[str, tuple[int, dict[str, Any]]] = {}   # path -> (mtime_ns, index)
_CACHE_LIMIT = 64


def _load_index(doc_dir: Path) -> dict[str, Any] | None:
    """Read ``bm25.json``, reusing the parsed copy while the file is unchanged
    (a long-running `serve` otherwise re-parses every index per question)."""
    path = doc_dir / INDEX_FILE
    try:
        mtime = path.stat().st_mtime_ns
        cached = _CACHE.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != VERSION:
        return None
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[str(path)] = (mtime, data)
    return data


def ensure_index(store: DocStore, doc_id: str) -> tuple[dict[str, Any] | None, bool]:
    """The document's index, built (and saved, best effort) when missing or
    stale. Returns (index or None if the document has no pages, built now)."""
    doc_dir = Path(store._root) / "docs" / doc_id
    index = _load_index(doc_dir)
    if index is not None:
        return index, False
    pages = store.get_pages(doc_id)
    if not isinstance(pages, list):
        return None, False
    texts = _page_texts(pages)
    try:
        return write_index(doc_dir, texts), True
    except OSError:
        return build_index(texts), True


def _page_texts(pages: list[Any]) -> list[str]:
    by_index = {p["page_index"]: p.get("markdown") or "" for p in pages
                if isinstance(p, dict) and isinstance(p.get("page_index"), int)}
    return [by_index.get(i, "") for i in range(1, max(by_index, default=0) + 1)]


# ───────────────────────────────────────────────────────────── search
@dataclass
class Hit:
    doc_id: str
    doc_name: str
    page: int
    score: float
    section: str = ""
    node_id: str = ""
    page_label: str | None = None
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["score"] = round(self.score, 3)
        if out["page_label"] is None:
            del out["page_label"]
        return out


@dataclass
class SearchResult:
    hits: list[Hit]
    searched: int            # documents searched
    built: list[str]         # names of documents indexed lazily just now
    match: str = "page"


def resolve_match(match: str | None = None) -> str:
    """`match`, else ``SUPERINDEX_BM25_MATCH``, else ``page``."""
    value = (match or os.environ.get(MATCH_ENV) or "page").strip().lower()
    if value not in MATCH_MODES:
        raise ValueError(f"{MATCH_ENV}: expected one of {', '.join(MATCH_MODES)}, "
                         f"got {value!r}")
    return value


def _bm25(terms: Counter[str], views: dict[str, tuple[list[int], dict[str, list[int]]]],
          base: int) -> dict[tuple[str, int], float]:
    """BM25 score of every unit (page or passage) holding a query term; units
    are numbered from `base` in the postings. IDF and the average length come
    from all units in `views` ({doc_id: (lengths, postings)})."""
    n_units = sum(sum(1 for n in lengths if n) for lengths, _ in views.values())
    if not n_units:
        return {}
    avgdl = sum(sum(lengths) for lengths, _ in views.values()) / n_units
    scores: dict[tuple[str, int], float] = {}
    for term, qtf in terms.items():
        df = sum(len(postings.get(term, ())) // 2 for _, postings in views.values())
        if not df:
            continue
        idf = math.log(1 + (n_units - df + 0.5) / (df + 0.5))
        for doc_id, (lengths, postings) in views.items():
            posting = postings.get(term)
            if not posting:
                continue
            for j in range(0, len(posting), 2):
                unit, tf = posting[j], posting[j + 1]
                norm = tf + K1 * (1 - B + B * lengths[unit - base] / avgdl)
                key = (doc_id, unit)
                scores[key] = scores.get(key, 0.0) + qtf * idf * tf * (K1 + 1) / norm
    return scores


def search(store_path: str | os.PathLike[str], query: str, *,
           doc_ids: list[str] | None = None, top_k: int = 5, match: str | None = None,
           page_weight: float = PAGE_WEIGHT) -> SearchResult:
    """Top pages for `query` across the store's completed documents, or only
    those in `doc_ids`. `match` (default: `resolve_match`) scores whole pages
    or a page's best passage (+ `page_weight` × its page score)."""
    match = resolve_match(match)
    store = DocStore(str(store_path))
    metas = [m for m in store.list_metas() if m.get("status") == "completed"]
    if doc_ids is not None:
        allowed = set(doc_ids)
        metas = [m for m in metas if m["id"] in allowed]
    terms = Counter(tokenize(query, html_input=False))
    if not terms or not metas:
        return SearchResult([], len(metas), [], match)

    indexes: dict[str, dict[str, Any]] = {}
    built: list[str] = []
    for meta in metas:
        index, fresh = ensure_index(store, meta["id"])
        if index is not None:
            indexes[meta["id"]] = index
            if fresh:
                built.append(meta["name"])

    scores = _bm25(terms, {d: (ix["lengths"], ix["postings"]) for d, ix in indexes.items()}, 1)
    best: dict[tuple[str, int], int] = {}     # page -> its best passage's number on the page
    if match == "passage":
        passage_scores = _bm25(terms, {d: (ix["passages"]["lengths"],
                                           ix["passages"]["postings"])
                                       for d, ix in indexes.items()}, 0)
        top: dict[tuple[str, int], tuple[float, int]] = {}
        for (doc_id, i), value in passage_scores.items():
            key = (doc_id, indexes[doc_id]["passages"]["pages"][i])
            if key not in top or value > top[key][0] or (value == top[key][0]
                                                          and i < top[key][1]):
                top[key] = (value, i)
        for key, (value, i) in top.items():
            best[key] = i - bisect.bisect_left(indexes[key[0]]["passages"]["pages"], key[1])
        scores = {key: top.get(key, (0.0, 0))[0] + page_weight * value
                  for key, value in scores.items()}

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    names = {m["id"]: m["name"] for m in metas}
    labels = {m["id"]: ((m.get("metadata") or {}).get("page_labels") or {}) for m in metas}
    hits = [Hit(doc_id, names[doc_id], page, score,
                page_label=labels[doc_id].get(str(page)))
            for (doc_id, page), score in ranked[:max(1, top_k)]]
    _decorate(store, hits, query, best if match == "passage" else None)
    return SearchResult(hits, len(metas), built, match)


def _decorate(store: DocStore, hits: list[Hit], query: str,
              passages: dict[tuple[str, int], int] | None = None) -> None:
    """Section titles from the tree, snippets from the page text — or, with
    `passages` ({(doc_id, page): passage number on the page}), from that
    passage."""
    trees: dict[str, list[Any]] = {}
    pages: dict[str, list[str]] = {}
    pattern = highlight_pattern(query)
    for hit in hits:
        if hit.doc_id not in trees:
            trees[hit.doc_id] = store.get_tree(hit.doc_id) or []
            pages[hit.doc_id] = _page_texts(store.get_pages(hit.doc_id) or [])
        texts = pages[hit.doc_id]
        text = texts[hit.page - 1] if 0 < hit.page <= len(texts) else ""
        focus = text
        pos = _best_window(text, pattern, SNIPPET_CHARS)
        if passages is not None and (hit.doc_id, hit.page) in passages:
            pieces = split_passages(text)
            number = passages[(hit.doc_id, hit.page)]
            if 0 <= number < len(pieces):
                start, end, focus = pieces[number]
                pos = min(start + _best_window(focus, pattern, SNIPPET_CHARS), end)
        heading = _heading_before(text, pos)
        hit.section, hit.node_id = section_for(trees[hit.doc_id], hit.page, heading)
        hit.snippet = snippet(focus, pattern)


_HEADING_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+(.+?)|\*\*(.+?)\*\*)[ \t]*$", re.MULTILINE)


def _heading_before(page_markdown: str, pos: int) -> str | None:
    """Title of the last heading on the page at or before `pos` (the match),
    or None when the match sits above the page's first heading."""
    title = None
    for m in _HEADING_RE.finditer(page_markdown):
        if m.start() > pos:
            break
        title = (m.group(1) or m.group(2)).strip()
    return title


def section_for(tree: list[Any], page: int, heading: str | None = None) -> tuple[str, str]:
    """(``"Chapter > Section"``, node_id) of the section a hit on `page` is in.

    With `heading` (the last heading above the match on that page), the node
    of that title starting on this page. Otherwise — or if none matches — the
    deepest node covering the page, preferring one that started on an earlier
    page when the match sits above the page's first heading (`heading` None),
    and among equally deep ones the one that starts latest."""
    if heading is not None:
        found = _find_section(tree, page, lambda n: n.get("start_index") == page
                              and str(n.get("title") or "").strip() == heading)
        if found:
            return found
    else:
        found = _find_section(tree, page, lambda n: n.get("start_index", page) < page)
        if found:
            return found
    return _find_section(tree, page, lambda n: True) or ("", "")


def _find_section(tree: list[Any], page: int, accept: Any) -> tuple[str, str] | None:
    """Deepest (then latest-starting) accepted node whose range holds `page`."""
    best: tuple[int, int, list[str], str] | None = None

    def visit(nodes: list[Any], path: list[str]) -> None:
        nonlocal best
        for node in nodes:
            if not isinstance(node, dict):
                continue
            start, end = node.get("start_index"), node.get("end_index")
            if not (isinstance(start, int) and isinstance(end, int) and start <= page <= end):
                continue
            here = path + [str(node.get("title") or "")]
            key = (len(here), start)
            if accept(node) and (best is None or key >= best[:2]):
                best = (len(here), start, here, str(node.get("node_id") or ""))
            visit(node.get("nodes") or [], here)

    visit(tree, [])
    if best is None:
        return None
    return " > ".join(best[2]), best[3]


def highlight_pattern(query: str) -> re.Pattern[str] | None:
    """Regex for the query's surface forms: words and numbers (digits may
    carry thousands separators in the page), CJK runs and their bigrams."""
    forms: set[str] = set()
    for m in _TOKEN_RE.finditer(_normalize(query)):
        tok = m.group(0)
        if not tok[0].isascii():
            forms.add(re.escape(tok))
            forms.update(re.escape(tok[i:i + 2]) for i in range(len(tok) - 1))
        elif tok[0].isdigit():
            digits = tok.replace(",", "")
            forms.add(",?".join(re.escape(c) for c in digits))
        elif tok not in STOPWORDS:
            forms.add(rf"\b{re.escape(tok)}" + ("" if tok.endswith("$") else r"\b"))
    if not forms:
        return None
    return re.compile("|".join(sorted(forms, key=len, reverse=True)), re.IGNORECASE)


def _best_window(text: str, pattern: re.Pattern[str] | None, width: int) -> int:
    """Offset of the match that opens the `width`-wide window holding the most
    distinct query terms (0 when nothing matches)."""
    matches = list(pattern.finditer(text)) if pattern else []
    best_pos, best_count = 0, 0
    for m in matches:
        inside = {x.group(0).lower() for x in matches
                  if m.start() - width // 5 <= x.start() and x.end() <= m.start() + width}
        if len(inside) > best_count:
            best_pos, best_count = m.start(), len(inside)
    return best_pos


def snippet(page_markdown: str, pattern: re.Pattern[str] | None,
            width: int = SNIPPET_CHARS) -> str:
    """About `width` characters of the page's plain text around the densest
    cluster of query terms, with matches in ``**bold**``."""
    text = " ".join(unicodedata.normalize("NFKC", plain_text(page_markdown)).split())
    text = re.sub(r"(?:\s*\|\s*){2,}", " | ", text)
    start = max(0, _best_window(text, pattern, width) - width // 5)
    end = min(len(text), start + width)
    piece = text[start:end]
    piece = pattern.sub(lambda m: f"**{m.group(0)}**", piece) if pattern else piece
    return ("…" if start > 0 else "") + piece + ("…" if end < len(text) else "")
