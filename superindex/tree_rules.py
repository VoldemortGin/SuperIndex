"""Chapter trees from a PDF's layout (`superindex.engine.flash`), cleaned by
deterministic rules, for `md_ingest` to index over the extractor's page text.

The structure (titles, start/end pages) comes from flash with its no-LLM
merge pass (``optimize="merge"``); the page text stays the Markdown's. On top
of flash: joined same-page titles are shortened (`rename_union_titles`), fake
headings — figures, KPI cards, single characters — are folded into the node
before them and "(continued)" sections into the section they continue
(`drop_fake_titles`), and long leaves are split into page chunks
(`split_long_leaves`). An over-flat level is regrouped: by heading numbering
(`group_by_numbering`), then, for a still too flat top level, by one LLM call
(`group_top_level`). A tree that looks broken (`bad_tree_reason`) is
refused, and the caller builds from the Markdown headings instead.

Each node also gets ``_anchor``: where its title sits on its first page
(`set_anchors`), so `own_text` can cut a page shared by several sections at
their titles.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

MAX_LEAF_PAGES = 20      # a leaf longer than this is split ...
SPLIT_PAGES = 10         # ... into chunks of this many pages (upstream max_page_num_each_node)
FLAT_MAX_PAGES = 10      # flash's one-node-per-page tree is refused past this many pages
MAX_NODES_PER_PAGE = 3
MAX_FAKE_RATIO = 0.25

_PERIOD = r"(?:[12]H|H[12]|FY|Q[1-4]|[1-4]Q)\s?'?\d{2,4}"
_NUMBER = (r"[+\-−–]?\(?(?:US|HK|S|A|NT|RMB|USD|HKD)?[$¥€£]?\d[\d.,]*\)?"
           r"(?:%|x|bps|pps|ppts|pp|m|mn|bn|b|k|亿|万|百万|千)?(?:元|港元|美元)?")
_FAKE_TOKEN_RE = re.compile(
    rf"^(?:{_NUMBER}|{_PERIOD}|bps|pps|ppts|pp|x|%|US\$|HK\$|RMB|USD|HKD|YoY|CER|HoH|AER"
    r"|[+\-−–/|·•:])$", re.IGNORECASE)
_KPI_RE = re.compile(r"(?<![A-Za-z])(?:YoY|CER|HoH)\s*[:：]?\s*[+\-−–]?\(?\d[\d.,]*\)?\s*%?\s*$",
                     re.IGNORECASE)
_CONTINUED_RE = re.compile(r"\s*[（(]\s*(?:續|续|continued)\s*[)）]\s*$", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
# Heading numbering styles, outermost first: 一、 > 1 / 1. / 1、 > (a) / （一） / (1)
_NUMBERING_RES = (
    re.compile(r"^[一二三四五六七八九十百]+\s*[、．.]"),
    re.compile(r"^\d{1,3}(?:\s*[.、．](?!\d)|\s+)(?!\s*(?:年|月|日|个|%))"),
    re.compile(r"^[（(]\s*(?:[一二三四五六七八九十]+|\d{1,3}|[A-Za-z])\s*[)）]"),
)
GROUP_MIN_TOP = 15        # the LLM regroups a top level of more than max(this, pages / 3) nodes ...
GROUP_DECK_RATIO = 0.8    # ... unless it is a deck: about one top-level node per page
GROUP_PROMPT = (
    "你在为一份长文档整理目录。下面是目录树当前的全部顶层节点（序号、标题、起止页），它们太扁平了。"
    "请把相邻的节点分成若干组，每组对应文档里的一个大章节。\n"
    "要求：每个节点恰好属于一个组；每组成员的序号必须连续；组数至少 2 个且少于节点数；"
    "组标题必须取自某个成员的标题，或文档原文中出现的章节名，不得编造新的标题或事实。\n"
    "只输出一个 JSON 数组，不要解释：\n"
    '[{"title": "组标题", "members": [序号, ...]}, ...]\n\n'
    "顶层节点（序号 | 标题 | 起始页-结束页）：\n{nodes}"
)


def _norm(title: str) -> str:
    return re.sub(r"\s+", "", str(title or "")).casefold()


def _walk(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for node in tree:
        out.append(node)
        out.extend(_walk(node.get("nodes") or []))
    return out


# ───────────────────────────────────────────────────────────── title rules
def is_fake_title(title: str) -> bool:
    """A "heading" that is body text: a single character, only figures
    (numbers, percentages, period codes such as 1H26 / 2Q24 / FY2024,
    currency amounts, units, "YoY CER" column heads), or a KPI card ending in YoY / CER / HoH and a figure."""
    text = " ".join(str(title or "").split())
    if len(_norm(text)) <= 1:
        return True
    if _KPI_RE.search(text):
        return True
    tokens = text.split()
    return all(_FAKE_TOKEN_RE.match(t) for t in tokens)


def fake_ratio(tree: list[dict[str, Any]]) -> float:
    nodes = _walk(tree)
    return sum(is_fake_title(n["title"]) for n in nodes) / len(nodes) if nodes else 0.0


def rename_union_titles(tree: list[dict[str, Any]]) -> None:
    """A node flash merged from same-page siblings is titled with all their
    titles joined by "; " (or, when too long, a page label); rename it to the
    first title and a count: "管理合約 等 3 项" / "Overview + 2 more". The
    first title is kept in ``_search`` for `set_anchors`."""
    for node in _walk(tree):
        items = [str(t) for t in node.get("key_items") or [] if t]
        if len(items) < 2:
            continue
        start, end = node["start_index"], node["end_index"]
        label = f"p.{start}" if start == end else f"p.{start}-{end}"
        if node["title"] not in ("; ".join(items), label):
            continue        # a collapsed parent keeps its own heading
        first = items[0]
        node["_search"] = first
        node["title"] = (f"{first} 等 {len(items)} 项" if _CJK_RE.search("".join(items))
                         else f"{first} + {len(items) - 1} more")


def _base_title(title: str) -> str:
    """A title without its "(continued)" suffix, normalized."""
    return _norm(_CONTINUED_RE.sub("", title))


def _extend_end(node: dict[str, Any], end: int) -> None:
    """Stretch `node` and its last descendants to cover through page `end`."""
    while True:
        node["end_index"] = max(node["end_index"], end)
        if not node.get("nodes"):
            return
        node = node["nodes"][-1]


def _absorb(prev: dict[str, Any], node: dict[str, Any]) -> None:
    """`prev` takes over `node`'s pages and children."""
    _extend_end(prev, node["end_index"])
    if node.get("nodes"):
        prev.setdefault("nodes", []).extend(node["nodes"])


