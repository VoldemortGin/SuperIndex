"""Offline tests for report types (source folder / file name → `report_type`,
`config/routing_policy.yaml` `report_types`), finer periods, the rule-only
doc_meta backfill, type-aware routing and the agent's `[type · period]` labels.
Synthetic folders and Markdown only; every LLM call is forbidden.

    pytest tests/test_report_types.py
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import batch, md_ingest
from superindex.md_ingest import (
    backfill_doc_meta,
    clash_names,
    doc_meta_from_filename,
    doc_meta_stats,
    drop_clash_leftovers,
    index_markdown,
    normalize_period,
    pdf_source_path,
    same_name_sources,
)
from superindex.nav.policy import POLICY_ENV, RoutingPolicy

POLICY = RoutingPolicy.load(ROOT / "config" / "routing_policy.yaml")
FOLDERS = {"qmr": "QMR Encropyted", "mbr": "MBR QBR_Finance  Part Encropyted",
           "factbook": "Factbook Encropyted", "trend": "Country Trends Encropyted",
           "deck": "GO EXCO Deco Encropyted"}
TEXT = "<!-- page: 1 -->\n\n# Overview\n\nNew business value grew 12%.\n"


@pytest.fixture(autouse=True)
def _shipped_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(POLICY_ENV, str(ROOT / "config" / "routing_policy.yaml"))


@pytest.fixture
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from superindex.engine import utils

    def forbidden(*_: Any, **__: Any) -> str:
        raise AssertionError("LLM must not be called")

    monkeypatch.setattr(utils, "llm_completion", forbidden)


def _pdf_tree(root: Path, files: dict[str, list[str]]) -> Path:
    """`root/<folder>/<name>` empty PDFs; keys of `files` are FOLDERS keys."""
    for key, names in files.items():
        for name in names:
            path = root / FOLDERS[key] / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4\n")
    return root


# ───────────────────────────────────────────────────────────── types
@pytest.mark.parametrize(("folder", "name", "kind"), [
    ("qmr", "QMR 2022Q3.pdf", "qmr"),
    ("mbr", "Finance MBR Mar 2022.pdf", "mbr"),
    ("mbr", "Monthly report 2022-03.pdf", "mbr"),
    ("mbr", "QBR Q3 2022.pdf", "qbr"),
    ("mbr", "Quarterly Business Review 3Q22.pdf", "qbr"),
    ("mbr", "Finance pack 2022.pdf", "mbr_qbr"),
    ("mbr", "MBR and QBR 2022.pdf", "mbr_qbr"),
    ("factbook", "FB_YE2022.pdf", "factbook"),
    ("trend", "HK 2022.pdf", "exco_trend"),
    ("deck", "Deck Sep 2023.pdf", "go_exco_deck"),
])
def test_folder_sets_report_type(folder: str, name: str, kind: str) -> None:
    source = f"{FOLDERS[folder]}/{name}"
    assert POLICY.report_type_for(source, name) == (kind, "folder")
    meta = doc_meta_from_filename(name.replace(".pdf", ".md"), source, POLICY)
    assert meta["report_type"] == kind and meta["report_type_source"] == "folder"
    assert meta["source_path"] == source and meta["source_folder"] == FOLDERS[folder]


def test_file_name_aliases_then_legacy_types() -> None:
    assert POLICY.report_type_for(None, "Factbook_1H2022.md") == ("factbook", "filename")
    assert POLICY.report_type_for("other/GO EXCO Deck 2023.pdf", "GO EXCO Deck 2023.pdf") \
        == ("go_exco_deck", "filename")
    assert POLICY.report_type_for(None, "AIA_AR2024.md") == (None, None)
    meta = doc_meta_from_filename("AIA_AR2024.md", None, POLICY)     # unchanged legacy rules
    assert (meta["report_type"], meta["period"]) == ("annual", "FY2024")
    assert "report_type_source" not in meta
    # an LLM's free-text type maps to a configured key
    assert md_ingest.normalize_report_type("Quarterly Management Report", policy=POLICY) == "qmr"
    assert md_ingest.normalize_report_type("年报", policy=POLICY) == "annual"


def test_question_names_types() -> None:
    assert POLICY.report_types_in("2022 年 QMR 里的新业务价值？") == ["qmr"]
    assert POLICY.report_types_in("What does the Quarterly Business Review say?") == ["qbr"]
    assert POLICY.report_types_in("GO EXCO deck 2023 的重点") == ["go_exco_deck"]
    assert POLICY.report_types_in("2022 年 monthly 保费") == []          # file-name-only word
    assert POLICY.report_types_in("2022 年香港的新业务价值") == []
    assert POLICY.covering_types(["mbr"]) == {"mbr", "mbr_qbr"}


def test_bad_report_types_config() -> None:
    from superindex.nav.policy import PolicyError

    with pytest.raises(PolicyError):
        RoutingPolicy.from_dict({"report_types": {"x": {"split": ["nope"]}}})
    with pytest.raises(PolicyError):
        RoutingPolicy.from_dict({"report_types": ["qmr"]})
    assert "报告类型" in POLICY.describe()


# ───────────────────────────────────────────────────────────── periods
@pytest.mark.parametrize(("text", "period"), [
    ("Factbook 1H2022", "1H2022"), ("Factbook HY22", "1H2022"),
    ("Factbook H1 2022", "1H2022"), ("Factbook YE2022", "FY2022"),
    # Q2 / Q4 stay quarters unless the type says otherwise (`quarter_as_half`)
    ("QMR Q2 2022", "2022Q2"), ("QMR 4Q22", "2022Q4"), ("Pack Q4 2022", "2022Q4"),
    ("Factbook YE Dec 2022", "FY2022"), ("FY22", "FY2022"), ("2H2022", "2H2022"),
    ("2022年下半年", "2H2022"), ("QMR 2022Q3", "2022Q3"), ("QMR Q3 2022", "2022Q3"),
    ("QMR 3Q22", "2022Q3"), ("QMR Q3'22", "2022Q3"), ("QMR_2022_Q1", "2022Q1"),
    ("3Q2024", "2024Q3"), ("第三季度 2022", "2022Q3"), ("MBR Mar 2022", "2022M03"),
    ("MBR March 2022", "2022M03"), ("MBR Mar-22", "2022M03"), ("MBR 2022-03", "2022M03"),
    ("MBR 202203", "2022M03"), ("2022M03", "2022M03"), ("2022年3月", "2022M03"),
    ("03/2022", "2022M03"), ("Country Trends 2022", "FY2022"),
    # ambiguous: not filled
    ("QMR Q1-Q3 2022", None), ("Trends 2021-2022", None), ("Jan 2022 to Feb 2022", None),
    ("no period", None),
])
def test_normalize_period_forms(text: str, period: str | None) -> None:
    assert normalize_period(text) == period


def test_quarter_as_half_only_for_factbook() -> None:
    assert POLICY.quarter_as_half("factbook") and not POLICY.quarter_as_half("qmr")
    assert not POLICY.quarter_as_half(None)
    assert normalize_period("Factbook Q2 2022", "factbook", True) == "1H2022"
    assert normalize_period("Factbook Q4 2022", "factbook", True) == "FY2022"
    assert normalize_period("Factbook Q3 2022", "factbook", True) == "2022Q3"
    periods = {source: doc_meta_from_filename(Path(source).with_suffix(".md").name, source,
                                              POLICY)["period"]
               for source in (f"{FOLDERS['factbook']}/Pack Q2 2022.pdf",
                              f"{FOLDERS['factbook']}/Pack Q4 2022.pdf",
                              f"{FOLDERS['qmr']}/Pack Q2 2022.pdf",
                              f"{FOLDERS['qmr']}/Pack Q4 2022.pdf",
                              f"{FOLDERS['deck']}/Pack Q4 2022.pdf",
                              "loose/Pack Q4 2022.pdf")}
    assert list(periods.values()) == ["1H2022", "FY2022", "2022Q2", "2022Q4", "2022Q4", "2022Q4"]
    assert doc_meta_from_filename("Factbook_Q2_2022.md", None, POLICY)["period"] == "1H2022"
    assert md_ingest.period_year("2022Q4") == 2022
    with pytest.raises(Exception, match="quarter_as_half"):
        RoutingPolicy.from_dict({"report_types": {"x": {"quarter_as_half": "yes"}}})


def test_llm_quarter_follows_folder_type(monkeypatch: pytest.MonkeyPatch) -> None:
    from superindex.engine import utils

    monkeypatch.setattr(utils, "llm_completion", lambda *_a, **_k: json.dumps(
        {"company": "X", "period": "Q4 2022", "report_type": "other"}))
    for folder, period in (("factbook", "FY2022"), ("qmr", "2022Q4")):
        meta = md_ingest.extract_doc_meta("Pack.md", TEXT.splitlines(), "fake/model",
                                          source_path=f"{FOLDERS[folder]}/Pack.pdf")
        assert meta["period"] == period


def test_period_year_and_quarterly_type() -> None:
    assert [md_ingest.period_year(p) for p in ("2022Q3", "2022M03", "2H2022")] == [2022] * 3
    assert md_ingest.normalize_report_type(None, "2022Q3") == "quarterly"
    assert md_ingest.normalize_report_type(None, "2022M03") is None


# ───────────────────────────────────────────────────────────── source paths
def test_pdf_source_path_and_name_clashes(tmp_path: Path) -> None:
    pdf_dir = _pdf_tree(tmp_path / "pdfs", {"qmr": ["QMR 2022Q3.pdf", "Pack 2022.pdf"],
                                            "factbook": ["Pack 2022.pdf", "FB YE2022.pdf"]})
    md_dir = tmp_path / "md"
    pdfs = md_ingest.page_images.pdf_index(pdf_dir)
    # mirrored layout (what step 1 writes)
    mirrored = md_dir / FOLDERS["qmr"] / "QMR 2022Q3.md"
    mirrored.parent.mkdir(parents=True)
    mirrored.write_text(TEXT, encoding="utf-8")
    assert pdf_source_path(mirrored, pdf_dir, md_dir, pdfs) == f"{FOLDERS['qmr']}/QMR 2022Q3.pdf"
    # flat old md: found by file name; ambiguous name: None
    flat = md_dir / "FB YE2022.md"
    flat.write_text(TEXT, encoding="utf-8")
    assert pdf_source_path(flat, pdf_dir, md_dir, pdfs) == f"{FOLDERS['factbook']}/FB YE2022.pdf"
    clash = md_dir / "Pack 2022.md"
    clash.write_text(TEXT, encoding="utf-8")
    assert pdf_source_path(clash, pdf_dir, md_dir, pdfs) is None
    # the sidecar wins
    clash.with_suffix(".meta.json").write_text(json.dumps(
        {"source_path": f"{FOLDERS['factbook']}/Pack 2022.pdf"}), encoding="utf-8")
    assert pdf_source_path(clash, pdf_dir, md_dir, pdfs) == f"{FOLDERS['factbook']}/Pack 2022.pdf"
    rel = [p.relative_to(pdf_dir).as_posix() for p in sorted(pdf_dir.rglob("*.pdf"))]
    assert same_name_sources(rel) == {"Pack 2022.md": [f"{FOLDERS['factbook']}/Pack 2022.pdf",
                                                       f"{FOLDERS['qmr']}/Pack 2022.pdf"]}


def test_clash_names_prefix_only_clashing() -> None:
    rel = [f"{FOLDERS['qmr']}/Pack 2022.pdf", f"{FOLDERS['factbook']}/Pack 2022.pdf",
           f"{FOLDERS['qmr']}/QMR 2022Q3.pdf", "2021/Factbook/Deck.pdf", "2022/Factbook/Deck.pdf",
           "Solo.pdf", "sub/Solo.pdf"]
    assert clash_names(rel) == {
        f"{FOLDERS['qmr']}/Pack 2022.pdf": f"{FOLDERS['qmr']}__Pack 2022.md",
        f"{FOLDERS['factbook']}/Pack 2022.pdf": f"{FOLDERS['factbook']}__Pack 2022.md",
        "2021/Factbook/Deck.pdf": "2021__Factbook__Deck.md",     # same folder name: more path
        "2022/Factbook/Deck.pdf": "2022__Factbook__Deck.md",
        "Solo.pdf": "Solo.md", "sub/Solo.pdf": "sub__Solo.md",
    }
    assert clash_names(list(reversed(rel))) == clash_names(rel)


# ───────────────────────────────────────────────────────────── store
def _stored(store: Path, doc_id: str) -> dict[str, Any]:
    return json.loads((store / "docs" / doc_id / "doc.json").read_text(encoding="utf-8"))


def test_index_with_source_path(tmp_path: Path, no_llm: None) -> None:
    md = tmp_path / "Finance MBR Mar 2022.md"
    md.write_text(TEXT, encoding="utf-8")
    source = f"{FOLDERS['mbr']}/Finance MBR Mar 2022.pdf"
    res = index_markdown(md, tmp_path / "store", source_path=source)
    meta = _stored(tmp_path / "store", res.doc_id)["metadata"]["doc_meta"]
    assert (meta["report_type"], meta["period"], meta["source_path"]) == ("mbr", "2022M03", source)
    assert meta["period_source"] == "filename" and meta["rules"] == md_ingest.DOC_META_RULES


def test_file_name_period_beats_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from superindex.engine import utils

    monkeypatch.setattr(utils, "llm_completion", lambda *_a, **_k: json.dumps(
        {"company": "X", "period": "FY2022", "report_type": "other"}))
    meta = md_ingest.extract_doc_meta("QMR Q3 2022.md", TEXT.splitlines(), "fake/model",
                                      source_path=f"{FOLDERS['qmr']}/QMR Q3 2022.pdf")
    assert (meta["company"], meta["period"], meta["report_type"]) == ("X", "2022Q3", "qmr")
    assert meta["source"] == "llm+filename" and meta["period_source"] == "filename"


def test_backfill_rules_only(tmp_path: Path, no_llm: None) -> None:
    pdf_dir = _pdf_tree(tmp_path / "pdfs", {"qmr": ["QMR 3Q2022.pdf"], "trend": ["HK.pdf"]})
    store = tmp_path / "store"
    ids = {}
    for name in ("QMR 3Q2022.md", "HK.md", "Loose 2021.md"):
        (tmp_path / name).write_text(TEXT + name, encoding="utf-8")
        ids[name] = index_markdown(tmp_path / name, store).doc_id
    # make them look like metadata from before this change (LLM-extracted, old spelling)
    for name, doc_id in ids.items():
        path = store / "docs" / doc_id / "doc.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["metadata"]["doc_meta"] = {"company": "ACME", "period": "3Q2022" if "QMR" in name
                                       else None, "report_type": "other", "source": "llm",
                                       "model": "old/model"}
        path.write_text(json.dumps(doc), encoding="utf-8")
    (store / "manifest.json").unlink()

    done = dict(backfill_doc_meta(store, pdf_dir=pdf_dir))          # no model: no LLM either way
    assert set(done) == set(ids)
    qmr, hk, loose = done["QMR 3Q2022.md"], done["HK.md"], done["Loose 2021.md"]
    assert (qmr["report_type"], qmr["period"], qmr["company"]) == ("qmr", "2022Q3", "ACME")
    assert qmr["source_path"] == f"{FOLDERS['qmr']}/QMR 3Q2022.pdf" and qmr["model"] == "old/model"
    assert (hk["report_type"], hk["source_folder"]) == ("exco_trend", FOLDERS["trend"])
    assert (loose["report_type"], loose["period"]) == ("other", "FY2021")
    assert "source_path" not in loose
    assert backfill_doc_meta(store, pdf_dir=pdf_dir) == []
    stats = doc_meta_stats(store)
    assert "exco_trend 1" in stats and "qmr 1" in stats and "other 1" in stats
    assert "有期间 2" in stats and "来源文件夹无法识别类型 0，来源路径未知 1" in stats
    assert _stored(store, ids["HK.md"])["pageNum"] == 1                  # text untouched


def test_skip_removes_same_name_copies(tmp_path: Path, no_llm: None,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    md = tmp_path / "AIA_AR2024.md"
    md.write_text(TEXT, encoding="utf-8")
    store = tmp_path / "store"
    keep = index_markdown(md, store).doc_id
    # a copy restored from PERSIST_DIR next to the local one (same name, other id)
    for twin in ("pi-restored", "pi-older"):
        shutil.copytree(store / "docs" / keep, store / "docs" / twin)
        doc = json.loads((store / "docs" / twin / "doc.json").read_text(encoding="utf-8"))
        doc["id"] = twin
        if twin == "pi-older":
            doc["metadata"]["sha256"] = "0" * 64
        (store / "docs" / twin / "doc.json").write_text(json.dumps(doc), encoding="utf-8")
    (store / "manifest.json").unlink()
    from superindex.engine.local_store import DocStore

    assert len(DocStore(str(store)).list_metas()) == 3
    res = index_markdown(md, store)
    assert res.skipped
    left = [m["id"] for m in DocStore(str(store)).list_metas()]
    assert left == [res.doc_id] and _stored(store, res.doc_id)["metadata"]["sha256"] != "0" * 64
    assert "已删除其余 2 份" in capsys.readouterr().out


def test_same_name_in_two_folders_kept_apart(tmp_path: Path, no_llm: None) -> None:
    from superindex.engine.local_store import DocStore

    pdf_dir = _pdf_tree(tmp_path / "pdfs", {"qmr": ["Pack 2022.pdf", "QMR 2022Q3.pdf"],
                                            "factbook": ["Pack 2022.pdf"]})
    md_dir, store = tmp_path / "md", tmp_path / "store"
    sources = [p.relative_to(pdf_dir).as_posix() for p in sorted(pdf_dir.rglob("*.pdf"))]
    mds = []
    for source in sources:
        md = (md_dir / source).with_suffix(".md")
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(TEXT + source, encoding="utf-8")
        mds.append(md)
    # the store from before: one unprefixed copy of the clashing name
    index_markdown(mds[0], store)
    names = clash_names(sources)
    assert drop_clash_leftovers(store, sources) == ["Pack 2022.md"]
    pdfs = md_ingest.page_images.pdf_index(pdf_dir)

    def build() -> list[Any]:
        out = []
        for md in mds:
            source = pdf_source_path(md, pdf_dir, md_dir, pdfs)
            out.append(index_markdown(md, store, source_path=source, name=names.get(source or "")))
        return out

    first = build()
    stored = {m["name"]: m for m in DocStore(str(store)).list_metas()}
    assert sorted(stored) == sorted([f"{FOLDERS['factbook']}__Pack 2022.md",
                                     f"{FOLDERS['qmr']}__Pack 2022.md",
                                     "QMR 2022Q3.md"])                   # non-clashing name unchanged
    meta = stored[f"{FOLDERS['factbook']}__Pack 2022.md"]["metadata"]["doc_meta"]
    assert (meta["report_type"], meta["period"], meta["source_path"]) == \
        ("factbook", "FY2022", f"{FOLDERS['factbook']}/Pack 2022.pdf")
    again = build()
    assert all(r.skipped for r in again) and [r.doc_id for r in again] == [r.doc_id for r in first]
    assert drop_clash_leftovers(store, sources) == []
    # a question's doc "Pack 2022.pdf" still matches (both copies, by substring)
    docs = DocStore(str(store)).list_metas()
    assert len(batch._scope(docs, ["Pack 2022.pdf"])) == 2


# ───────────────────────────────────────────────────────────── routing
def _doc(name: str, period: str | None, kind: str | None) -> dict[str, Any]:
    return {"id": "pi-" + name, "name": name, "status": "completed",
            "metadata": {"doc_meta": {"period": period, "report_type": kind}}}


DOCS = [_doc("QMR_2022Q3", "2022Q3", "qmr"), _doc("QMR_2021Q3", "2021Q3", "qmr"),
        _doc("MBR_2022M03", "2022M03", "mbr"), _doc("MBRQBR_2022", "FY2022", "mbr_qbr"),
        _doc("FB_1H2022", "1H2022", "factbook"), _doc("FB_FY2022", "FY2022", "factbook"),
        _doc("Trend_2023", "FY2023", "exco_trend"), _doc("Deck_2020", "2020M09", "go_exco_deck"),
        _doc("Deck_x", None, "go_exco_deck")]


def _names(route: batch.Route) -> list[str]:
    return list(route.docs)


def test_route_years_not_quarters() -> None:
    route = batch.route_scope(DOCS, "2022 年第三季度新业务价值？", policy=POLICY)
    assert route.reason == "routed" and route.report_types == []
    assert _names(route) == ["QMR_2022Q3", "MBR_2022M03", "MBRQBR_2022", "FB_1H2022",
                             "FB_FY2022", "Trend_2023", "Deck_x"]
    route = batch.route_scope(DOCS, "Q3'22 新业务价值？", adjacent=False, policy=POLICY)
    assert route.years == [2022] and "Trend_2023" not in route.docs


def test_route_named_type_only() -> None:
    route = batch.route_scope(DOCS, "2022 年 QMR 的新业务价值？", policy=POLICY)
    assert _names(route) == ["QMR_2022Q3"] and route.report_types == ["qmr"]
    assert "类型 qmr" in route.note and route.fields()["route_report_types"] == ["qmr"]
    route = batch.route_scope(DOCS, "2022 年 MBR 的保费？", policy=POLICY)
    assert _names(route) == ["MBR_2022M03", "MBRQBR_2022"]
    # a document of unknown period still counts for its type
    route = batch.route_scope(DOCS, "2022 GO EXCO Deck 讲了什么？", adjacent=False, policy=POLICY)
    assert _names(route) == ["Deck_x"]
    # named type absent in those years: no type filter
    route = batch.route_scope(DOCS, "2022 EXCO Trend 的趋势？", adjacent=False, policy=POLICY)
    assert route.report_types == ["exco_trend"] and "类型" not in route.note
    assert "QMR_2022Q3" in route.docs and "Trend_2023" not in route.docs


def test_route_first_half_keeps_new_types() -> None:
    docs = DOCS + [_doc("AR2022", "FY2022", "annual"), _doc("IR2022", "1H2022", "interim")]
    route = batch.route_scope(docs, "2022 年上半年新业务价值？", adjacent=False, policy=POLICY)
    assert route.interim_only and "IR2022" in route.docs and "AR2022" not in route.docs
    assert {"QMR_2022Q3", "FB_FY2022", "FB_1H2022", "MBRQBR_2022"} <= set(route.docs)
    route = batch.route_scope(DOCS, "2022 年上半年新业务价值？", adjacent=False, policy=POLICY)
    assert "FB_FY2022" in route.docs and "不按期间类型排除" in route.note


def test_route_diagnostics_type_mismatch() -> None:
    route = batch.route_scope(DOCS, "2022 年 QMR 的新业务价值？", policy=POLICY)
    record = {"tool_calls": [{"name": "get_page_content",
                              "arguments": {"doc_name": n, "pages": "1"}}
                             for n in ("QMR_2022Q3", "FB_FY2022")]}
    diag = batch.route_diagnostics(record, route, DOCS)
    assert diag["read_type_mismatch"] == ["FB_FY2022"]
    assert diag["read_outside_route"] == ["FB_FY2022"]


# ───────────────────────────────────────────────────────────── agent tools
def test_browse_labels_and_report_type_filter(tmp_path: Path, no_llm: None) -> None:
    from superindex.engine import SuperIndexClient, agent_tools

    store = tmp_path / "store"
    for name, folder in (("QMR Q3 2022.md", "qmr"), ("FB YE2022.md", "factbook"),
                         ("notes.md", None)):
        (tmp_path / name).write_text(TEXT + name, encoding="utf-8")
        index_markdown(tmp_path / name, store,
                       source_path=f"{FOLDERS[folder]}/{name[:-3]}.pdf" if folder else None)
    client = SuperIndexClient(chat_model="openai/offline-test", storage_path=str(store))

    def browse(**arguments: Any) -> dict[str, Any]:
        text, is_error = agent_tools.call_tool(client, "browse_documents", arguments)
        assert not is_error
        return json.loads(text)

    labels = {d["name"]: d.get("label") for d in browse()["documents"]}
    assert labels == {"QMR Q3 2022.md": "[qmr · 2022Q3]", "FB YE2022.md": "[factbook · FY2022]",
                      "notes.md": None}
    assert [d["name"] for d in browse(report_type="QMR")["documents"]] == ["QMR Q3 2022.md"]
    assert [d["name"] for d in browse(report_type="factbook")["documents"]] \
        == ["FB YE2022.md"]
    empty = browse(report_type="mbr")
    assert empty["documents"] == [] and "report_type" in empty["next_steps"]["options"][0]
    text, _ = agent_tools.call_tool(client, "get_document", {"doc_name": "QMR Q3 2022.md"})
    assert json.loads(text)["label"] == "[qmr · 2022Q3]"
    assert "report_type" in agent_tools._local_schema("browse_documents")["properties"]
    base = agent_tools._base_instructions(client)
    assert "REPORT TYPES" in base and "- factbook:" in base and "report_type" in base
