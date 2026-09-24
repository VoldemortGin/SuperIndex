"""Offline tests for `superindex.bm25`, the `search` CLI and the agent's
`search_pages` tool — no network, no LLM.

    pytest tests/test_bm25.py
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

from superindex import agent_search, bm25  # noqa: E402
from superindex.md_ingest import index_markdown  # noqa: E402

DI_SAMPLE = ROOT / "samples" / "di_native_excerpt.md"
PLAIN_SAMPLE = ROOT / "samples" / "aia_ar2021_excerpt.md"


# ───────────────────────────────────────────────────────────── tokenizer
def test_tokenize_mixed_text() -> None:
    tokens = bm25.tokenize("The HK$1,234.5 dividend grew 12.5% in FY2023 <td>末期股息</td>")
    assert "hk$" in tokens and "1234.5" in tokens and "12.5" in tokens
    assert "fy2023" in tokens and "dividend" in tokens
    assert "the" not in tokens and "in" not in tokens          # stopwords
    assert {"末", "期", "股", "息", "末期", "期股", "股息"} <= set(tokens)
    assert "td" not in tokens                                    # HTML tags stripped


def test_tokenize_normalizes_full_width() -> None:
    assert bm25.tokenize("ＶＯＮＢ　２０２３") == ["vonb", "2023"]


def test_plain_text_flattens_tables() -> None:
    text = bm25.plain_text("<table><tr><th>A</th><th>B</th></tr><tr><td>1&amp;2</td></tr></table>")
    assert " ".join(text.split()) == "A | B | 1&2 |"


# ───────────────────────────────────────────────────────────── store fixtures
@pytest.fixture()
def store(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "AIA_AR2023_DI.md").write_text(DI_SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    (src / "AIA_AR2021.md").write_text(PLAIN_SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "store"
    for md in sorted(src.iterdir()):
        index_markdown(md, out)
    return out


def ids_by_name(store: Path) -> dict[str, str]:
    from pageindex.local_store import DocStore
    return {m["name"]: m["id"] for m in DocStore(str(store)).list_metas()}


# ───────────────────────────────────────────────────────────── search
def test_chinese_query_ranks_the_right_page(store: Path) -> None:
    hits = bm25.search(store, "末期股息", top_k=3).hits
    assert (hits[0].doc_name, hits[0].page) == ("AIA_AR2023_DI.md", 2)
    assert hits[0].section == "2023 年度报告 > 主席报告"
    assert "**末期股息**" in hits[0].snippet
    assert hits[0].page_label == "2"


def test_english_query_and_section_path(store: Path) -> None:
    hit = bm25.search(store, "net profit attributable to shareholders", top_k=1).hits[0]
    assert (hit.doc_name, hit.page) == ("AIA_AR2023_DI.md", 4)
    assert hit.section == "Financial Statements > Consolidated Income Statement"
    assert hit.node_id == "0004"


def test_section_follows_the_match_on_a_shared_page(store: Path) -> None:
    # the plain report is one pseudo-page holding every section
    top = "AIA Group Limited — 2021 Annual Report (excerpt)"
    for query, section in (("final dividend", "Chairman's Statement > Dividend"),
                           ("LCSM cover ratio", "Chairman's Statement > Capital Management"),
                           ("net profit 7,427",
                            "Financial Statements > Consolidated Income Statement")):
        hit = bm25.search(store, query, doc_ids=[ids_by_name(store)["AIA_AR2021.md"]],
                          top_k=1).hits[0]
        assert hit.section == f"{top} > {section}", query
    # a match above the page's first heading belongs to the section carried over
    hit = bm25.search(store, "EV Equity 68,865", top_k=1).hits[0]
    assert (hit.page, hit.section) == (4, "2023 年度报告 > Financial Review")


def test_financial_numbers(store: Path) -> None:
    # "US$3,764" and "3764" both reach the page that prints "US$3,764 million"
    for query in ("US$3,764", "3764 million"):
        hit = bm25.search(store, query, top_k=1).hits[0]
        assert (hit.doc_name, hit.page) == ("AIA_AR2023_DI.md", 4)
        assert "**US$3,764**" in hit.snippet or "**3,764**" in hit.snippet
    # a figure printed on two pages: the page that also names the metric wins
    hits = bm25.search(store, "OPAT 6,610", top_k=3).hits
    assert (hits[0].doc_name, hits[0].page) == ("AIA_AR2023_DI.md", 3)
    # a table value in the plain Markdown report
    hit = bm25.search(store, "final dividend 108.00", top_k=1).hits[0]
    assert hit.doc_name == "AIA_AR2021.md"


def test_continuation_page_is_found_by_repeated_header(store: Path) -> None:
    # page 4 holds the second half of the "Metric" table, header repeated
    pages = {h.page for h in bm25.search(store, "EV Equity", top_k=5).hits
             if h.doc_name == "AIA_AR2023_DI.md"}
    assert 4 in pages


def test_whitelist_limits_documents(store: Path) -> None:
    ids = ids_by_name(store)
    hits = bm25.search(store, "dividend", doc_ids=[ids["AIA_AR2021.md"]], top_k=10).hits
    assert hits and {h.doc_name for h in hits} == {"AIA_AR2021.md"}
    assert bm25.search(store, "dividend", doc_ids=[], top_k=10).hits == []
    both = {h.doc_name for h in bm25.search(store, "dividend", top_k=10).hits}
    assert both == {"AIA_AR2021.md", "AIA_AR2023_DI.md"}


def test_no_match_and_empty_query(store: Path) -> None:
    assert bm25.search(store, "zzzqqq", top_k=5).hits == []
    assert bm25.search(store, "the of", top_k=5).hits == []


def test_snippet_is_short(store: Path) -> None:
    for hit in bm25.search(store, "dividend", top_k=5).hits:
        plain = hit.snippet.replace("**", "").strip("…")
        assert len(plain) <= bm25.SNIPPET_CHARS


# ───────────────────────────────────────────────────────────── persistence
def test_reindex_replaces_index(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("# A\n\nalpha beta\n", encoding="utf-8")
    store = tmp_path / "store"
    first = index_markdown(md, store)
    assert (store / "docs" / first.doc_id / "bm25.json").is_file()
    assert bm25.search(store, "alpha").hits

    md.write_text("# A\n\ngamma delta\n", encoding="utf-8")
    second = index_markdown(md, store)
    assert second.doc_id != first.doc_id
    assert not (store / "docs" / first.doc_id).exists()       # old copy and its index gone
    assert bm25.search(store, "alpha").hits == []
    assert [h.doc_id for h in bm25.search(store, "gamma").hits] == [second.doc_id]


def test_old_store_is_indexed_lazily(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("# A\n\nalpha beta\n", encoding="utf-8")
    store = tmp_path / "store"
    res = index_markdown(md, store)
    index_file = store / "docs" / res.doc_id / "bm25.json"

    index_file.unlink()                                       # a store from before bm25
    result = bm25.search(store, "alpha")
    assert result.built == ["doc.md"] and result.hits
    assert index_file.is_file()
    assert bm25.search(store, "alpha").built == []

    index_file.unlink()                                       # the skip path backfills too
    assert index_markdown(md, store).skipped
    assert index_file.is_file()

    index_file.write_text(json.dumps({"version": 0}), encoding="utf-8")   # stale format
    assert bm25.search(store, "alpha").built == ["doc.md"]


# ───────────────────────────────────────────────────────────── CLI
def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "superindex", *args], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8")


def test_search_cli(store: Path) -> None:
    out = run_cli("search", "末期股息", "--store", str(store), "--top-k", "2")
    assert out.returncode == 0, out.stderr
    assert "AIA_AR2023_DI.md  p.2 (printed 2)" in out.stdout
    assert "section: 2023 年度报告 > 主席报告" in out.stdout

    out = run_cli("search", "dividend", "--store", str(store), "--doc", "AIA_AR2021", "--json")
    hits = json.loads(out.stdout)
    assert hits and {h["doc_name"] for h in hits} == {"AIA_AR2021.md"}

    out = run_cli("search", "dividend", "--store", str(store), "--doc", "nope")
    assert out.returncode == 2 and "no indexed document matches" in out.stderr


# ───────────────────────────────────────────────────────────── agent tool
@pytest.fixture()
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    from pageindex import agent_tools
    monkeypatch.setattr(agent_tools, "_tool_specs", agent_tools._tool_specs)
    agent_search.install()
    agent_search.install()                                    # idempotent


def _client(store: Path) -> Any:
    from pageindex import PageIndexClient
    return PageIndexClient(chat_model="openai/offline-test", storage_path=str(store))


def _call(specs: list[Any], arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    invoke = next(s[3] for s in specs if s[0] == "search_pages")
    blocks, is_error = invoke(arguments)
    return json.loads(blocks[0]["text"]), is_error


def test_tool_is_registered_and_scoped(store: Path, installed: None) -> None:
    from pageindex import agent_tools

    client = _client(store)
    names = [s[0] for s in agent_tools._tool_specs(client)]
    assert names.count("search_pages") == 1 and "get_page_content" in names

    payload, err = _call(agent_tools._tool_specs(client), {"query": "2022 2023", "top_k": "3"})
    assert not err and payload["success"] and len(payload["results"]) == 3
    assert {"doc_name", "page", "section", "snippet", "score"} <= set(payload["results"][0])

    ids = ids_by_name(store)
    scoped = agent_tools._tool_specs(client, False, ids["AIA_AR2021.md"])
    payload, err = _call(scoped, {"query": "dividend", "top_k": 10})
    assert {r["doc_name"] for r in payload["results"]} == {"AIA_AR2021.md"}
    # a document outside the chat's scope cannot be named
    payload, err = _call(scoped, {"query": "dividend", "doc_name": "AIA_AR2023_DI.md"})
    assert err and payload["errorCode"] == "NOT_FOUND"

    payload, err = _call(agent_tools._tool_specs(client),
                         {"query": "末期股息", "doc_name": "AIA_AR2023_DI.md"})
    assert not err and payload["results"][0]["page"] == 2
    payload, err = _call(agent_tools._tool_specs(client), {"query": " "})
    assert err and payload["errorCode"] == "INVALID_INPUT"


def test_openai_agent_gets_the_tool(store: Path, installed: None) -> None:
    from pageindex.local_chat import _openai_agent

    agent = _openai_agent(_client(store), "chat", "openai/offline-test", "x", None, None,
                          doc_ids=None)
    assert "search_pages" in [t.name for t in agent.tools]


def test_make_client_installs_tool_and_guidance(store: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from pageindex import agent_tools

    from superindex.cli import make_client
    from superindex.runtime import LLMSettings

    monkeypatch.setattr(agent_tools, "_tool_specs", agent_tools._tool_specs)
    settings = LLMSettings(None, "openai/offline-test", None, None, None)
    client = make_client(settings, store, instructions="Answer in Chinese.")
    assert "search_pages" in [s[0] for s in agent_tools._tool_specs(client)]
    base = agent_tools._base_instructions(client)
    assert "Answer in Chinese." in base and agent_search.GUIDANCE in base


# ───────────────────────────────────────────────────────────── passages
def _texts(passages: list[tuple[int, int, str]]) -> list[str]:
    return [" ".join(t.split()) for _, _, t in passages]


def test_split_passages_headings_paragraphs_and_tables() -> None:
    body = "x" * 350
    page = (f"# Title\n\n## One\n\n{body}\n\n## Two\n\nshort text\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n## Three\n\nlast")
    parts = bm25.split_passages(page)
    texts = _texts(parts)
    assert texts[0].startswith("# Title ## One") and texts[0].endswith(body)
    # a heading starts a passage once the open one is long enough, and a small
    # table stays whole with the text around it
    assert texts[1] == "## Two short text | a | b | |---|---| | 1 | 2 | ## Three last"
    for start, end, text in parts:
        assert page[start:end] == text


def test_split_passages_never_ends_on_a_heading() -> None:
    page = "## A\n\n" + "y" * 500 + "\n\n### B\n\n" + "z" * 500
    texts = _texts(bm25.split_passages(page))
    assert texts == ["## A " + "y" * 500, "### B " + "z" * 500]


def test_split_long_pipe_table_repeats_header() -> None:
    rows = "\n".join(f"| row {i} | {i * 1000} |" for i in range(80))
    page = "## Table\n\n| item | value |\n|---|---|\n" + rows + "\n\nafter"
    parts = bm25.split_passages(page)
    tables = [t for _, _, t in parts if "| item | value |" in t]
    assert len(tables) >= 2
    assert tables[0].startswith("## Table")                 # heading rides on the first piece
    for t in tables:
        assert "| item | value |\n|---|---|" in t
        assert bm25._size(t) <= bm25.PASSAGE_MAX + len("## Table") + 1
    body_rows = [line for t in tables for line in t.splitlines() if line.startswith("| row")]
    assert len(body_rows) == 80                             # every row exactly once
    assert _texts(parts)[-1] == "after"


def test_split_long_html_table_repeats_header() -> None:
    rows = "".join(f"<tr><td>row {i}</td><td>{i * 7}</td></tr>" for i in range(60))
    page = f"<table><tr><th>Item</th><th>Value</th></tr>{rows}</table>"
    tables = [t for _, _, t in bm25.split_passages(page)]
    assert len(tables) >= 2
    for t in tables:
        assert t.startswith("<table><tr><th>Item</th><th>Value</th></tr><tr>")
        assert t.endswith("</table>")
        assert bm25._size(t) <= bm25.PASSAGE_MAX
    assert sum(t.count("<td>row ") for t in tables) == 60


def test_split_long_text_at_sentence_ends() -> None:
    sentence = "The group grew its business in many markets this year. "
    page = sentence * 40
    parts = bm25.split_passages(page)
    assert len(parts) >= 3
    for start, end, text in parts:
        assert bm25._size(text) <= bm25.PASSAGE_MAX
        assert text.rstrip().endswith(".")
    assert "".join(t for _, _, t in parts) == page


def test_index_has_passage_counts() -> None:
    index = bm25.build_index(["# A\n\nalpha", "", "beta\n\n" + "gamma " * 300])
    assert index["version"] == bm25.VERSION == 2
    passages = index["passages"]
    assert passages["pages"][0] == 1 and set(passages["pages"]) == {1, 3}
    assert len(passages["lengths"]) == len(passages["pages"])
    assert passages["postings"]["alpha"] == [0, 1]
    assert index["postings"]["alpha"] == [1, 1] and index["postings"]["gamma"][0] == 3


# ───────────────────────────────────────────────────────────── match modes
FILLER = ("Management continued to invest in training, digital tools and customer service "
          "across the region, and cost discipline remained a priority for every team. ")


@pytest.fixture()
def diluted(tmp_path: Path) -> Path:
    """Page 1: a short summary naming the terms; page 2: a long page whose one
    paragraph holds the fact."""
    md = tmp_path / "report.md"
    md.write_text(
        "<!-- page: 1 -->\n\n## Highlights\n\nValue of new business by market and "
        "channel (Vietnam, agency) is shown on the market pages. " + FILLER * 3 + "\n\n"
        "<!-- page: 2 -->\n\n## Markets\n\n" + (FILLER * 4 + "\n\n") * 6
        + "### Vietnam\n\nIn Vietnam, the agency channel's value of new business was 187 "
        "million; new business from agency grew as Vietnam agents gained value.\n\n"
        + (FILLER * 4 + "\n\n") * 6,
        encoding="utf-8")
    store = tmp_path / "store"
    index_markdown(md, store)
    return store


def test_passage_mode_beats_length_dilution(diluted: Path) -> None:
    query = "Vietnam agency value of new business"
    page = bm25.search(diluted, query, top_k=5, match="page")
    assert page.match == "page" and [h.page for h in page.hits] == [1, 2]
    passage = bm25.search(diluted, query, top_k=5, match="passage")
    assert passage.match == "passage" and [h.page for h in passage.hits] == [2, 1]
    top = passage.hits[0]
    assert "187" in top.snippet and "**Vietnam**" in top.snippet     # the passage's snippet
    assert top.section.endswith("Vietnam")
    # the page score can be mixed back in
    mixed = bm25.search(diluted, query, top_k=5, match="passage", page_weight=100.0)
    assert [h.page for h in mixed.hits] == [1, 2]


def test_passage_mode_returns_each_page_once(store: Path) -> None:
    hits = bm25.search(store, "2023 2022 dividend", top_k=20, match="passage").hits
    keys = [(h.doc_id, h.page) for h in hits]
    assert keys and len(keys) == len(set(keys))
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_match_from_environment(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(bm25.MATCH_ENV, "passage")
    assert bm25.resolve_match() == "passage" and bm25.resolve_match("page") == "page"
    assert bm25.search(store, "dividend").match == "passage"
    monkeypatch.setenv(bm25.MATCH_ENV, "chunk")
    with pytest.raises(ValueError, match="SUPERINDEX_BM25_MATCH"):
        bm25.resolve_match()
    monkeypatch.delenv(bm25.MATCH_ENV)
    assert bm25.resolve_match() == "page"


def test_v1_index_is_rebuilt_with_passages(tmp_path: Path) -> None:
    md = tmp_path / "doc.md"
    md.write_text("# A\n\nalpha beta\n", encoding="utf-8")
    store = tmp_path / "store"
    res = index_markdown(md, store)
    index_file = store / "docs" / res.doc_id / "bm25.json"
    old = json.loads(index_file.read_text(encoding="utf-8"))
    del old["passages"]
    old["version"] = 1                                        # an index from before passages
    index_file.write_text(json.dumps(old), encoding="utf-8")

    result = bm25.search(store, "alpha", match="passage")
    assert result.built == ["doc.md"] and result.hits
    rebuilt = json.loads(index_file.read_text(encoding="utf-8"))
    assert rebuilt["version"] == 2 and rebuilt["passages"]["postings"]["alpha"] == [0, 1]
    assert bm25.search(store, "alpha", match="passage").built == []


def test_search_cli_match_flag(diluted: Path) -> None:
    query = "Vietnam agency value of new business"
    out = run_cli("search", query, "--store", str(diluted), "--match", "passage", "--json")
    assert out.returncode == 0, out.stderr
    assert [h["page"] for h in json.loads(out.stdout)] == [2, 1]
    out = run_cli("search", query, "--store", str(diluted), "--json")
    assert [h["page"] for h in json.loads(out.stdout)] == [1, 2]
    out = run_cli("search", query, "--store", str(diluted), "--match", "chunk")
    assert out.returncode == 2


def test_tool_reports_match(diluted: Path, installed: None,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    from pageindex import agent_tools

    monkeypatch.setenv(bm25.MATCH_ENV, "passage")
    payload, err = _call(agent_tools._tool_specs(_client(diluted)),
                         {"query": "Vietnam agency value of new business"})
    assert not err and payload["match"] == "passage"
    assert payload["results"][0]["page"] == 2 and "187" in payload["results"][0]["snippet"]