def drop_fake_titles(tree: list[dict[str, Any]]) -> int:
    """Fold fake headings (`is_fake_title`) back into body text: the node goes,
    its pages (and children) join the previous sibling — or stay with the
    parent when it is the first child (its children then take its place). A
    first top-level node hands its pages to the next one. A heading ending in
    "(continued)" / "（續）" / "(续)" whose remaining title matches the previous
    sibling's (itself with or without the suffix) is merged into it; one that
    matches its parent's goes, its children taking its place. Works in place;
    returns the nodes removed."""
    removed = 0

    def clean(nodes: list[dict[str, Any]], parent: dict[str, Any] | None) -> list[dict[str, Any]]:
        nonlocal removed
        out: list[dict[str, Any]] = []
        carry: int | None = None        # start page of a dropped first top-level node
        for node in nodes:
            if node.get("nodes"):
                node["nodes"] = clean(node["nodes"], node)
                if not node["nodes"]:
                    node.pop("nodes")
            if _CONTINUED_RE.search(node["title"]):
                base = _base_title(node["title"])
                if out and base == _base_title(out[-1]["title"]):
                    _absorb(out[-1], node)
                    removed += 1
                    continue
                if parent is not None and base == _base_title(parent["title"]):
                    out.extend(node.get("nodes") or [])
                    removed += 1
                    continue
            if not is_fake_title(node["title"]):
                if carry is not None:
                    node["start_index"] = min(node["start_index"], carry)
                    carry = None
                out.append(node)
                continue
            removed += 1
            if out:
                _absorb(out[-1], node)
            elif node.get("nodes"):
                out.extend(node["nodes"])
            elif parent is None:
                carry = node["start_index"] if carry is None else carry
        if carry is not None and not out:     # nothing left to hand the pages to
            removed -= 1
            out = nodes[:1]
        return out

    tree[:] = clean(tree, None)
    return removed


