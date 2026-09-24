"""`python -m superindex batch` — run a question set through the `ask` chain.

Question files:
    .json   the scripts/questions.json layout ({"questions": [...]}) or a list
    .jsonl  one object per line ({"question", "expected"?, "doc"?, "id"?})
    .csv    a `question` column; optional `expected`, `doc`, `id`
    .txt    one question per line; `#` starts a comment

Every question is answered like `ask` (same client, `search_pages` included),
non-streamed, with a per-question timeout; one failure never stops the run.
Results go to `<out>/results.jsonl` (one record per question, appended as they
finish, so `--resume` can skip what is done) and `<out>/summary.md`.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from superindex.runtime import ConfigError, app_dir

RESULTS_FILE = "results.jsonl"
SUMMARY_FILE = "summary.md"
ALL_DOCS = {"", "ALL", "*"}


@dataclass
class Question:
    id: str
    question: str
    expected: str | None = None
    doc: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


# ───────────────────────────────────────────────────────────── loading
def _doc_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r"[;|]", str(value))
    return [str(v).strip() for v in items if str(v).strip() not in ALL_DOCS]


def _from_item(item: Any, index: int) -> Question | None:
    if isinstance(item, str):
        item = {"question": item}
    if not isinstance(item, dict):
        raise TypeError(f"question #{index}: expected an object or a string, got {item!r}")
    text = str(item.get("question") or "").strip()
    if not text:
        return None
    expected = item.get("expected")
    extra = {k: v for k, v in item.items() if k not in ("id", "question", "expected", "doc")}
    return Question(id=str(item.get("id") or f"Q{index:03d}"), question=text,
                    expected=(str(expected).strip() or None) if expected is not None else None,
                    doc=_doc_list(item.get("doc")), extra=extra)


def load_questions(path: Path) -> list[Question]:
    """Read a question set; see the module docstring for the formats."""
    suffix = path.suffix.lower()
    if suffix not in (".json", ".jsonl", ".csv", ".txt", ".md", ""):
        raise ValueError(f"unsupported question file type: {path.suffix} "
                         "(use .json, .jsonl, .csv or .txt)")
    text = path.read_text(encoding="utf-8-sig")
    items: list[Any]
    if suffix == ".json":
        data = json.loads(text)
        items = data.get("questions", []) if isinstance(data, dict) else data
    elif suffix == ".jsonl":
        items = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif suffix == ".csv":
        rows = list(csv.DictReader(text.splitlines()))
        if rows and "question" not in rows[0]:
            raise ValueError(f"{path.name}: CSV needs a `question` column")
        items = [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in rows]
    else:
        items = [line.strip() for line in text.splitlines()
                 if line.strip() and not line.lstrip().startswith("#")]
    questions = [q for i, item in enumerate(items, start=1) if (q := _from_item(item, i))]
    seen: set[str] = set()
    for q in questions:
        if q.id in seen:
            raise ValueError(f"{path.name}: duplicate question id {q.id!r}")
        seen.add(q.id)
    return questions


# ───────────────────────────────────────────────────────────── scoring
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[Decimal]:
    out = []
    for raw in _NUMBER.findall(text):
        try:
            out.append(Decimal(raw.replace(",", "")).normalize())
        except InvalidOperation:
            continue
    return out


def score(expected: str | None, answer: str | None) -> dict[str, Any] | None:
    """Rough score: every number in `expected` must appear in the answer
    (1,814 == 1814, 38.00 == 38); without numbers, `expected` must appear
    verbatim (case-insensitive). None when there is nothing to compare."""
    if not expected:
        return None
    answer = answer or ""
    wanted = list(dict.fromkeys(_numbers(expected)))
    if wanted:
        present = set(_numbers(answer))
        matched = [str(n) for n in wanted if n in present]
        return {"method": "numbers", "hit": len(matched) == len(wanted),
                "matched": len(matched), "total": len(wanted),
                "missing": [str(n) for n in wanted if n not in present]}
    norm = " ".join(expected.split()).casefold()
    hit = norm in " ".join(answer.split()).casefold()
    return {"method": "substring", "hit": hit, "matched": int(hit), "total": 1,
            "missing": [] if hit else [expected]}


# ───────────────────────────────────────────────────────────── running
def pages_read(tool_calls: list[dict[str, Any]]) -> list[str]:
    """`doc:pages` for every get_page_content call, in call order."""
    out = []
    for call in tool_calls:
        if call.get("name") != "get_page_content":
            continue
        args = call.get("arguments") or {}
        out.append(f"{args.get('doc_name', '?')}:{args.get('pages', '?')}")
    return out


def _arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def run_question(client: Any, q: Question, scope: str | list[str] | None, *,
                 timeout: float, reasoning_effort: str | None = None) -> dict[str, Any]:
    """Answer one question; never raises. `llm_turns` is an estimate: one turn
    per batch of tool calls (ended by a tool result or text), plus the final
    answer turn."""
    answer: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    state: dict[str, Any] = {"turns": 0, "error": None}
    cancel = threading.Event()

    def consume() -> None:
        try:
            stream = client.chat(q.question, doc_id=scope, stream=True,
                                 reasoning_effort=reasoning_effort)
            in_tools = False
            for ev in stream.events:
                if cancel.is_set():
                    break           # dropping the stream stops the agent run
                etype = ev.get("type")
                if etype == "tool_call":
                    if not in_tools:
                        state["turns"] += 1
                    in_tools = True
                    tool_calls.append({"name": ev.get("name"),
                                       "arguments": _arguments(ev.get("arguments"))})
                else:               # a result or text ends that turn's tool calls
                    in_tools = False
                    if etype == "answer":
                        answer.append(ev.get("delta") or "")
        except Exception as exc:  # noqa: BLE001 - recorded, the batch goes on
            state["error"] = f"{type(exc).__name__}: {exc}"

    t0 = time.time()
    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        cancel.set()
        state["error"] = f"timeout after {timeout:g}s"
    text = "".join(answer).strip()
    turns = state["turns"] + (1 if text else 0)
    return {
        "id": q.id, "question": q.question, "doc": q.doc, "expected": q.expected,
        **q.extra,
        "scope": scope, "answer": text, "error": state["error"],
        "seconds": round(time.time() - t0, 2), "llm_turns": turns,
        "tool_calls": list(tool_calls), "pages_read": pages_read(tool_calls),
        "score": score(q.expected, text),
    }


# ───────────────────────────────────────────────────────────── output
def read_results(out_dir: Path) -> dict[str, dict[str, Any]]:
    """The last record per question id in `results.jsonl`."""
    path = out_dir / RESULTS_FILE
    records: dict[str, dict[str, Any]] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                records[str(rec.get("id"))] = rec
    return records


def _cell(text: Any, limit: int = 80) -> str:
    s = " ".join(str(text or "").split()).replace("|", "\\|")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _hit_mark(rec: dict[str, Any]) -> str:
    sc = rec.get("score")
    if rec.get("error"):
        return "ERR"
    if not sc:
        return "-"
    return f"{'✓' if sc['hit'] else '✗'} {sc['matched']}/{sc['total']}"


def write_summary(records: list[dict[str, Any]], path: Path, meta: dict[str, Any]) -> None:
    scored = [r for r in records if r.get("score") and not r.get("error")]
    hits = sum(1 for r in scored if r["score"]["hit"])
    errors = sum(1 for r in records if r.get("error"))
    secs = [float(r.get("seconds") or 0) for r in records]
    lines = [
        f"# 批量问答结果 — {meta.get('started', '')}",
        "",
        f"- 题集：`{meta.get('questions', '')}`",
        f"- 模型：`{meta.get('chat_model', '')}`　store：`{meta.get('store', '')}`",
        f"- 题数：{len(records)}　错误：{errors}",
        f"- 命中率（粗评分）：{hits}/{len(scored)}"
        + (f"（{hits / len(scored):.0%}）" if scored else ""),
        (f"- 单题耗时合计：{sum(secs):.1f}s　平均：{(sum(secs) / len(secs) if secs else 0):.1f}s"
         f"　本次运行墙钟：{meta.get('wall_seconds', 0):.1f}s"),
        "",
        ("> 粗评分：期望答案中的每个数字都出现在回答里（1,814 与 1814、38.00 与 38 视为相同）"
         "即算命中；期望答案不含数字时按整句（忽略大小写）包含判断。仅供快速筛查，需人工复核。"),
        "",
        "| # | ID | 问题 | 命中 | 耗时(s) | 轮次 | 读取页码 | 错误 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(records, start=1):
        lines.append(f"| {i} | {_cell(r.get('id'), 20)} | {_cell(r.get('question'), 60)} "
                     f"| {_hit_mark(r)} | {float(r.get('seconds') or 0):.1f} "
                     f"| {r.get('llm_turns', '')} | {_cell(', '.join(r.get('pages_read') or []), 60)} "
                     f"| {_cell(r.get('error'), 60)} |")
    lines += ["", "## 逐题详情", ""]
    for r in records:
        lines.append(f"### {r.get('id')} {_hit_mark(r)}")
        lines.append("")
        lines.append(f"**问题**：{r.get('question')}")
        lines.append("")
        if r.get("expected"):
            lines.append(f"**期望**：{r['expected']}")
            lines.append("")
        if r.get("error"):
            lines.append(f"**错误**：{r['error']}")
            lines.append("")
        lines.append("**回答**：")
        lines.append("")
        lines.append(r.get("answer") or "（空）")
        lines.append("")
        calls = [f"`{c.get('name')}` {json.dumps(c.get('arguments'), ensure_ascii=False)}"
                 for c in r.get("tool_calls") or []]
        if calls:
            lines.append("<details><summary>工具调用</summary>")
            lines.append("")
            lines += [f"{n}. {c}" for n, c in enumerate(calls, start=1)]
            lines.append("")
            lines.append("</details>")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ───────────────────────────────────────────────────────────── command
def _batch_root() -> Path:
    return app_dir() / "results" / "batch"


def _out_dir(args: argparse.Namespace) -> Path:
    if args.out:
        return Path(args.out).expanduser()
    root = _batch_root()
    if args.resume:
        runs = sorted(p for p in root.glob("*") if (p / RESULTS_FILE).is_file()) \
            if root.is_dir() else []
        if runs:
            return runs[-1]
    return root / datetime.now().strftime("%Y%m%d-%H%M%S")


def _scope(docs: list[dict[str, Any]], wanted: list[str]) -> list[str]:
    from superindex.cli import _resolve_docs

    ids: list[str] = []
    for w in wanted:
        try:
            found = _resolve_docs(docs, [w])
        except ConfigError:
            stem = Path(w).stem          # questions.json names PDFs; the store has .md
            if stem == w:
                raise
            found = _resolve_docs(docs, [stem])
        ids.extend(i for i in found if i not in ids)
    return ids


def cmd_batch(args: argparse.Namespace) -> int:
    from pageindex.local_store import DocStore

    from superindex.cli import _settings, _store, make_client

    qfile = Path(args.questions).expanduser()
    questions = load_questions(qfile)
    if args.limit:
        questions = questions[: args.limit]
    if not questions:
        print(f"no questions in {qfile}")
        return 1

    settings = _settings(args)
    store = _store(args)
    docs = [m for m in DocStore(str(store)).list_metas() if m.get("status") == "completed"]
    if not docs:
        print(f"no documents in {store} — run `index` first")
        return 1
    client = make_client(settings, store, instructions=args.instructions)

    out_dir = _out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    done = {k: r for k, r in read_results(out_dir).items() if not r.get("error")} \
        if args.resume else {}
    if not args.resume:
        (out_dir / RESULTS_FILE).write_text("", encoding="utf-8")
    todo = [q for q in questions if q.id not in done]

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"store     : {store}")
    print(f"questions : {qfile} ({len(questions)}; {len(done)} done, {len(todo)} to run)")
    print(f"out       : {out_dir}")
    print(f"timeout   : {args.timeout:g}s  concurrency: {args.concurrency}", flush=True)

    lock = threading.Lock()
    finished = [0]

    def run(q: Question) -> None:
        record: dict[str, Any]
        try:
            scope_ids = _scope(docs, args.doc or q.doc) if (args.doc or q.doc) else None
            scope = scope_ids[0] if scope_ids and len(scope_ids) == 1 else scope_ids
            record = run_question(client, q, scope, timeout=args.timeout,
                                  reasoning_effort=settings.reasoning_effort)
        except Exception as exc:  # noqa: BLE001 - e.g. an unknown doc: record it
            record = {"id": q.id, "question": q.question, "doc": q.doc,
                      "expected": q.expected, **q.extra, "scope": None, "answer": "",
                      "error": f"{type(exc).__name__}: {exc}", "seconds": 0.0,
                      "llm_turns": 0, "tool_calls": [], "pages_read": [],
                      "score": score(q.expected, "")}
        with lock:
            with (out_dir / RESULTS_FILE).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            finished[0] += 1
            status = "ERROR " + record["error"] if record["error"] else _hit_mark(record)
            print(f"[{finished[0]}/{len(todo)}] {q.id}  {record['seconds']:.1f}s  "
                  f"{status}  pages: {', '.join(record['pages_read']) or '-'}", flush=True)
            if record["answer"]:
                print(f"    {_cell(record['answer'], 160)}", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        list(pool.map(run, todo))
    wall = time.time() - t0

    latest = read_results(out_dir)
    records = [latest[q.id] for q in questions if q.id in latest]
    write_summary(records, out_dir / SUMMARY_FILE, {
        "started": started, "questions": str(qfile), "store": str(store),
        "chat_model": settings.chat_model, "wall_seconds": wall,
    })
    errors = sum(1 for r in records if r.get("error"))
    print(f"\n{len(records)} question(s), {errors} error(s), {wall:.1f}s")
    print(f"summary   : {out_dir / SUMMARY_FILE}")
    return 0
