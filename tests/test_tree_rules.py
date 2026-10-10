"""Offline tests for `superindex.tree_rules` and the flash tree path of
`superindex.md_ingest` — no PDF parsing, no LLM.

    pytest tests/test_tree_rules.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import md_ingest, page_images, tree_rules  # noqa: E402
from superindex.md_ingest import (  # noqa: E402
    build_doc_tree,
    index_markdown,
    parse_pages,
)
from superindex.tree_rules import (  # noqa: E402
    add_numbered_containers,
    bad_tree_reason,
    drop_fake_titles,
    group_by_numbering,
    group_top_level,
    is_fake_title,
    needs_llm_grouping,
    own_text,
    rename_union_titles,
    set_anchors,
    split_long_leaves,
)


def node(title: str, start: int, end: int, *kids: dict[str, Any], **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"title": title, "start_index": start, "end_index": end, **extra}
    if kids:
        out["nodes"] = list(kids)
    return out


def outline(tree: list[dict[str, Any]], depth: int = 0) -> list[tuple[int, str, int, int]]:
    out = []
    for n in tree:
        out.append((depth, n["title"], n["start_index"], n["end_index"]))
        out.extend(outline(n.get("nodes") or [], depth + 1))
    return out


# ───────────────────────────────────────────────────────────── rule 2: union titles
def test_rename_union_titles() -> None:
    tree = [node("管理合約; 關連交易; 主要業務", 3, 3, key_items=["管理合約", "關連交易", "主要業務"]),
            node("ASSETS; LIABILITIES", 4, 4, key_items=["ASSETS", "LIABILITIES"]),
            node("p.5-6", 5, 6, key_items=["Long A", "Long B", "Long C"]),
            node("Overview", 7, 9, key_items=["Child 1", "Child 2"]),      # collapsed parent
            node("Single", 10, 10, key_items=["Single"])]
    rename_union_titles(tree)
    assert [n["title"] for n in tree] == ["管理合約 等 3 项", "ASSETS + 1 more",
                                          "Long A + 2 more", "Overview", "Single"]
    assert tree[0]["_search"] == "管理合約"


# ───────────────────────────────────────────────────────────── rule 3: fake titles
@pytest.mark.parametrize("title", ["1H26", "2Q24 FY2024", "US$3.6b", "+16%", "(2.5)%",
                                   "4.9 pps 4.8 pps", "VONB YoY +16%", "Growth CER (2)%",
                                   "x", "一", "YoY CER", "HK$ 1,234m", "2024"])
def test_fake_titles(title: str) -> None:
    assert is_fake_title(title)


@pytest.mark.parametrize("title", ["2024 Annual Report", "Page 5", "Overview", "7 货币资金",
                                   "VONB Up 19% in 2Q", "FY2024 Results", "一、公司简介"])
def test_real_titles(title: str) -> None:
    assert not is_fake_title(title)


def test_fake_folds_into_previous_sibling() -> None:
    tree = [node("Chapter", 1, 10,
                 node("Intro", 1, 3, node("Deep", 2, 3)),
                 node("+16%", 4, 5),
                 node("Next", 6, 10))]
    assert drop_fake_titles(tree) == 1
    assert outline(tree) == [(0, "Chapter", 1, 10), (1, "Intro", 1, 5), (2, "Deep", 2, 5),
                             (1, "Next", 6, 10)]


def test_fake_first_child_and_children_promoted() -> None:
    tree = [node("Part A", 1, 9, node("1H26", 1, 1), node("2Q24", 2, 5, node("Real", 3, 5)),
                 node("Part B", 6, 9))]
    drop_fake_titles(tree)
    assert outline(tree) == [(0, "Part A", 1, 9), (1, "Real", 3, 5), (1, "Part B", 6, 9)]


def test_fake_first_top_level_hands_pages_to_next() -> None:
    tree = [node("x", 1, 2), node("Report", 3, 9)]
    drop_fake_titles(tree)
    assert outline(tree) == [(0, "Report", 1, 9)]
    only = [node("2024", 1, 3)]
    drop_fake_titles(only)
    assert outline(only) == [(0, "2024", 1, 3)]       # nothing to hand the pages to


def test_continued_merges() -> None:
    tree = [node("10. Income tax", 5, 6),
            node("10. Income tax (continued)", 7, 7, node("Deferred", 7, 7)),
            node("10. Income tax (continued)", 8, 9),
            node("39. 交易", 10, 20, node("39. 交易（ 續）", 12, 20, node("(a) 明細", 13, 14))),
            node("其他資產（續）", 21, 22)]
    drop_fake_titles(tree)
    assert outline(tree) == [(0, "10. Income tax", 5, 9), (1, "Deferred", 7, 9),
                             (0, "39. 交易", 10, 20), (1, "(a) 明細", 13, 14),
                             (0, "其他資產（續）", 21, 22)]


# ───────────────────────────────────────────────────────────── rule 4: long leaves
def test_split_long_leaves() -> None:
    tree = [node("Notes", 30, 54), node("Short", 55, 74), node("Parent", 75, 120, node("K", 75, 76))]
    assert split_long_leaves(tree) == 1
    assert [(n["title"], n["start_index"], n["end_index"]) for n in tree[0]["nodes"]] == [
        ("Notes (p.30–39)", 30, 39), ("Notes (p.40–49)", 40, 49), ("Notes (p.50–54)", 50, 54)]
    assert "nodes" not in tree[1] and len(tree[2]["nodes"]) == 1


# ───────────────────────────────────────────────────────────── rule 5: bad trees
def test_bad_tree_reason() -> None:
    good = [node("Alpha", 1, 5), node("Beta", 6, 20)]
    assert bad_tree_reason(good, "detected", 20) is None
    assert "no structure" in bad_tree_reason([], "unreadable", 20)
    pages = [node(f"Page {p}", p, p) for p in range(1, 12)]
    assert "one node per page" in bad_tree_reason(pages, "pages", 11)
    assert bad_tree_reason(pages[:7], "pages", 7) is None
    dense = [node(f"S{i}", 1, 1) for i in range(7)]
    assert "too many nodes" in bad_tree_reason(dense, "detected", 2)
    fake = [node("Alpha", 1, 1), node("+5%", 2, 2), node("Beta", 3, 3)]
    assert "fake titles" in bad_tree_reason(fake, "detected", 3)


# ───────────────────────────────────────────────────────────── page text
def test_own_text_cuts_shared_pages_at_titles() -> None:
    pages = ["intro\n## Alpha\nalpha text\n## Beta\nbeta text", "beta more\n## Gamma\ng", "tail"]
    tree = [node("Alpha", 1, 1), node("Beta", 1, 2), node("Gamma", 2, 3), node("Missing", 3, 3)]
    set_anchors(tree, pages)
    assert [n["_anchor"] for n in tree] == [6, 26, 10, None]    # line starts, heading marks in
    texts = [own_text(n, tree[i + 1] if i + 1 < len(tree) else None, pages)
             for i, n in enumerate(tree)]
    assert texts[0] == "## Alpha\nalpha text"
    assert texts[1] == "## Beta\nbeta text\n\nbeta more"
    assert texts[2] == "## Gamma\ng"      # Missing starts page 3 (title not found): cut there
    assert texts[3] == "tail"


def test_own_text_whole_page_when_title_not_found() -> None:
    pages = ["some page text"]
    tree = [node("Nope", 1, 1), node("Also nope", 1, 1)]
    set_anchors(tree, pages)
    assert own_text(tree[0], tree[1], pages) == "some page text"
    assert own_text(tree[1], None, pages) == "some page text"


# ───────────────────────────────────────────────────────────── R4 A: numbering
def test_group_by_numbering() -> None:
    tree = [node("Preface", 1, 2), node("一、公司简介", 3, 5), node("二、财务", 6, 6),
            node("1 公司基本情况", 7, 8), node("(a) 明细", 8, 9), node("无编号", 9, 9),
            node("2 货币资金", 10, 11), node("三、附件", 12, 15), node("尾声", 16, 16)]
    group_by_numbering(tree)
    assert outline(tree) == [
        (0, "Preface", 1, 2), (0, "一、公司简介", 3, 5), (0, "二、财务", 6, 11),
        (1, "1 公司基本情况", 7, 9), (2, "(a) 明细", 8, 9), (2, "无编号", 9, 9),
        (1, "2 货币资金", 10, 11), (0, "三、附件", 12, 15), (0, "尾声", 16, 16)]


def test_group_by_numbering_needs_two_styles() -> None:
    tree = [node("1. A", 1, 2), node("Plain", 3, 3), node("2. B", 4, 5)]
    assert group_by_numbering(tree) == 0 and len(tree) == 3
    years = [node("一、甲", 1, 2), node("1 乙", 3, 3), node("5 年 3-5 年", 3, 4)]   # "5 年": no number
    assert group_by_numbering(years) == 2
    assert outline(years) == [(0, "一、甲", 1, 4), (1, "1 乙", 3, 3), (1, "5 年 3-5 年", 3, 4)]


def test_add_numbered_containers() -> None:
    tree = [node("九、其他信息", 40, 61), node("1 公司基本情况", 62, 62), node("2 税项", 63, 70)]
    headings = [(3, "一、公司简介"), (4, "二、财务会计信息"), (40, "九、 其他信息"),
                (45, "十、附件：审计报告"), (80, "十一、没有成员")]
    assert add_numbered_containers(tree, headings) == ["十、附件：审计报告"]
    group_by_numbering(tree)
    assert outline(tree) == [(0, "九、其他信息", 40, 61), (0, "十、附件：审计报告", 45, 70),
                             (1, "1 公司基本情况", 62, 62), (1, "2 税项", 63, 70)]
    assert tree[1]["_source"] == "markdown"


# ───────────────────────────────────────────────────────────── R4 B: LLM regrouping
def flat(n: int = 20, pages: int = 45) -> tuple[list[dict[str, Any]], list[str]]:
    step = pages // n
    tree = [node(f"Section {i}", 1 + (i - 1) * step, i * step) for i in range(1, n + 1)]
    texts = [f"page {p}" for p in range(1, pages + 1)]
    texts[0] += "\nPart One\nPart Two"
    return tree, texts


def test_llm_grouping_applied() -> None:
    tree, pages = flat()
    prompts: list[str] = []

    def complete(prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps([{"title": "Part One", "members": list(range(1, 11))},
                           {"title": "Section 11", "members": list(range(11, 20))},
                           {"title": "Section 20", "members": [20]}])
    assert group_top_level(tree, pages, complete) == "llm: 3 groups"
    assert len(prompts) == 1 and "20 | Section 20 | 39-40" in prompts[0]
    assert [(n["title"], n["start_index"], n["end_index"], len(n.get("nodes") or []))
            for n in tree] == [("Part One", 1, 20, 10), ("Section 11", 21, 38, 8),
                               ("Section 20", 39, 40, 0)]
    assert tree[0]["_group"] == "llm" and "_group" not in tree[1]     # first member as parent


@pytest.mark.parametrize("reply", [
    "not json",
    json.dumps([{"title": "Part One", "members": [1, 3]}, {"title": "Part Two", "members": [2]}]),
    json.dumps([{"title": "Part One", "members": list(range(1, 21))}]),            # one group
    json.dumps([{"title": "Made Up", "members": list(range(1, 11))},
                {"title": "Part Two", "members": list(range(11, 21))}]),          # invented title
    json.dumps([{"title": "Part One", "members": list(range(1, 11))},
                {"title": "Part Two", "members": list(range(11, 20))}]),          # misses 20
])
def test_llm_grouping_rejected(reply: str) -> None:
    tree, pages = flat()
    before = outline(tree)
    assert group_top_level(tree, pages, lambda _: reply).startswith("rejected")
    assert outline(tree) == before


def test_llm_grouping_call_failure_keeps_tree() -> None:
    tree, pages = flat()

    def boom(_: str) -> str:
        raise RuntimeError("down")
    assert "LLM call failed" in group_top_level(tree, pages, boom) and len(tree) == 20


def test_llm_grouping_not_for_decks_or_small_tops() -> None:
    deck, pages = flat(n=50, pages=55)
    assert not needs_llm_grouping(deck, 55)
    assert group_top_level(deck, pages, lambda _: pytest.fail("no LLM call")) == "not needed"
    assert not needs_llm_grouping(*flat(n=15, pages=30)[:1], 30)
    assert needs_llm_grouping(flat()[0], 45)


# ───────────────────────────────────────────────────────────── md_ingest wiring
def doc_md(pages: int = 3) -> str:
    body = ["<!-- page: 1 -->\n# Report\n\n**Bold heading**\n\nIntro text.\n\n## 2024\n\nfigures"]
    body += [f"<!-- page: {p} -->\n## Section {p}\n\ntext {p}" for p in range(2, pages + 1)]
    return "\n\n".join(body) + "\n"


FLASH_TREE = [{"title": "Report", "node_id": "0000", "start_index": 1, "end_index": 3,
               "nodes": [{"title": "Section 2", "node_id": "0001", "start_index": 2, "end_index": 2},
                         {"title": "Section 3", "node_id": "0002", "start_index": 3, "end_index": 3}]}]


@pytest.fixture
def fake_flash(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from superindex.engine import flash
    calls: list[str] = []

    def run(pdf: str, summary: bool = True, optimize: Any = None, **_: Any) -> dict[str, Any]:
        assert summary is False and optimize == "merge"
        calls.append(pdf)
        return {"structure": json.loads(json.dumps(FLASH_TREE)), "toc_source": "detected"}
    monkeypatch.setattr(flash, "page_index_flash", run)
    return calls


def test_flash_tree_used(fake_flash: list[str], tmp_path: Path) -> None:
    parsed = parse_pages(doc_md())
    tree, info = build_doc_tree(parsed, "doc", tmp_path / "a.pdf", "flash", 3)
    assert info["tree_builder"] == "flash" and "tree_fallback" not in info
    assert outline(tree) == [(0, "Report", 1, 3), (1, "Section 2", 2, 2), (1, "Section 3", 3, 3)]
    assert [n["node_id"] for n in md_ingest._preorder(tree)] == ["0000", "0001", "0002"]
    texts = md_ingest._own_texts(tree, parsed)
    assert texts[0].startswith("# Report") and "figures" in texts[0]
    assert texts[1] == "## Section 2\n\ntext 2"


@pytest.mark.parametrize("why", ["raise", "bad", "pages"])
def test_fallback_to_markdown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, why: str) -> None:
    def flash_tree(*_: Any, **__: Any) -> tuple[None, str]:
        if why == "raise":
            raise RuntimeError("broken pdf")
        return None, "too many fake titles: 30%"
    monkeypatch.setattr(tree_rules, "flash_tree", flash_tree)
    parsed = parse_pages(doc_md())
    tree, info = build_doc_tree(parsed, "doc", tmp_path / "a.pdf", "flash", 4 if why == "pages" else 3)
    assert info["tree_builder"] == "markdown" and info["tree_fallback"]
    titles = [t for _, t, _, _ in outline(tree)]
    assert "Bold heading" not in titles and "2024" not in titles     # no bold, no fake titles
    assert titles == ["Report", "Section 2", "Section 3"]
    # a Markdown with no # heading may still use bold lines
    plain = parse_pages("<!-- page: 1 -->\n**Bold heading**\n\ntext\n")
    tree, _ = build_doc_tree(plain, "doc", tmp_path / "a.pdf", "flash", 1)
    assert [t for _, t, _, _ in outline(tree)] == ["Bold heading"]


def test_markdown_source_unchanged(tmp_path: Path) -> None:
    parsed = parse_pages(doc_md())
    tree, info = build_doc_tree(parsed, "doc", tmp_path / "a.pdf", "markdown", 3)
    assert info == {"tree_source": "markdown", "tree_builder": "markdown", "tree_group_llm": False}
    assert tree == md_ingest.build_tree(parsed, "doc")
    with pytest.raises(ValueError):
        build_doc_tree(parsed, "doc", None, "other")


def test_switching_tree_source_rebuilds(fake_flash: list[str], tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")
    monkeypatch.setattr(page_images, "pdf_metadata",
                        lambda p: {"pdf_path": str(p), "pdf_pages": 3, "pdf_stamp": "x"})
    md = tmp_path / "doc.md"
    md.write_text(doc_md(), encoding="utf-8")
    store = tmp_path / "store"

    def info() -> dict[str, Any]:
        (meta,) = md_ingest.DocStore(str(store)).list_metas()
        return meta["metadata"]

    first = index_markdown(md, store, pdf=pdf)
    assert not first.skipped and info()["tree_builder"] == "flash" and first.nodes == 3
    assert index_markdown(md, store, pdf=pdf).skipped
    switched = index_markdown(md, store, pdf=pdf, tree_source="markdown")
    assert not switched.skipped and info()["tree_builder"] == "markdown"
    assert index_markdown(md, store, pdf=pdf, tree_source="markdown").skipped
    back = index_markdown(md, store, pdf=pdf)
    assert not back.skipped and info()["tree_source"] == "flash"
    # back to Markdown with no PDF given: the flash tree still goes
    assert not index_markdown(md, store, tree_source="markdown").skipped
    assert len(fake_flash) == 2


def test_old_store_without_pdf_not_rebuilt(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text(doc_md(), encoding="utf-8")
    store = tmp_path / "store"
    index_markdown(md, store, tree_source="markdown")
    meta_path = next((store / "docs").glob("*/doc.json"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for key in ("tree_source", "tree_builder", "tree_group_llm"):
        meta["metadata"].pop(key, None)
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert index_markdown(md, store).skipped                 # flash asked, but no PDF to use


def test_needs_new_tree() -> None:
    need = md_ingest._needs_new_tree
    pdf = Path("a.pdf")
    assert need({}, "flash", pdf, False)                                   # old store, PDF now given
    assert not need({}, "flash", None, False)                              # nothing to build flash from
    assert need({"tree_source": "flash", "tree_builder": "flash"}, "markdown", None, False)
    assert not need({"tree_source": "flash", "tree_builder": "markdown"}, "markdown", None, False)
    grouped = {"tree_source": "flash", "tree_builder": "flash", "tree_group_needed": True,
               "tree_group_llm": False}
    assert need(grouped, "flash", pdf, True)                               # regrouping now allowed
    assert not need({**grouped, "tree_group_needed": False}, "flash", pdf, True)


def test_no_pdf_keeps_old_path(tmp_path: Path) -> None:
    parsed = parse_pages(doc_md())
    tree, info = build_doc_tree(parsed, "doc", None, "flash")
    assert tree == md_ingest.build_tree(parsed, "doc")                    # bold lines still headings
    assert info["tree_builder"] == "markdown" and "tree_fallback" not in info
    md = tmp_path / "doc.md"
    md.write_text(doc_md(), encoding="utf-8")
    res = index_markdown(md, tmp_path / "store")
    assert res.warnings == [] and res.nodes == len(md_ingest._preorder(tree))
