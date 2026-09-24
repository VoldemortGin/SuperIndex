"""Offline tests for `superindex.md_ingest` — no network, no LLM.

    pytest tests/test_md_ingest.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import md_ingest  # noqa: E402
from superindex.extractors.azure_di import (  # noqa: E402
    PAGE_MARKER,
    AzureDocIntelligence,
)
from superindex.md_ingest import build_tree, index_markdown, parse_pages  # noqa: E402


def mark(n: int) -> str:
    """A page marker exactly as the Azure DI extractor writes it."""
    return f"\n{PAGE_MARKER.format(n=n)}\n"


# A report whose chapters and one table run across page breaks.
REPORT = (
    mark(1)
    + "# Annual Report 2021\n\nCover text.\n\n"
    + "## Chairman's Statement\n\nA year of growth.\n"
    + mark(2)
    + "The statement continues on page two.\n\n"
    + "### Dividend\n\nFinal dividend 108.00 HK cents.\n\n"
    + "| Component | 2021 | 2020 |\n|---|---|---|\n| Interim | 38.00 | 35.00 |\n"
    + mark(3)
    + "| Final | 108.00 | 100.00 |\n| Total | 146.00 | 135.00 |\n\n"
    + "## Financial Statements\n\nNet profit 7,427.\n"
    + mark(4)
    + "### Notes\n\nNote 1 text.\n"
)


def walk(tree: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for node in tree:
        out[node["title"]] = node
        out.update(walk(node.get("nodes") or []))
    return out


# ───────────────────────────────────────────────────────────── pages
def test_pages_follow_markers() -> None:
    parsed = parse_pages(REPORT)
    assert parsed.has_markers
    assert len(parsed.pages) == 4
    assert all("<!--" not in p for p in parsed.pages)
    assert all("<!--" not in line for line in parsed.lines)
    assert parsed.pages[0].startswith("# Annual Report 2021")
    assert parsed.pages[1].startswith("The statement continues")
    assert "Net profit 7,427." in parsed.pages[2]
    assert parsed.pages[3].startswith("### Notes")


def test_table_split_by_page_break_repeats_header() -> None:
    parsed = parse_pages(REPORT)
    page3 = parsed.pages[2].split("\n")
    assert page3[:3] == ["| Component | 2021 | 2020 |", "|---|---|---|",
                         "| Final | 108.00 | 100.00 |"]
    # page 2 keeps its own part of the table, once
    assert parsed.pages[1].count("| Component |") == 1
    assert parsed.pages[1].rstrip().endswith("| Interim | 38.00 | 35.00 |")


def test_page_gaps_keep_pdf_numbering() -> None:
    md = "Intro before any marker.\n" + mark(2) + "Page two.\n" + mark(4) + "Page four.\n"
    parsed = parse_pages(md)
    assert len(parsed.pages) == 4
    assert parsed.pages[0] == "" and parsed.pages[2] == ""
    # text ahead of the first marker belongs to the first marked page
    assert parsed.pages[1] == "Intro before any marker.\n\nPage two."
    assert parsed.pages[3] == "Page four."


def test_marker_sharing_a_line_splits_it() -> None:
    parsed = parse_pages("alpha <!-- page: 1 --> beta <!--page:2--> gamma")
    assert parsed.pages == ["alpha\nbeta", "gamma"]


def test_azure_to_markdown_round_trip() -> None:
    content = "# Title\n\nPage one body.\n## Part B\n\nPage two body.\n"
    split = content.index("## Part B")
    result = {"content": content, "pages": [
        {"pageNumber": 1, "spans": [{"offset": 0, "length": split}]},
        {"pageNumber": 2, "spans": [{"offset": split, "length": len(content) - split}]},
    ]}
    parsed = parse_pages(AzureDocIntelligence.to_markdown(result))
    assert parsed.pages == ["# Title\n\nPage one body.", "## Part B\n\nPage two body."]
    tree = build_tree(parsed, "doc")
    nodes = walk(tree)
    assert (nodes["Title"]["start_index"], nodes["Title"]["end_index"]) == (1, 2)
    assert (nodes["Part B"]["start_index"], nodes["Part B"]["end_index"]) == (2, 2)


def test_no_markers_short_document_is_one_page() -> None:
    parsed = parse_pages("# A\n\ntext\n\n## B\n\nmore\n")
    assert not parsed.has_markers
    assert parsed.pages == ["# A\n\ntext\n\n## B\n\nmore"]


def test_no_markers_long_document_pseudo_pages() -> None:
    table = "| k | v |\n|---|---|\n" + "".join(f"| row{i} | {i} |\n" for i in range(40))
    md = "# Big\n\n" + ("word " * 60 + "\n\n") * 6 + table + "\n## Tail\n\nend\n"
    parsed = parse_pages(md, page_chars=500)
    assert len(parsed.pages) > 1
    # the whole table stays on one page: soft breaks never fall inside it
    holder = [p for p in parsed.pages if "| row0 |" in p]
    assert len(holder) == 1 and "| row39 |" in holder[0]
    assert "".join(parsed.pages).replace("\n", "") == md.replace("\n", "")


# ───────────────────────────────────────────────────────────── tree
def test_tree_page_ranges_cover_subtrees() -> None:
    parsed = parse_pages(REPORT)
    tree = build_tree(parsed, "report")
    nodes = walk(tree)
    ranges = {t: (n["start_index"], n["end_index"]) for t, n in nodes.items()}
    assert ranges == {
        "Annual Report 2021": (1, 4),
        "Chairman's Statement": (1, 3),   # its Dividend table ends on page 3
        "Dividend": (2, 3),
        "Financial Statements": (3, 4),
        "Notes": (4, 4),
    }
    assert [n["node_id"] for n in md_ingest._preorder(tree)] == \
        ["0000", "0001", "0002", "0003", "0004"]


def test_preface_and_headingless_documents() -> None:
    parsed = parse_pages(mark(1) + "Loose intro.\n" + mark(2) + "# First\n\nBody.\n")
    tree = build_tree(parsed, "doc")
    assert [(n["title"], n["start_index"], n["end_index"]) for n in tree] == \
        [("Preface", 1, 1), ("First", 2, 2)]

    parsed = parse_pages(mark(1) + "one\n" + mark(2) + "two\n" + mark(3) + "three\n")
    tree = build_tree(parsed, "plain")
    assert tree[0]["title"] == "plain"
    assert (tree[0]["start_index"], tree[0]["end_index"]) == (1, 3)
    assert [c["title"] for c in tree[0]["nodes"]] == ["Page 1", "Page 2", "Page 3"]


# ───────────────────────────────────────────────────────────── store + client
@pytest.fixture()
def report_md(tmp_path: Path) -> Path:
    path = tmp_path / "AIA_Report_2021.md"
    path.write_text(REPORT, encoding="utf-8")
    return path


def _client(store: Path) -> Any:
    from superindex.engine import SuperIndexClient
    return SuperIndexClient(chat_model="openai/offline-test", storage_path=str(store))


def test_store_is_loadable_by_pageindex_client(tmp_path: Path, report_md: Path) -> None:
    store = tmp_path / "store"
    res = index_markdown(report_md, store)
    assert (res.pages, res.nodes, res.has_markers, res.skipped) == (4, 5, True, False)

    pages = json.loads((store / "docs" / res.doc_id / "pages.json").read_text(encoding="utf-8"))
    assert [p["page_index"] for p in pages] == [1, 2, 3, 4]

    client = _client(store)
    docs = client.list_documents()["documents"]
    assert [(d["id"], d["name"], d["pageNum"], d["status"]) for d in docs] == \
        [(res.doc_id, "AIA_Report_2021.md", 4, "completed")]
    # the text-bearing tree the engine rebuilds from pages.json
    tree = client.get_tree(res.doc_id)["result"]
    assert "Final dividend 108.00" in tree[0]["nodes"][0]["nodes"][0]["text"]

    from superindex.engine.agent_tools import call_tool
    out, err = call_tool(client, "get_document_structure", {"doc_name": "AIA_Report_2021.md"})
    assert not err
    structure = json.loads(out)["structure"]
    dividend = walk(structure)["Dividend"]
    assert (dividend["start_index"], dividend["end_index"]) == (2, 3)

    out, err = call_tool(client, "get_page_content",
                         {"doc_name": "AIA_Report_2021.md", "pages": "3"})
    assert not err
    text = json.dumps(json.loads(out), ensure_ascii=False)
    assert "| Final | 108.00 | 100.00 |" in text and "| Component |" in text
    assert "Final dividend 108.00" not in text   # that is page 2


def test_reindex_skips_unchanged_and_replaces_changed(tmp_path: Path, report_md: Path) -> None:
    store = tmp_path / "store"
    first = index_markdown(report_md, store)
    again = index_markdown(report_md, store)
    assert again.skipped and again.doc_id == first.doc_id

    report_md.write_text(REPORT + mark(5) + "# Appendix\n\nExtra.\n", encoding="utf-8")
    changed = index_markdown(report_md, store)
    assert not changed.skipped and changed.doc_id != first.doc_id
    docs = _client(store).list_documents()["documents"]
    assert [(d["id"], d["pageNum"]) for d in docs] == [(changed.doc_id, 5)]


def test_summaries_use_pageindex_summarizer(tmp_path: Path, report_md: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from superindex.engine import utils

    prompts: list[str] = []

    async def fake_acompletion(model: str, prompt: str) -> str:
        prompts.append(prompt)
        return json.dumps({"points": [], "summary": f"summary #{len(prompts)}"})

    monkeypatch.setattr(utils, "llm_acompletion", fake_acompletion)
    monkeypatch.setattr(utils, "llm_completion", lambda model, prompt, **_: "A 2021 report.")
    monkeypatch.setattr(utils, "count_tokens", lambda text, model=None: len(text.split()) * 100)

    store = tmp_path / "store"
    res = index_markdown(report_md, store, summary_model="ollama_chat/test")
    tree = json.loads((store / "docs" / res.doc_id / "tree.json").read_text(encoding="utf-8"))
    nodes = walk(tree)
    assert all(n.get("summary", "").startswith("summary #") for n in nodes.values())
    meta = json.loads((store / "docs" / res.doc_id / "doc.json").read_text(encoding="utf-8"))
    assert meta["description"] == "A 2021 report."
    assert meta["metadata"]["summary"] is True
    # a leaf is summarized from its own section only, not the whole page
    leaf_prompt = next(p for p in prompts if "Note 1 text." in p)
    assert "Net profit" not in leaf_prompt

    # a summarized copy also satisfies a later no-summary run
    again = index_markdown(report_md, store)
    assert again.skipped


def test_md_flow_imports_no_pdf_stack() -> None:
    code = ("import sys, superindex.cli, superindex.md_ingest;"
            "bad = [m for m in ('PyPDF2', 'pypdfium2', 'superindex.engine.flash', 'litellm')"
            " if m in sys.modules]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True,
                         text=True, check=True)
    assert out.stdout.strip() == "[]"
