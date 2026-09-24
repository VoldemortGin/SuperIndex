"""Offline tests for `superindex.prefetch` — keyword-search candidates put in
front of the question for ask / serve / batch. No LLM: fake clients record
the message they are sent.

    pytest tests/test_prefetch.py
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import batch, cli, prefetch  # noqa: E402
from superindex.md_ingest import index_markdown  # noqa: E402
from superindex.runtime import ConfigError  # noqa: E402

DI_SAMPLE = ROOT / "samples" / "di_native_excerpt.md"
PLAIN_SAMPLE = ROOT / "samples" / "aia_ar2021_excerpt.md"


@pytest.fixture()
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store"
    index_markdown(PLAIN_SAMPLE, path)
    index_markdown(DI_SAMPLE, path)
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (prefetch.ENV, prefetch.K_ENV):
        monkeypatch.delenv(name, raising=False)


def ids_by_name(store: Path) -> dict[str, str]:
    from superindex.engine.local_store import DocStore
    return {m["name"]: m["id"] for m in DocStore(str(store)).list_metas()}


class FakeStream:
    events = ({"type": "tool_call", "name": "calculate",
               "arguments": {"expression": "1 + 1"}},
              {"type": "tool_result", "name": "calculate", "output": "{}"},
              {"type": "answer", "delta": "113.75 HK cents"})


class FakeClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def chat(self, message: str, **_: Any) -> FakeStream:
        self.messages.append(message)
        return FakeStream()

    def list_documents(self, limit: int = 100) -> dict[str, Any]:
        return {"documents": [{"id": "x", "name": "x"}]}


# ───────────────────────────────────────────────────────────── settings
def test_resolve_k_flags_and_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert prefetch.resolve_k() == 5
    assert prefetch.resolve_k(k=3) == 3 and prefetch.resolve_k(k=99) == prefetch.MAX_K
    assert prefetch.resolve_k(False, 3) == 0
    monkeypatch.setenv(prefetch.K_ENV, "8")
    assert prefetch.resolve_k() == 8 and prefetch.resolve_k(k=2) == 2
    for off in ("0", "false", "OFF", "no"):
        monkeypatch.setenv(prefetch.ENV, off)
        assert prefetch.resolve_k() == 0
    assert prefetch.resolve_k(True) == 8            # --prefetch beats the environment
    monkeypatch.setenv(prefetch.ENV, "1")
    monkeypatch.setenv(prefetch.K_ENV, "many")
    with pytest.raises(ConfigError):
        prefetch.resolve_k()
    with pytest.raises(ConfigError):
        prefetch.resolve_k(k=-1)


def test_cli_flags() -> None:
    for command in (["ask", "q"], ["serve"], ["batch", "q.jsonl"]):
        args = cli.build_parser().parse_args(command)
        assert args.prefetch is None and args.prefetch_k is None
        args = cli.build_parser().parse_args([*command, "--no-prefetch", "--prefetch-k", "3"])
        assert args.prefetch is False and cli._prefetch_k(args) == 0
        args = cli.build_parser().parse_args([*command, "--prefetch", "--prefetch-k", "3"])
        assert cli._prefetch_k(args) == 3


# ───────────────────────────────────────────────────────────── the block
def test_block_format(store: Path) -> None:
    message, hits = prefetch.prepare(store, "末期股息", None, 3)
    assert hits and len(hits) <= 3
    head, question = message.split("\n\n问题：")
    assert question == "末期股息"
    lines = head.splitlines()
    assert lines[0] == prefetch.HEADER and lines[1] == prefetch.NOTE
    assert lines[-1] == prefetch.FOOTER
    assert lines[2] == "1. di_native_excerpt.md 第 2 页（PageNumber 2） — 章节：2023 年度报告 > 主席报告"
    assert "**末期股息**" in lines[3]
    for c in prefetch.candidates(hits):
        assert len(c["snippet"].replace("**", "")) <= prefetch.SNIPPET_CHARS + 2


def test_whitelist_and_no_hit(store: Path) -> None:
    ids = ids_by_name(store)
    hits = prefetch.search(store, "dividend", ids["aia_ar2021_excerpt.md"], 5)
    assert hits and {h.doc_name for h in hits} == {"aia_ar2021_excerpt.md"}
    hits = prefetch.search(store, "dividend", [ids["di_native_excerpt.md"]], 5)
    assert hits and {h.doc_name for h in hits} == {"di_native_excerpt.md"}
    assert prefetch.prepare(store, "zzzqqq", None, 5) == ("zzzqqq", [])
    assert prefetch.prepare(store, "dividend", None, 0) == ("dividend", [])


# ───────────────────────────────────────────────────────────── ask / serve
def test_ask_sends_the_block_and_prints_it(store: Path, monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    fake = FakeClient()
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: fake)
    base = ["ask", "末期股息", "--store", str(store), "--chat-model", "fake/model"]
    assert cli.cmd_ask(cli.build_parser().parse_args([*base, "-v"])) == 0
    assert fake.messages[-1].startswith(prefetch.HEADER)
    err = capsys.readouterr().err
    assert "[prefetch] " + prefetch.HEADER in err and "[tool] calculate" in err
    assert cli.cmd_ask(cli.build_parser().parse_args([*base, "--no-prefetch"])) == 0
    assert fake.messages[-1] == "末期股息"


def test_web_answer_emits_prefetch_event(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from superindex.webapp import server

    fake = FakeClient()
    monkeypatch.setattr(server, "get_client", lambda: fake)
    monkeypatch.setattr(server, "STORE", store)
    monkeypatch.setattr(server, "PREFETCH_K", 3)
    handler = server.Handler.__new__(server.Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a: None             # type: ignore[method-assign]
    handler.send_header = lambda *a: None               # type: ignore[method-assign]
    handler.end_headers = lambda: None                  # type: ignore[method-assign]
    handler.stream_answer("末期股息", [ids_by_name(store)["di_native_excerpt.md"]])
    body = handler.wfile.getvalue().decode("utf-8")
    events = [chunk.split("\n")[0] for chunk in body.strip().split("\n\n")]
    assert events[0] == "event: prefetch" and events[-1] == "event: done"
    data = json.loads(body.split("\n\n")[0].split("data: ", 1)[1])
    assert {c["doc_name"] for c in data["candidates"]} == {"di_native_excerpt.md"}
    assert fake.messages[-1].startswith(prefetch.HEADER)


# ───────────────────────────────────────────────────────────── batch
def test_batch_records_candidates(tmp_path: Path, store: Path,
                                  monkeypatch: pytest.MonkeyPatch) -> None:
    qfile = tmp_path / "q.jsonl"
    qfile.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
        {"id": "A", "question": "末期股息", "expected": "113.75"},
        {"id": "B", "question": "末期股息", "expected": "2023", "pages": [9]},
        {"id": "C", "question": "zzzqqq"},
    ]) + "\n", encoding="utf-8")
    fake = FakeClient()
    monkeypatch.setattr(cli, "make_client", lambda *a, **k: fake)
    out = tmp_path / "out"
    args = cli.build_parser().parse_args(["batch", str(qfile), "--store", str(store),
                                          "--out", str(out), "--chat-model", "fake/model",
                                          "--prefetch-k", "3"])
    assert batch.cmd_batch(args) == 0
    recs = batch.read_results(out)
    assert recs["A"]["prefetch"][0] == {"doc_name": "di_native_excerpt.md", "page": 2,
                                        "relevant": True}
    assert recs["A"]["prefetch_hit"] is True and recs["A"]["score"]["hit"]
    assert recs["B"]["prefetch_hit"] is False and not recs["B"]["score"]["hit"]
    assert recs["C"]["prefetch"] == [] and recs["C"]["prefetch_hit"] is None
    assert [c["name"] for c in recs["A"]["tool_calls"]] == ["calculate"]
    # the score reads the answer only: "2023" in the block sent for B never counts
    assert prefetch.HEADER in fake.messages[1] and "2023" in fake.messages[1]
    summary = (out / batch.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "检索前置：开（k=3）　候选含答案页：1/2" in summary
    assert "候选含答案页 0 题（找到了但没用好）、不含 1 题（检索没找到）" in summary
    assert "| 线索 |" in summary and "**检索线索**：di_native_excerpt.md:2 ✓" in summary

    args = cli.build_parser().parse_args(["batch", str(qfile), "--store", str(store),
                                          "--out", str(tmp_path / "off"), "--chat-model",
                                          "fake/model", "--no-prefetch"])
    assert batch.cmd_batch(args) == 0
    assert "prefetch" not in batch.read_results(tmp_path / "off")["A"]
    summary = (tmp_path / "off" / batch.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "检索前置：关" in summary and "| 线索 |" not in summary
    assert fake.messages[-1] == "zzzqqq"
