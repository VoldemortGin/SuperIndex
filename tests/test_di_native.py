"""Offline tests for Azure DI's native Markdown in `superindex.md_ingest`:
``<!-- PageBreak -->`` pages, DI comments, HTML tables across pages, figures.

    pytest tests/test_di_native.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from extractors.azure_di import PAGE_MARKER, AzureDocIntelligence  # noqa: E402
from superindex.md_ingest import build_tree, index_markdown, parse_pages  # noqa: E402

SAMPLE = ROOT / "samples" / "di_native_excerpt.md"
TABLE_TAG = re.compile(r"<(/?)(table|thead|tbody|tr|th|td)\b[^>]*>", re.IGNORECASE)


def walk(tree: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for node in tree:
        out[node["title"]] = node
        out.update(walk(node.get("nodes") or []))
    return out


def balanced(page: str) -> bool:
    """Every table tag on the page closes in order."""
    stack: list[str] = []
    for m in TABLE_TAG.finditer(page):
        if not m.group(1):
            stack.append(m.group(2).lower())
        elif not stack or stack.pop() != m.group(2).lower():
            return False
    return not stack


def sample() -> str:
    return SAMPLE.read_text(encoding="utf-8")


# ───────────────────────────────────────────────────────────── pages
def test_pagebreak_pages_are_physical_and_labels_are_kept() -> None:
    parsed = parse_pages(sample())
    assert parsed.page_mode == "pagebreak" and parsed.has_markers
    assert len(parsed.pages) == 4
    assert parsed.pages[0].startswith("# 2023 年度报告")
    assert parsed.pages[1].startswith("## 主席报告")
    assert "Net profit attributable" in parsed.pages[3]
    # PageNumber is only a label: page 1 is printed "i", page_index stays 1
    assert parsed.page_labels == {1: "i", 2: "2", 3: "3", 4: "4"}
    assert parsed.page_meta[0].headers == ["友邦保险控股有限公司 2023 年报"]
    assert parsed.page_meta[3].headers == ["Financial Review"]
    assert all(m.footers == ["AIA Group Limited"] for m in parsed.page_meta)


def test_di_comments_never_reach_text() -> None:
    parsed = parse_pages(sample())
    for text in [*parsed.pages, *parsed.lines]:
        assert "<!--" not in text and "PageHeader" not in text
        assert "AIA Group Limited\"" not in text
    # a header comment's value is not body text either
    assert "友邦保险控股有限公司 2023 年报" not in "\n".join(parsed.pages)


def test_other_comments_dropped_and_not_headings() -> None:
    md = ("<!-- PageBreak -->\n# Real\n\n<!-- # not a heading -->\ntext <!-- note --> more\n"
          "<!--\n## multi-line comment\n-->\nend\n")
    parsed = parse_pages(md)
    assert parsed.page_mode == "pagebreak"
    assert parsed.pages[0] == ""                   # nothing before the break
    assert "note" not in parsed.pages[1]
    assert re.search(r"text\s+more", parsed.pages[1])
    titles = list(walk(build_tree(parsed, "doc")))
    assert "Real" in titles
    assert not any("not a heading" in t or "multi-line" in t for t in titles)


def test_page_marker_wins_over_pagebreak() -> None:
    """Our extractor keeps DI's PageBreak comments and adds its own markers;
    the explicit markers set the pages, the PageBreaks are just dropped."""
    content = ("# Title\n\nPage one.\n<!-- PageNumber=\"1\" -->\n<!-- PageBreak -->\n"
               "## Part B\n\nPage two.\n<!-- PageNumber=\"2\" -->\n")
    split = content.index("## Part B")
    result = {"content": content, "pages": [
        {"pageNumber": 5, "spans": [{"offset": 0, "length": split}]},
        {"pageNumber": 6, "spans": [{"offset": split, "length": len(content) - split}]},
    ]}
    parsed = parse_pages(AzureDocIntelligence.to_markdown(result))
    assert parsed.page_mode == "marker"
    assert len(parsed.pages) == 6                  # numbering follows the markers
    assert parsed.pages[4] == "# Title\n\nPage one."
    assert parsed.pages[5] == "## Part B\n\nPage two."
    assert parsed.page_labels == {5: "1", 6: "2"}


def test_no_marks_keeps_pseudo_pages_and_drops_comments() -> None:
    parsed = parse_pages("<!-- PageHeader=\"H\" -->\n# A\n\ntext\n")
    assert parsed.page_mode == "pseudo" and not parsed.has_markers
    assert parsed.pages == ["# A\n\ntext"]


# ───────────────────────────────────────────────────────────── tables
def test_split_table_gets_header_repeated() -> None:
    """Page 2 ends with a closed table, page 3 opens with its header-less
    continuation: the <thead> is repeated there."""
    page3 = parse_pages(sample()).pages[2]
    assert page3.startswith("<table>\n<thead>\n<tr><th>项目</th><th>2023</th>")
    assert page3.index("<thead>") < page3.index("内含价值权益")
    assert page3.count("<thead>") == 1


def test_unclosed_table_is_closed_and_reopened_with_header() -> None:
    parsed = parse_pages(sample())
    page3, page4 = parsed.pages[2], parsed.pages[3]
    assert page3.rstrip().endswith("<tr><td>OPAT</td><td>6,610</td><td>6,168</td></tr>\n</table>")
    assert page4.startswith("<table>\n<tr><th>Metric</th><th>2023</th><th>2022</th></tr>")
    assert page4.index("Metric") < page4.index("EV Equity")
    assert all(balanced(p) for p in parsed.pages)


def test_table_cut_mid_row_across_three_pages() -> None:
    md = ("<table>\n<thead><tr><th>A</th><th>B</th></tr></thead>\n<tbody>\n"
          "<tr><td>1</td><td>2</td></tr>\n<tr><td>3</td>\n<!-- PageBreak -->\n"
          "<td>4</td></tr>\n<tr><td>5</td><td>6</td></tr>\n<!-- PageBreak -->\n"
          "<tr><td>7</td><td>8</td></tr>\n</tbody>\n</table>\n\nAfter.\n")
    pages = parse_pages(md).pages
    assert len(pages) == 3
    assert all(balanced(p) for p in pages)
    assert pages[1].startswith("<table>\n<thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr>")
    assert pages[2].startswith("<table>\n<thead><tr><th>A</th>")
    assert pages[2].rstrip().endswith("After.")


def test_unrelated_table_on_next_page_is_left_alone() -> None:
    md = ("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>\n"
          "<!-- PageBreak -->\n"
          "<table><tr><td>x</td><td>y</td><td>z</td></tr></table>\n"       # 3 columns
          "<!-- PageBreak -->\n"
          "<table><tr><th>C</th><th>D</th></tr><tr><td>3</td><td>4</td></tr></table>\n")
    pages = parse_pages(md).pages
    assert "<th>A</th>" not in pages[1]            # column count differs
    assert pages[2].count("<th>") == 2             # has its own header


def test_pipe_table_header_logic_still_works() -> None:
    md = ("| K | V |\n|---|---|\n| a | 1 |\n<!-- PageBreak -->\n| b | 2 |\n")
    assert parse_pages(md).pages[1].split("\n")[:3] == ["| K | V |", "|---|---|", "| b | 2 |"]


# ───────────────────────────────────────────────────────────── tree
def test_tree_on_di_output() -> None:
    parsed = parse_pages(sample())
    nodes = walk(build_tree(parsed, "report"))
    ranges = {t: (n["start_index"], n["end_index"]) for t, n in nodes.items()}
    assert ranges == {
        "2023 年度报告": (1, 4),
        "主席报告": (2, 3),              # its table continues onto page 3
        "Financial Review": (3, 4),       # its table is cut onto page 4
        "Financial Statements": (4, 4),
        "Consolidated Income Statement": (4, 4),
    }
    # "# 4,034" inside <figure> is chart text, not a chapter; figcaption stays
    assert "4,034" not in ranges
    assert "<figcaption>图 1：新业务价值（百万美元）</figcaption>" in parsed.pages[0]


def test_table_cell_lines_are_not_headings() -> None:
    md = ("# Real\n\n<table>\n<tr><td>\n# cell text\n</td></tr>\n</table>\n\n"
          "<figure>\n**Bold chart label**\n</figure>\n\n## Next\n\nx\n")
    titles = list(walk(build_tree(parse_pages(md), "doc")))
    assert titles == ["Real", "Next"]


def test_unclosed_figure_does_not_hide_later_headings() -> None:
    md = "# A\n\n<figure>\nchart\n\n## B\n\ntext\n"
    assert list(walk(build_tree(parse_pages(md), "doc"))) == ["A", "B"]


# ───────────────────────────────────────────────────────────── store
def test_index_di_markdown_records_labels_and_mode(tmp_path: Path) -> None:
    md = tmp_path / "AIA_AR2023.md"
    md.write_text(sample(), encoding="utf-8")
    res = index_markdown(md, tmp_path / "store")
    assert (res.pages, res.nodes, res.has_markers) == (4, 5, True)
    doc_dir = tmp_path / "store" / "docs" / res.doc_id
    meta = json.loads((doc_dir / "doc.json").read_text(encoding="utf-8"))
    assert meta["metadata"]["page_mode"] == "pagebreak"
    assert meta["metadata"]["page_labels"] == {"1": "i", "2": "2", "3": "3", "4": "4"}
    pages = json.loads((doc_dir / "pages.json").read_text(encoding="utf-8"))
    assert [p["page_index"] for p in pages] == [1, 2, 3, 4]
    assert (doc_dir / "bm25.json").is_file()


def test_marker_line_output_unchanged_for_extractor_format() -> None:
    md = f"\n{PAGE_MARKER.format(n=1)}\n# A\n\none\n\n{PAGE_MARKER.format(n=2)}\ntwo\n"
    parsed = parse_pages(md)
    assert parsed.page_mode == "marker"
    assert parsed.pages == ["# A\n\none", "two"]