def split_long_leaves(tree: list[dict[str, Any]], max_pages: int = MAX_LEAF_PAGES,
                      chunk: int = SPLIT_PAGES) -> int:
    """Give a leaf of more than `max_pages` pages one child per `chunk` pages,
    titled "<title> (p.x–y)". Returns the leaves split."""
    split = 0
    for node in _walk(tree):
        start, end = node["start_index"], node["end_index"]
        if node.get("nodes") or end - start + 1 <= max_pages:
            continue
        node["nodes"] = [{"title": f"{node['title']} (p.{p}–{min(p + chunk - 1, end)})",
                          "start_index": p, "end_index": min(p + chunk - 1, end),
                          "_chunk": True}
                         for p in range(start, end + 1, chunk)]
        split += 1
    return split


def numbering_rank(title: str) -> int | None:
    """The heading's numbering style, 0 outermost (`_NUMBERING_RES`); None
    when it is not numbered."""
    text = str(title or "").strip()
    return next((rank for rank, pattern in enumerate(_NUMBERING_RES) if pattern.match(text)), None)


def _numeral(title: str) -> str:
    return re.split(r"[、．.\s]", str(title).strip(), maxsplit=1)[0]


def add_numbered_containers(nodes: list[dict[str, Any]],
                            headings: list[tuple[int, str]]) -> list[str]:
    """Outermost numbered chapters (一、 …) the Markdown has as a heading but
    `nodes` lacks: one is inserted (marked ``_source: "markdown"``) where the
    next node is lower-numbered, so that `group_by_numbering` gathers those
    under it; one that would hold nothing is skipped. `headings` is
    (page, title) in document order. Returns the titles added."""
    have = {_numeral(n["title"]) for n in nodes if numbering_rank(n["title"]) == 0}
    added = []
    for page, title in headings:
        if numbering_rank(title) != 0 or _numeral(title) in have:
            continue
        at = next((i for i, n in enumerate(nodes) if n["start_index"] >= page), len(nodes))
        while at < len(nodes) and numbering_rank(nodes[at]["title"]) == 0 \
                and nodes[at]["start_index"] == page:
            at += 1
        if at == len(nodes) or numbering_rank(nodes[at]["title"]) in (None, 0):
            continue
        nodes.insert(at, {"title": " ".join(title.split()), "start_index": page,
                          "end_index": page, "_source": "markdown"})
        have.add(_numeral(title))
        added.append(title)
    return added


def group_by_numbering(nodes: list[dict[str, Any]]) -> int:
    """In every sibling list numbered in two or more styles, hang each numbered
    node under the nearest node before it numbered in an outer style (一、 >
    1. > (a)), its page range stretched to cover them; an unnumbered node
    follows the node before it, so the document order holds. Works in
    place; returns the nodes moved."""
    moved = 0
    for node in nodes:
        if node.get("nodes"):
            moved += group_by_numbering(node["nodes"])
    ranks = [numbering_rank(n["title"]) for n in nodes]
    if len({r for r in ranks if r is not None}) < 2:
        return moved
    out: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []    # open containers, outermost first
    depth = 0                                       # containers holding the previous node
    for node, rank in zip(nodes, ranks):
        if rank is not None:
            while stack and stack[-1][0] >= rank:
                stack.pop()
            depth = len(stack)
        holders = stack[:depth]
        if holders:
            holders[-1][1].setdefault("nodes", []).append(node)
            for _, holder in holders:
                holder["end_index"] = max(holder["end_index"], node["end_index"])
            moved += 1
        else:
            out.append(node)
        if rank is not None:
            stack.append((rank, node))
    nodes[:] = out
    return moved


def needs_llm_grouping(tree: list[dict[str, Any]], page_count: int) -> bool:
    """A top level of more than max(`GROUP_MIN_TOP`, pages / 3) nodes that is
    not a deck (at least `GROUP_DECK_RATIO` nodes per page)."""
    top = len(tree)
    return top > max(GROUP_MIN_TOP, page_count / 3) and top < GROUP_DECK_RATIO * page_count


