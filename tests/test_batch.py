"""Offline tests for `superindex.batch` — question files, rough scoring,
the per-question runner and the results/summary output. No LLM: the client
is a fake whose chat stream replays canned events.

    pytest tests/test_batch.py
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import batch, cli  # noqa: E402
from superindex.md_ingest import index_markdown  # noqa: E402

PLAIN_SAMPLE = ROOT / "samples" / "aia_ar2021_excerpt.md"
DI_SAMPLE = ROOT / "samples" / "di_native_excerpt.md"


class FakeStream:
    def __init__(self, events: list[dict[str, Any]], block: threading.Event | None = None,
                 error: Exception | None = None) -> None:
        self._events, self._block, self._error = events, block, error

    @property
    def events(self) -> Any:
        def gen() -> Any:
            yield from self._events
            if self._block is not None:
                self._block.wait(5)
            if self._error is not None:
                raise self._error
        return gen()


class FakeClient:
    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, Any]] = []
        self.messages: list[str] = []

    def chat(self, message: str, doc_id: Any = None, stream: bool = False,
             reasoning_effort: str | None = None) -> FakeStream:
        self.messages.append(message)
        question = message.rsplit("问题：", 1)[-1]      # past a prefetch block
        self.calls.append((question, doc_id))
        spec = self.answers.get(question, "")
        if isinstance(spec, Exception):
            raise spec
        if isinstance(spec, FakeStream):
            return spec
        return FakeStream(_events(spec))


def _events(answer: str) -> list[dict[str, Any]]:
    return [
        {"type": "tool_call", "call_id": "1", "name": "search_pages",
         "arguments": json.dumps({"query": "dividend"})},
        {"type": "tool_result", "call_id": "1", "name": "search_pages", "output": "{}"},
        {"type": "tool_call", "call_id": "2", "name": "get_page_content",
         "arguments": {"doc_name": "aia_ar2021_excerpt.md", "pages": "1-2"}},
        {"type": "tool_result", "call_id": "2", "name": "get_page_content", "output": "{}"},
        {"type": "answer", "delta": answer[: len(answer) // 2]},
        {"type": "answer", "delta": answer[len(answer) // 2:]},
    ]


# ───────────────────────────────────────────────────────────── loading
def test_load_existing_questions_json() -> None:
    qs = batch.load_questions(ROOT / "scripts" / "questions.json")
    assert len(qs) == 20
    assert qs[0].id == "Q01" and qs[0].doc == ["AIA_Annual_Report_FY2025.pdf"]
    assert qs[0].expected and qs[0].extra.get("source_page") == 17
    assert all(q.question for q in qs)
    assert len(batch.load_questions(ROOT / "scripts" / "questions_3docs.json")) == 18


def test_load_sample_jsonl() -> None:
    qs = batch.load_questions(ROOT / "samples" / "questions_sample.jsonl")
    assert len(qs) >= 4 and all(q.expected and q.doc for q in qs)


def test_load_txt_csv_jsonl(tmp_path: Path) -> None:
    txt = tmp_path / "q.txt"
    txt.write_text("# comment\nFirst question?\n\n  Second question?  \n", encoding="utf-8")
    qs = batch.load_questions(txt)
    assert [q.question for q in qs] == ["First question?", "Second question?"]
    assert [q.id for q in qs] == ["Q001", "Q002"] and qs[0].expected is None

    csv_file = tmp_path / "q.csv"
    csv_file.write_text("\ufeffquestion,expected,doc\n\"A, b?\",42,x.md;ALL\nC?,,\n",
                        encoding="utf-8")
    qs = batch.load_questions(csv_file)
    assert qs[0].question == "A, b?" and qs[0].expected == "42" and qs[0].doc == ["x.md"]
    assert qs[1].expected is None and qs[1].doc == []

    jsonl = tmp_path / "q.jsonl"
    jsonl.write_text('{"id": "a", "question": "Q?", "expected": 7, "doc": ["d1", "d2"]}\n'
                     '"plain string question"\n', encoding="utf-8")
    qs = batch.load_questions(jsonl)
    assert (qs[0].id, qs[0].expected, qs[0].doc) == ("a", "7", ["d1", "d2"])
    assert qs[1].question == "plain string question"


def test_load_rejects_bad_files(tmp_path: Path) -> None:
    bad = tmp_path / "q.csv"
    bad.write_text("text\nhello\n", encoding="utf-8")
    with pytest.raises(ValueError, match="question"):
        batch.load_questions(bad)
    dup = tmp_path / "q.jsonl"
    dup.write_text('{"id": "x", "question": "a"}\n{"id": "x", "question": "b"}\n',
                   encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        batch.load_questions(dup)
    with pytest.raises(ValueError, match="unsupported"):
        batch.load_questions(tmp_path / "q.xlsx")


# ───────────────────────────────────────────────────────────── scoring
def test_score_numbers() -> None:
    sc = batch.score("US$1,814 million, up 22 per cent",
                     "VONB was 1814 million US dollars, 22% higher.")
    assert sc and sc["hit"] and sc["method"] == "numbers" and sc["total"] == 2
    sc = batch.score("38.00 Hong Kong cents, up 8.6 per cent", "38 HK cents, up 8.5%")
    assert sc and not sc["hit"] and sc["matched"] == 1 and sc["missing"] == ["8.6"]
    assert batch.score(None, "x") is None and batch.score("", "x") is None


def test_score_numbers_are_whole_tokens() -> None:
    sc = batch.score("10 per cent", "It grew 2010 and 110.")
    assert sc and not sc["hit"]


def test_score_substring_without_numbers() -> None:
    assert batch.score("Hong  Kong", "based in hong kong.")["hit"]  # type: ignore[index]
    assert not batch.score("Singapore", "Hong Kong")["hit"]  # type: ignore[index]


# ───────────────────────────────────────────────────────────── running
def test_run_question_collects_tools_pages_turns() -> None:
    q = batch.Question("Q1", "What dividend?", expected="108.00 cents")
    client = FakeClient({"What dividend?": "The final dividend was 108 HK cents."})
    rec = batch.run_question(client, q, "pi-1", timeout=5)
    assert rec["error"] is None
    assert rec["answer"] == "The final dividend was 108 HK cents."
    assert [c["name"] for c in rec["tool_calls"]] == ["search_pages", "get_page_content"]
    assert rec["tool_calls"][0]["arguments"] == {"query": "dividend"}   # JSON string parsed
    assert rec["pages_read"] == ["aia_ar2021_excerpt.md:1-2"]
    assert rec["llm_turns"] == 3
    assert rec["score"]["hit"] and rec["scope"] == "pi-1"
    assert client.calls == [("What dividend?", "pi-1")]


def test_run_question_records_errors() -> None:
    q = batch.Question("Q1", "boom")
    rec = batch.run_question(FakeClient({"boom": RuntimeError("no backend")}), q, None,
                             timeout=5)
    assert rec["error"] == "RuntimeError: no backend" and rec["answer"] == ""

    stream = FakeStream([{"type": "answer", "delta": "partial"}], error=ValueError("cut"))
    rec = batch.run_question(FakeClient({"boom": stream}), q, None, timeout=5)
    assert rec["error"] == "ValueError: cut" and rec["answer"] == "partial"


def test_run_question_times_out() -> None:
    release = threading.Event()
    stream = FakeStream([{"type": "answer", "delta": "slow"}], block=release)
    q = batch.Question("Q1", "slow?")
    rec = batch.run_question(FakeClient({"slow?": stream}), q, None, timeout=0.2)
    release.set()
    assert rec["error"] == "timeout after 0.2s" and rec["seconds"] < 3


# ───────────────────────────────────────────────────────────── command
def _args(questions: Path, store: Path, out: Path, **kw: Any) -> Any:
    argv = ["batch", str(questions), "--store", str(store), "--out", str(out),
            "--chat-model", "fake/model"]
    for key, value in kw.items():
        flag = "--" + key.replace("_", "-")
        argv += [flag] if value is True else [flag, str(value)]
    return cli.build_parser().parse_args(argv)


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    index_markdown(PLAIN_SAMPLE, path)
    index_markdown(DI_SAMPLE, path)
    return path


def test_cmd_batch_writes_results_and_summary(tmp_path: Path, store: Path,
                                              monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    qfile = tmp_path / "q.jsonl"
    qfile.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"id": "A", "question": "dividend?", "expected": "108.00", "doc": "aia_ar2021_excerpt"},
        {"id": "B", "question": "vonb?", "expected": "4,034", "doc": "di_native_excerpt.pdf"},
        {"id": "C", "question": "missing doc?", "doc": "no_such_doc"},
        {"id": "D", "question": "fails?", "expected": "1"},
    ]) + "\n", encoding="utf-8")
    fake = FakeClient({"dividend?": "108 HK cents", "vonb?": "It was 4034.",
                       "fails?": RuntimeError("down")})
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: fake)

    out = tmp_path / "out"
    assert batch.cmd_batch(_args(qfile, store, out, concurrency=2)) == 0

    lines = (out / batch.RESULTS_FILE).read_text(encoding="utf-8").splitlines()
    recs = {r["id"]: r for r in map(json.loads, lines)}
    assert set(recs) == {"A", "B", "C", "D"}
    assert recs["A"]["score"]["hit"] and recs["B"]["score"]["hit"]
    assert isinstance(recs["A"]["scope"], str) and recs["A"]["scope"].startswith("pi-")
    assert "no_such_doc" in recs["C"]["error"]
    assert recs["D"]["error"] == "RuntimeError: down" and recs["D"]["scope"] is None

    summary = (out / batch.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "命中率（粗评分）：2/2" in summary and "错误：2" in summary
    assert summary.index("| 1 | A |") < summary.index("| 4 | D |")
    assert "aia_ar2021_excerpt.md:1-2" in summary
    assert str(out / batch.SUMMARY_FILE) in capsys.readouterr().out

    # --resume re-runs only the failed questions; --doc overrides the file's doc
    fake.answers["fails?"] = "1"
    fake.calls.clear()
    args = _args(qfile, store, out, resume=True, doc="aia_ar2021")
    assert batch.cmd_batch(args) == 0
    assert sorted(q for q, _ in fake.calls) == ["fails?", "missing doc?"]
    latest = batch.read_results(out)
    assert latest["D"]["error"] is None and latest["C"]["error"] is None
    assert latest["C"]["scope"] == recs["A"]["scope"]


def test_cmd_batch_limit_and_empty_store(tmp_path: Path, store: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    qfile = tmp_path / "q.txt"
    qfile.write_text("one?\ntwo?\nthree?\n", encoding="utf-8")
    fake = FakeClient({})
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: fake)
    assert batch.cmd_batch(_args(qfile, store, tmp_path / "o", limit=2)) == 0
    assert [q for q, _ in fake.calls] == ["one?", "two?"]
    assert batch.cmd_batch(_args(qfile, tmp_path / "empty", tmp_path / "o2")) == 1


# ───────────────────────────────────────────────────────────── retrieval only
def test_gold_pages() -> None:
    def q(**extra: Any) -> batch.Question:
        return batch.Question("Q", "?", extra=extra)

    assert batch.gold_pages(q()) is None
    assert batch.gold_pages(q(page=3)) == {3}
    assert batch.gold_pages(q(pages=[2, "5"])) == {2, 5}
    assert batch.gold_pages(q(pages="1, 4-6")) == {1, 4, 5, 6}
    with pytest.raises(ValueError, match="bad page"):
        batch.gold_pages(q(pages="x"))


def test_retrieval_metrics() -> None:
    records = [{"judge": "expected", "rank": 1}, {"judge": "pages", "rank": 2},
               {"judge": "expected", "rank": 4}, {"judge": "error", "rank": None},
               {"judge": None, "rank": None}]                 # not judged: left out
    m = batch.retrieval_metrics(records, top_k=5)
    assert m["questions"] == 4
    assert (m["recall@1"], m["recall@3"], m["recall@5"]) == (0.25, 0.5, 0.75)
    assert m["mrr"] == pytest.approx((1 + 1 / 2 + 1 / 4) / 4)
    assert set(batch.retrieval_metrics(records, top_k=3)) == {"questions", "recall@1",
                                                              "recall@3", "mrr"}
    assert batch.retrieval_metrics([], 5)["mrr"] == 0.0


def test_run_retrieval_judges_by_expected_or_pages(store: Path) -> None:
    from pageindex.local_store import DocStore

    docs = DocStore(str(store)).list_metas()
    scope = batch._scope(docs, ["di_native_excerpt"])
    q = batch.Question("A", "末期股息", expected="113.75 港仙")
    rec = batch.run_retrieval(store, q, scope, top_k=3)
    assert rec["judge"] == "expected" and rec["rank"] == 1 and rec["hits"][0]["relevant"]
    assert rec["match"] == "page" and rec["hits"][0]["page"] == 2

    q = batch.Question("B", "末期股息", expected="113.75", extra={"pages": [4]})
    rec = batch.run_retrieval(store, q, scope, top_k=3, match="passage")
    assert rec["judge"] == "pages" and rec["match"] == "passage"
    assert rec["rank"] == next((i for i, h in enumerate(rec["hits"], 1) if h["page"] == 4), None)

    rec = batch.run_retrieval(store, batch.Question("C", "末期股息"), scope, top_k=3)
    assert rec["judge"] is None and rec["rank"] is None


def test_cmd_batch_retrieval_only(tmp_path: Path, store: Path,
                                  monkeypatch: pytest.MonkeyPatch,
                                  capsys: pytest.CaptureFixture[str]) -> None:
    qfile = tmp_path / "q.jsonl"
    qfile.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"id": "A", "question": "final dividend", "expected": "108.00",
         "doc": "aia_ar2021_excerpt"},
        {"id": "B", "question": "末期股息", "expected": "113.75", "doc": "di_native_excerpt"},
        {"id": "C", "question": "zzzqqq", "expected": "1"},
        {"id": "D", "question": "missing doc?", "expected": "1", "doc": "no_such_doc"},
    ]) + "\n", encoding="utf-8")

    def no_llm(*a: Any, **k: Any) -> None:
        raise AssertionError("retrieval-only must not build an LLM client")

    monkeypatch.setattr(cli, "make_client", no_llm)
    out = tmp_path / "out"
    args = _args(qfile, store, out, retrieval_only=True, top_k=3, match="passage")
    assert batch.cmd_batch(args) == 0
    recs = batch.read_results(out)
    assert recs["A"]["rank"] == 1 and recs["B"]["rank"] == 1 and recs["C"]["rank"] is None
    assert recs["A"]["match"] == "passage" and recs["A"]["top_k"] == 3
    assert "no_such_doc" in recs["D"]["error"] and recs["D"]["judge"] == "error"
    summary = (out / batch.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "纯检索评测" in summary and "`passage`" in summary
    assert "| recall@1 | recall@3 | mrr |" in summary and "| 0.500 | 0.500 | 0.500 |" in summary
    assert "recall@1 0.500" in capsys.readouterr().out
