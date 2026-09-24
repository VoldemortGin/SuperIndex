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