def parse_groups(reply: str, tree: list[dict[str, Any]], known_text: str
                 ) -> tuple[list[tuple[str, list[int]]] | None, str | None]:
    """The LLM's grouping of the top level (`GROUP_PROMPT`), checked: every
    node (1-based) in exactly one group, each group's members consecutive,
    groups in order, 2 <= groups < nodes, each title found in `known_text`
    (normalized: the titles and page text). (groups, None) or (None, why)."""
    text = reply or ""
    start, end = text.find("["), text.rfind("]")
    try:
        data = json.loads(text[start:end + 1]) if 0 <= start < end else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, list):
        return None, "no JSON array in the reply"
    groups: list[tuple[str, list[int]]] = []
    for item in data:
        members = item.get("members") if isinstance(item, dict) else None
        title = " ".join(str(item.get("title") or "").split()) if isinstance(item, dict) else ""
        if not title or not isinstance(members, list) or not members \
                or not all(isinstance(m, int) and not isinstance(m, bool) for m in members):
            return None, f"malformed group: {str(item)[:80]}"
        groups.append((title, members))
    flat = [m for _, members in groups for m in members]
    if flat != list(range(1, len(tree) + 1)):
        return None, "members do not cover every node once, consecutively and in order"
    if not 2 <= len(groups) < len(tree):
        return None, f"{len(groups)} groups for {len(tree)} nodes"
    for title, _ in groups:
        if _norm(title) not in known_text:
            return None, f"group title not in the document: {title[:60]}"
    return groups, None


def group_top_level(tree: list[dict[str, Any]], pages: list[str],
                    complete: Callable[[str], str]) -> str:
    """Regroup an over-flat top level (`needs_llm_grouping`) with one LLM call
    (`complete(prompt) -> reply`); a reply that fails `parse_groups`, or a
    failed call, leaves the tree as is. A group of several nodes becomes a
    parent titled as the LLM says — its first member itself when that is a
    leaf of the same title. Returns what happened, for the log."""
    if not needs_llm_grouping(tree, len(pages)):
        return "not needed"
    listing = "\n".join(f"{i} | {' '.join(n['title'].split())[:120]} | "
                         f"{n['start_index']}-{n['end_index']}" for i, n in enumerate(tree, 1))
    try:
        reply = complete(GROUP_PROMPT.replace("{nodes}", listing))
    except Exception as exc:  # noqa: BLE001 - the ungrouped tree is still usable
        return f"rejected: LLM call failed: {type(exc).__name__}: {exc}"[:300]
    known = _norm("\n".join([*(n["title"] for n in _walk(tree)), *pages]))
    groups, why = parse_groups(reply, tree, known)
    if groups is None:
        return f"rejected: {why}"
    out = []
    for title, members in groups:
        nodes = [tree[m - 1] for m in members]
        if len(nodes) == 1:
            out.append(nodes[0])
            continue
        end = max(n["end_index"] for n in nodes)
        first = nodes[0]
        if not first.get("nodes") and _norm(first["title"]) == _norm(title):
            first["end_index"] = max(first["end_index"], end)
            first["nodes"] = nodes[1:]
            out.append(first)
        else:
            out.append({"title": title, "start_index": first["start_index"],
                        "end_index": end, "nodes": nodes, "_group": "llm"})
    tree[:] = out
    return f"llm: {len(groups)} groups"


def bad_tree_reason(tree: list[dict[str, Any]], toc_source: str | None,
                    page_count: int) -> str | None:
    """Why a flash tree should not be used, or None: no structure, one node per
    page on more than `FLAT_MAX_PAGES` pages, more than `MAX_NODES_PER_PAGE`
    nodes per page, or more than `MAX_FAKE_RATIO` fake titles."""
    nodes = _walk(tree)
    if not nodes:
        return f"flash found no structure (toc_source={toc_source})"
    if toc_source == "pages" and page_count > FLAT_MAX_PAGES:
        return f"flash fell back to one node per page ({page_count} pages)"
    if len(nodes) > MAX_NODES_PER_PAGE * max(1, page_count):
        return f"too many nodes: {len(nodes)} for {page_count} pages"
    ratio = fake_ratio(tree)
    if ratio > MAX_FAKE_RATIO:
        return f"too many fake titles: {ratio:.0%}"
    return None


# ───────────────────────────────────────────────────────────── page text
def _title_pattern(title: str) -> re.Pattern[str] | None:
    chars = [c for c in str(title or "") if not c.isspace()][:30]
    if not chars:
        return None
    return re.compile(r"[\s*#_]*".join(re.escape(c) for c in chars), re.IGNORECASE)


