"""Offline tests for document metadata (`superindex.md_ingest.extract_doc_meta`)
and period routing (`superindex.batch.route_scope`). No LLM: the completion
call is monkeypatched.

    pytest tests/test_doc_meta_routing.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import batch, cli, md_ingest  # noqa: E402
from superindex.md_ingest import (  # noqa: E402
    backfill_doc_meta,
    doc_meta_from_filename,
    extract_doc_meta,
    index_markdown,
    normalize_period,
    parse_doc_meta_reply,
)
from superindex.nav.policy import RoutingPolicy  # noqa: E402

EMPTY = RoutingPolicy()
REPORT = "# Annual Report 2024\n\n## Chairman's Statement\n\nVONB grew 20% compared with 2023.\n"


# ───────────────────────────────────────────────────────────── file names / periods
@pytest.mark.parametrize(("name", "period", "kind"), [
    ("AIA_AR2024.md", "FY2024", "annual"),
    ("AIA_IR2024H1.md", "1H2024", "interim"),
    ("PingAn_IR2023H1.md", "1H2023", "interim"),
    ("HarbourLife_FY23_annual.md", "FY2023", "annual"),
    ("友邦2024年中期报告.md", "1H2024", "interim"),
    ("CPIC_3Q2024.md", "3Q2024", "quarterly"),
    ("report.md", None, None),
])
def test_doc_meta_from_filename(name: str, period: str | None, kind: str | None) -> None:
    meta = doc_meta_from_filename(name)
    assert (meta["period"], meta["report_type"]) == (period, kind)
    assert meta["company"] is None and meta["region"] is None


@pytest.mark.parametrize(("text", "period"), [
    ("FY2022", "FY2022"), ("2022", "FY2022"), ("FY22", "FY2022"), ("1H2025", "1H2025"),
    ("2025H1", "1H2025"), ("H1 2025", "1H2025"), ("2025年上半年", "1H2025"),
    ("Interim 2025", "1H2025"), ("null", None), (None, None), ("annual", None),
])
def test_normalize_period(text: str | None, period: str | None) -> None:
    assert normalize_period(text) == period


def test_normalize_period_interim_report_type() -> None:
    assert normalize_period("2024", "interim") == "1H2024"


# ───────────────────────────────────────────────────────────── LLM output
def test_parse_doc_meta_reply() -> None:
    reply = ('Sure:\n```json\n{"company": "AIA Group Limited", "region": "集团", '
             '"period": "2024", "report_type": "年报", "description": "友邦 2024 年报"}\n```')
    assert parse_doc_meta_reply(reply) == {
        "company": "AIA Group Limited", "region": "集团", "period": "FY2024",
        "report_type": "annual", "description": "友邦 2024 年报"}
    assert parse_doc_meta_reply('{"period": "2024年中期", "region": "null"}') == {
        "period": "1H2024", "report_type": "interim"}
    assert parse_doc_meta_reply("no json here") == {}
    assert parse_doc_meta_reply("{broken") == {}


def _fake_llm(monkeypatch: pytest.MonkeyPatch, reply: Any) -> list[str]:
    from superindex.engine import utils

    prompts: list[str] = []

    def fake(model: str, prompt: str, **_: Any) -> str:
        prompts.append(prompt)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(utils, "llm_completion", fake)
    return prompts


def test_extract_doc_meta_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    prompts = _fake_llm(monkeypatch, json.dumps({
        "company": "AIA", "region": "集团", "period": "FY2024", "report_type": "annual",
        "description": "AIA 2024 annual report"}))
    meta = extract_doc_meta("AIA_AR2024.md", REPORT.splitlines(), "fake/model")
    assert meta["source"] == "llm" and meta["model"] == "fake/model"
    assert (meta["company"], meta["period"], meta["report_type"]) == ("AIA", "FY2024", "annual")
    prompt = prompts[0]
    assert "AIA_AR2024.md" in prompt and "- Chairman's Statement" in prompt
    assert "VONB grew 20%" in prompt and "对比数" in prompt


def test_extract_doc_meta_falls_back_to_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_llm(monkeypatch, RuntimeError("down"))
    meta = extract_doc_meta("AIA_IR2024H1.md", REPORT.splitlines(), "fake/model")
    assert meta["source"] == "filename" and "RuntimeError: down" in meta["error"]
    assert (meta["period"], meta["report_type"]) == ("1H2024", "interim")

    _fake_llm(monkeypatch, '{"company": "AIA", "period": null}')
    meta = extract_doc_meta("AIA_AR2024.md", REPORT.splitlines(), "fake/model")
    assert meta["source"] == "llm+filename"
    assert (meta["company"], meta["period"]) == ("AIA", "FY2024")

    meta = extract_doc_meta("AIA_AR2024.md", REPORT.splitlines())    # no model, no call
    assert meta["source"] == "filename" and meta["model"] is None and "error" not in meta


def _stored(store: Path, doc_id: str) -> dict[str, Any]:
    return json.loads((store / "docs" / doc_id / "doc.json").read_text(encoding="utf-8"))


def test_index_and_backfill_store_doc_meta(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    md = tmp_path / "AIA_AR2024.md"
    md.write_text(REPORT, encoding="utf-8")
    store = tmp_path / "store"
    res = index_markdown(md, store)                       # default: file-name rules
    meta = _stored(store, res.doc_id)
    assert meta["metadata"]["doc_meta"]["period"] == "FY2024"
    assert meta["description"] is None

    prompts = _fake_llm(monkeypatch, json.dumps({
        "company": "AIA", "region": "集团", "period": "FY2024", "report_type": "annual",
        "description": "AIA 2024 annual report"}))
    again = index_markdown(md, store, doc_meta_model="fake/model")   # skipped, meta added
    assert again.skipped and again.doc_id == res.doc_id and len(prompts) == 1
    assert again.doc_meta_added and not res.doc_meta_added
    meta = _stored(store, res.doc_id)
    assert meta["metadata"]["doc_meta"]["source"] == "llm"
    assert meta["description"] == "AIA 2024 annual report"
    third = index_markdown(md, store, doc_meta_model="fake/model")   # same model: no new call
    assert third.skipped and not third.doc_meta_added
    assert len(prompts) == 1

    other = tmp_path / "CPIC_IR2023H1.md"
    other.write_text(REPORT, encoding="utf-8")
    plain = index_markdown(other, store, doc_meta=False)
    assert "doc_meta" not in _stored(store, plain.doc_id)["metadata"]
    done = backfill_doc_meta(store)                                  # no model: file names
    assert [(n, m["period"]) for n, m in done] == [("CPIC_IR2023H1.md", "1H2023")]
    assert backfill_doc_meta(store) == []
    assert len(backfill_doc_meta(store, force=True)) == 2
    assert _stored(store, plain.doc_id)["pageNum"] == 1                # text untouched


# ───────────────────────────────────────────────────────────── routing
def _doc(name: str, period: str | None, kind: str | None = None, company: str | None = None,
         region: str | None = None) -> dict[str, Any]:
    meta = {"period": period, "report_type": kind or md_ingest.normalize_report_type(None, period),
            "company": company, "region": region}
    return {"id": "pi-" + name, "name": name, "status": "completed",
            "metadata": {"doc_meta": meta}}


DOCS = [_doc("AR2023.md", "FY2023"), _doc("IR2023.md", "1H2023"),
        _doc("AR2024.md", "FY2024"), _doc("IR2024.md", "1H2024"),
        _doc("AR2025.md", "FY2025"), _doc("notes.md", None)]


def _names(route: batch.Route) -> list[str]:
    return [n.removesuffix(".md") for n in route.docs]


def test_route_year_with_next_year_and_unknown_period() -> None:
    route = batch.route_scope(DOCS, "2023 年新业务价值是多少？", policy=EMPTY)
    assert route.reason == "routed" and not route.fallback
    assert _names(route) == ["AR2023", "IR2023", "AR2024", "IR2024", "notes"]
    assert route.scope_ids == ["pi-" + n for n in route.docs]
    assert "2023" in route.note and route.fields()["route_reason"] == "routed"

    route = batch.route_scope(DOCS, "2023 年新业务价值是多少？", adjacent=False, policy=EMPTY)
    assert _names(route) == ["AR2023", "IR2023", "notes"]


def test_route_interim_question() -> None:
    route = batch.route_scope(DOCS, "2024 年上半年的 VONB？", policy=EMPTY)
    assert route.interim_only and _names(route) == ["IR2024", "notes"]
    # an interim dividend in a full-year question is not an interim-only question
    route = batch.route_scope(DOCS, "2024 年中期股息是多少？", adjacent=False, policy=EMPTY)
    assert _names(route) == ["AR2024", "IR2024", "notes"]
    # no interim report for that year: every report type of the year
    route = batch.route_scope(DOCS, "2025 年上半年的 VONB？", policy=EMPTY)
    assert _names(route) == ["AR2025", "notes"]


def test_route_fallbacks_and_doc() -> None:
    route = batch.route_scope(DOCS, "新业务价值是多少？", policy=EMPTY)
    assert (route.scope_ids, route.reason, route.fallback) == (None, "no_period", True)
    route = batch.route_scope(DOCS, "2019 年的碳排放？", policy=EMPTY)
    assert (route.scope_ids, route.reason) == (None, "no_match")
    bare = [{"id": "pi-x", "name": "x.md", "metadata": {}}]
    assert batch.route_scope(bare, "2024 年？", policy=EMPTY).reason == "no_meta"
    assert batch.route_scope(DOCS, "2024 年？", enabled=False).scope_ids is None
    route = batch.route_scope(DOCS, "2024 年？", ["AR2023"], policy=EMPTY)
    assert (route.scope_ids, route.reason, route.fallback) == (["pi-AR2023.md"], "doc", False)


def test_route_company_and_region() -> None:
    docs = [_doc("aia.md", "FY2024", company="AIA Group Limited", region="集团"),
            _doc("aia_sg.md", "FY2024", company="AIA Singapore", region="新加坡"),
            _doc("pingan.md", "FY2024", company="中国平安保险（集团）股份有限公司", region="集团"),
            _doc("x.md", "FY2024")]
    route = batch.route_scope(docs, "AIA 2024 VONB", adjacent=False, policy=EMPTY)
    assert _names(route) == ["aia", "x"] and "AIA Group Limited" in route.note
    # a mentioned region narrows, group-level reports stay
    route = batch.route_scope(docs, "AIA 2024 年新加坡 VONB", adjacent=False, policy=EMPTY)
    assert _names(route) == ["aia", "x"]
    route = batch.route_scope(docs, "2024 年新加坡 VONB", adjacent=False, policy=EMPTY)
    assert _names(route) == ["aia", "aia_sg", "pingan", "x"]
    # not mentioned (no alias): no company filter
    route = batch.route_scope(docs, "友邦 2024 VONB", adjacent=False, policy=EMPTY)
    assert len(route.docs) == 4
    aliased = RoutingPolicy.from_dict({"aliases": {"友邦": ["AIA Group Limited"]}})
    route = batch.route_scope(docs, "友邦 2024 VONB", adjacent=False, policy=aliased)
    assert _names(route) == ["aia", "x"]


def test_question_periods_policy_patterns() -> None:
    policy = RoutingPolicy.from_dict({"periods": [r"FY(\d{2})"]})
    assert batch.question_periods("FY24 dividend", policy) == ([2024], False)
    assert batch.question_periods("中国太保 2024 年上半年报告 中期股息", EMPTY) == ([2024], True)


# ───────────────────────────────────────────────────────────── diagnostics
def _read(*names: str) -> dict[str, Any]:
    return {"tool_calls": [{"name": "get_page_content", "arguments": {"doc_name": n, "pages": "1"}}
                           for n in names] + [{"name": "search_pages", "arguments": {}}]}


def test_route_diagnostics() -> None:
    route = batch.route_scope(DOCS, "2024 年 VONB", adjacent=False, policy=EMPTY)
    diag = batch.route_diagnostics(_read("AR2024.md", "AR2023.md", "AR2024.md"), route, DOCS)
    assert diag == {"read_docs": ["AR2024.md", "AR2023.md"], "read_outside_route": ["AR2023.md"],
                    "read_year_mismatch": ["AR2023.md"], "read_out_of_range": True}
    assert batch.route_diagnostics(_read("AR2024.md"), route, DOCS)["read_out_of_range"] is False
    # whole store, no year in the question: nothing to judge
    route = batch.route_scope(DOCS, "VONB", policy=EMPTY)
    assert batch.route_diagnostics(_read("AR2023.md"), route, DOCS)["read_out_of_range"] is None
    # whole store after no_match: the year still judges what was read
    route = batch.route_scope(DOCS, "2019 年 VONB", policy=EMPTY)
    diag = batch.route_diagnostics(_read("AR2023.md"), route, DOCS)
    assert diag["read_year_mismatch"] == ["AR2023.md"] and diag["read_out_of_range"] is True


def test_route_stats_and_summary_lines() -> None:
    def rec(reason: str, out: bool | None, hit: bool | None, error: str | None = None) -> dict:
        return {"route_reason": reason, "route_fallback": reason in batch.ROUTE_FALLBACKS,
                "read_out_of_range": out, "error": error,
                "score": None if hit is None else {"hit": hit}}

    records = [rec("routed", False, True), rec("routed", True, False), rec("no_period", None, False),
               rec("no_match", True, True), rec("doc", True, None), rec("routed", True, False, "x"),
               {"id": "old record"}]
    stats = batch.route_stats(records)
    assert stats == {"questions": 6, "routed": 3, "doc": 1, "off": 0, "fallback": 2,
                     "fallback_no_period": 1, "fallback_no_meta": 0, "fallback_no_match": 1,
                     "read_out_of_range": 4, "read_out_of_range_wrong": 1}
    lines = batch.route_summary_lines(records)
    assert "命中 3/6" in lines[0] and "回退全库 2" in lines[0]
    assert "4 题" in lines[1] and "判错 1 题" in lines[1]
    assert batch.route_summary_lines([{"id": "x"}]) == []


# ───────────────────────────────────────────────────────────── cmd_batch
class _Stream:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = iter(events)


class _Client:
    def __init__(self) -> None:
        self.scopes: dict[str, Any] = {}

    def chat(self, message: str, doc_id: Any = None, stream: bool = False,
             reasoning_effort: str | None = None) -> _Stream:
        question = message.rsplit("问题：", 1)[-1]
        self.scopes[question] = doc_id
        return _Stream([
            {"type": "tool_call", "name": "get_page_content",
             "arguments": {"doc_name": "AIA_AR2023.md", "pages": "1"}},
            {"type": "tool_result", "name": "get_page_content", "output": "{}"},
            {"type": "answer", "delta": "20%"}])


def test_cmd_batch_routes_questions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = tmp_path / "store"
    for name in ("AIA_AR2023.md", "AIA_AR2024.md", "AIA_AR2025.md"):
        (tmp_path / name).write_text(REPORT, encoding="utf-8")
        index_markdown(tmp_path / name, store)
    qfile = tmp_path / "q.jsonl"
    qfile.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"id": "A", "question": "2024 年 VONB 增长多少？", "expected": "20%"},
        {"id": "B", "question": "VONB 增长多少？", "expected": "21%"},
        {"id": "C", "question": "2024 年 VONB？", "doc": "AIA_AR2023"},
    ]) + "\n", encoding="utf-8")
    fake = _Client()
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: fake)
    out = tmp_path / "out"
    args = cli.build_parser().parse_args(["batch", str(qfile), "--store", str(store), "--out",
                                          str(out), "--chat-model", "fake/model",
                                          "--no-route-adjacent"])
    assert batch.cmd_batch(args) == 0

    recs = batch.read_results(out)
    assert recs["A"]["route_reason"] == "routed" and recs["A"]["routed_docs"] == ["AIA_AR2024.md"]
    assert isinstance(fake.scopes["2024 年 VONB 增长多少？"], str)
    assert recs["A"]["read_outside_route"] == ["AIA_AR2023.md"]
    assert recs["A"]["read_out_of_range"] is True
    assert recs["B"]["route_fallback"] is True and recs["B"]["scope"] is None
    assert recs["B"]["read_out_of_range"] is None
    assert recs["C"]["route_reason"] == "doc" and recs["C"]["routed_docs"] == ["AIA_AR2023.md"]
    assert recs["C"]["read_year_mismatch"] == ["AIA_AR2023.md"]
    summary = (out / batch.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "期间路由：命中 1/3" in summary and "其中粗评分判错 0 题" in summary

    args = cli.build_parser().parse_args(["batch", str(qfile), "--store", str(store), "--out",
                                          str(tmp_path / "off"), "--chat-model", "fake/model",
                                          "--no-route"])
    assert batch.cmd_batch(args) == 0
    off = batch.read_results(tmp_path / "off")
    assert off["A"]["route_reason"] == "off" and off["A"]["scope"] is None