def set_anchors(tree: list[dict[str, Any]], pages: list[str]) -> None:
    """Set ``_anchor`` on every node: the offset of its title in the text of
    its first page, searched from the previous node's title on when both
    start on the same page; None when not found. A page chunk (`split_long_leaves`)
    starts where its parent does (first chunk) or at its page top."""
    last_page, last_offset = 0, -1
    parent_anchor: dict[int, int | None] = {}
    for node in _walk(tree):
        page = node["start_index"]
        if node.get("_chunk"):
            anchor = parent_anchor.get(page, 0)
        else:
            text = pages[page - 1] if 1 <= page <= len(pages) else ""
            pattern = _title_pattern(node.get("_search") or node["title"])
            begin = last_offset if page == last_page else 0
            found = pattern.search(text, begin) if pattern else None
            anchor = found.start() if found else None
            if anchor is not None:      # take in heading marks before it: "## ", "**"
                line = text.rfind("\n", 0, anchor) + 1
                if not text[line:anchor].strip("#*_> \t"):
                    anchor = max(line, begin)
        node["_anchor"] = anchor
        if node.get("nodes") and node["nodes"][0].get("_chunk"):
            parent_anchor[page] = anchor
        if anchor is not None:
            last_page, last_offset = page, anchor


def _cut(pages: list[str], start: tuple[int, int], end: tuple[int, int]) -> str:
    (p1, o1), (p2, o2) = start, end
    if p1 == p2:
        return pages[p1 - 1][o1:o2].strip()
    parts = [pages[p1 - 1][o1:], *pages[p1:p2 - 1], pages[p2 - 1][:o2]]
    return "\n\n".join(p for p in parts if p.strip()).strip()


def own_text(node: dict[str, Any], nxt: dict[str, Any] | None, pages: list[str]) -> str:
    """A node's own text over `pages`: from its title (`_anchor`; its page top
    when not found) to the title of the next node in preorder `nxt`, within
    its own page range. When the next node starts on the same page and its
    title was not found, the cut falls at the page end — the whole page."""
    first, last = node["start_index"], node["end_index"]
    start = (first, node.get("_anchor") or 0)
    end = (last, len(pages[last - 1]))
    if nxt is not None:
        page, anchor = nxt["start_index"], nxt.get("_anchor")
        cut = ((page, anchor) if anchor is not None
               else (page, 0) if page > first else (first, len(pages[first - 1])))
        end = min(end, cut)
    if end <= start:
        if node.get("nodes"):
            return ""
        start, end = (first, 0), (first, len(pages[first - 1]))
    return _cut(pages, start, end)


# ───────────────────────────────────────────────────────────── flash
def flash_tree(pdf: Path, pages: list[str], headings: list[tuple[int, str]] | None = None,
               complete: Callable[[str], str] | None = None, log: dict[str, Any] | None = None,
               ) -> tuple[list[dict[str, Any]] | None, str | None]:
    """(tree, None) from flash's layout analysis of `pdf` (no LLM: merge-only
    optimize) with the rules above, page ranges clamped to `pages`; or
    (None, reason) when the tree looks broken (`bad_tree_reason`). Raises
    when flash cannot run. `headings` — the Markdown's (page, title) —
    supply missing outermost numbered chapters (`add_numbered_containers`);
    `complete` (prompt -> reply) allows the LLM regrouping
    (`group_top_level`). `log` gets ``tree_added``, ``tree_group_needed`` and ``tree_group``."""
    from superindex.engine.flash import page_index_flash

    result = page_index_flash(str(pdf), summary=False, optimize="merge")
    total = len(pages)
    tree = _clamp(result.get("structure") or [], total)
    rename_union_titles(tree)
    if reason := bad_tree_reason(tree, result.get("toc_source"), total):
        return None, reason
    drop_fake_titles(tree)
    log = {} if log is None else log
    if added := add_numbered_containers(tree, headings or []):
        log["tree_added"] = added
    group_by_numbering(tree)
    log["tree_group_needed"] = needs_llm_grouping(tree, total)
    if complete is not None and log["tree_group_needed"]:
        log["tree_group"] = group_top_level(tree, pages, complete)
        print(f"    tree regrouping: {log['tree_group']}", flush=True)
    split_long_leaves(tree)
    set_anchors(tree, pages)
    return tree, None


def _clamp(nodes: list[dict[str, Any]], total: int) -> list[dict[str, Any]]:
    """Nodes within pages 1..total (flash may count pages the Markdown lacks)."""
    out = []
    for node in nodes:
        start = max(1, int(node.get("start_index") or 1))
        if start > total:
            continue
        clean = {"title": str(node.get("title") or "").strip() or f"p.{start}",
                 "start_index": start,
                 "end_index": min(total, max(start, int(node.get("end_index") or start)))}
        if node.get("key_items"):
            clean["key_items"] = node["key_items"]
        if kids := _clamp(node.get("nodes") or [], total):
            clean["nodes"] = kids
        out.append(clean)
    return out
