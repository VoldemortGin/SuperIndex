"""`superindex batch` — run a question set through the `ask` chain.

Question files:
    .json   the scripts/questions.json layout ({"questions": [...]}) or a list
    .jsonl  one object per line ({"question", "expected"?, "doc"?, "id"?})
    .csv    a `question` column; optional `expected`, `doc`, `id`
    .txt    one question per line; `#` starts a comment

Every question is answered like `ask` (same client, `search_pages` included),
non-streamed, with a per-question timeout; one failure never stops the run.
Results go to `<out>/results.jsonl` (one record per question, appended as they
finish, so `--resume` can skip what is done) and `<out>/summary.md`.

`--retrieval-only` needs no LLM: each question text goes to the keyword search
(`superindex.bm25`, `--match`), and a top-k page counts as relevant when it is
one of the question's `pages` (e.g. ``[12, 13]`` or ``"12-13"``; `page` works
too), or else when its text holds the expected answer (same rule as the rough
score). The summary reports recall@1/3/5 and MRR.

With prefetch on (the default, `superindex.prefetch`), each question goes to
the agent behind its top keyword-search pages; the record keeps them
(`prefetch`, each judged like `--retrieval-only`) and `prefetch_hit` says
whether one of them holds the answer, so the summary can tell "search missed
it" from "found but not used". The rough score still reads only the answer.

A question without `doc` is routed (`route_scope`, `--no-route` to turn it
off): the years it names pick the documents whose stored metadata
(`md_ingest.extract_doc_meta`) has that period — interim reports only for a
first-half question, plus next year's reports unless `--no-route-adjacent` —
and the whole store is searched when it names no year or nothing matches; a
report type (`report_types` in the routing policy) narrows only when the
question names it. Records carry `routed_docs`, `route_reason`,
`route_fallback`, `route_note`, `route_report_types` and
(`route_diagnostics`) which read documents fell outside that range.

With `--page-image auto|always` (`superindex.page_images`) the record lists the
PDF page screenshots the question was given (`page_images`: document, page and
source auto/always/tool; `image_count`).
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

from superindex import bm25, image_chat, page_images, prefetch
from superindex.runtime import ConfigError, app_dir

RESULTS_FILE = "results.jsonl"
SUMMARY_FILE = "summary.md"
ALL_DOCS = {"", "ALL", "*"}
RECALL_AT = (1, 3, 5)


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
                 timeout: float, reasoning_effort: str | None = None,
                 message: str | None = None,
                 session: page_images.Session | None = None) -> dict[str, Any]:
    """Answer one question (sent as `message`, default the question text, with
    `session`'s page images); never raises. `llm_turns` is an estimate: one
    turn per batch of tool calls (ended by a tool result or text), plus the
    final answer turn."""
    answer: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    state: dict[str, Any] = {"turns": 0, "error": None}
    cancel = threading.Event()

    def consume() -> None:
        try:
            stream = image_chat.chat(client, message or q.question, doc_id=scope,
                                     reasoning_effort=reasoning_effort, session=session)
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
    record = {
        "id": q.id, "question": q.question, "doc": q.doc, "expected": q.expected,
        **q.extra,
        "scope": scope, "answer": text, "error": state["error"],
        "seconds": round(time.time() - t0, 2), "llm_turns": turns,
        "tool_calls": list(tool_calls), "pages_read": pages_read(tool_calls),
        "score": score(q.expected, text),
    }
    if session is not None:
        record["page_images"] = session.records()
        record["image_count"] = len(record["page_images"])
    return record


# ───────────────────────────────────────────────────────────── retrieval only
def gold_pages(q: Question) -> set[int] | None:
    """The question's relevant pages (`pages` or `page`: a number, a list, or
    text such as ``"12, 14-15"``), or None when it names none."""
    raw = q.extra.get("pages", q.extra.get("page"))
    if raw is None:
        return None
    items = raw if isinstance(raw, list) else re.split(r"[,;\s]+", str(raw))
    pages: set[int] = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        lo, _, hi = text.partition("-")
        try:
            pages.update(range(int(lo), int(hi or lo) + 1))
        except ValueError:
            raise ValueError(f"question {q.id}: bad page {item!r}") from None
    return pages or None


def judge_hits(store: Path, q: Question, hits: list[bm25.Hit],
               texts: dict[str, list[str]] | None = None) -> tuple[str | None, list[bool]]:
    """How `q` is judged ("pages", "expected" or None) and whether each hit is
    relevant: one of its `pages`, or else a page whose text holds the
    expected answer."""
    from superindex.engine.local_store import DocStore

    gold = gold_pages(q)
    judge = "pages" if gold is not None else ("expected" if q.expected else None)
    texts = {} if texts is None else texts
    relevant: list[bool] = []
    for h in hits:
        if gold is not None:
            relevant.append(h.page in gold)
        elif q.expected:
            if h.doc_id not in texts:
                texts[h.doc_id] = bm25._page_texts(DocStore(str(store)).get_pages(h.doc_id) or [])
            page = texts[h.doc_id][h.page - 1] if h.page <= len(texts[h.doc_id]) else ""
            relevant.append(bool((score(q.expected, bm25.plain_text(page)) or {}).get("hit")))
        else:
            relevant.append(False)
    return judge, relevant


def run_retrieval(store: Path, q: Question, scope: list[str] | None, *, top_k: int,
                  match: str | None = None, page_weight: float = bm25.PAGE_WEIGHT,
                  texts: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Search one question and rank its hits: `rank` is the position of the
    first relevant page (None if none in the top k)."""
    t0 = time.time()
    result = bm25.search(store, q.question, doc_ids=scope, top_k=top_k, match=match,
                         page_weight=page_weight)
    judge, flags = judge_hits(store, q, result.hits, texts)
    hits: list[dict[str, Any]] = []
    rank = None
    for i, (h, relevant) in enumerate(zip(result.hits, flags), start=1):
        if relevant and rank is None:
            rank = i
        hits.append({"doc_name": h.doc_name, "page": h.page, "score": round(h.score, 3),
                     "relevant": relevant, "snippet": h.snippet})
    return {"id": q.id, "question": q.question, "doc": q.doc, "expected": q.expected,
            **q.extra, "scope": scope, "match": result.match, "top_k": top_k,
            "judge": judge, "rank": rank if judge else None, "hits": hits, "error": None,
            "seconds": round(time.time() - t0, 3)}


def retrieval_metrics(records: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    """recall@k (k in 1/3/5 up to `top_k`, and `top_k`) and MRR over the
    questions that can be judged; an errored question counts as a miss."""
    judged = [r for r in records if r.get("judge")]
    n = len(judged)
    ranks = [r.get("rank") for r in judged]
    out: dict[str, Any] = {"questions": n}
    for k in sorted({k for k in RECALL_AT if k <= top_k} | {top_k}):
        out[f"recall@{k}"] = sum(1 for x in ranks if x and x <= k) / n if n else 0.0
    out["mrr"] = sum(1 / x for x in ranks if x) / n if n else 0.0
    return out


def write_retrieval_summary(records: list[dict[str, Any]], path: Path,
                            meta: dict[str, Any]) -> dict[str, Any]:
    top_k = int(meta.get("top_k") or 5)
    metrics = retrieval_metrics(records, top_k)
    names = [k for k in metrics if k != "questions"]
    lines = [
        f"# 纯检索评测 — {meta.get('started', '')}",
        "",
        f"- 题集：`{meta.get('questions', '')}`",
        f"- store：`{meta.get('store', '')}`",
        f"- 匹配模式（match）：`{meta.get('match', '')}`　top-k：{top_k}",
        (f"- 题数：{len(records)}　可判定：{metrics['questions']}"
         f"　错误：{sum(1 for r in records if r.get('error'))}"),
        "",
        "| " + " | ".join(names) + " |",
        "|" + "---|" * len(names),
        "| " + " | ".join(f"{metrics[k]:.3f}" for k in names) + " |",
        "",
        ("> 判定：题目给了 `pages`（或 `page`）时，命中页须是其中之一；否则页面文本须包含"
         "期望答案（规则同粗评分：期望中的数字全部出现，或无数字时整句包含）。"
         "名次 = 第一个相关页在结果中的位置，MRR 取其倒数（top-k 外记 0）。"),
        "",
        "| # | ID | 问题 | 判定 | 名次 | 结果页（✓ 相关） | 错误 |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(records, start=1):
        pages = ", ".join(f"{h['doc_name']}:{h['page']}{' ✓' if h.get('relevant') else ''}"
                          for h in r.get("hits") or [])
        lines.append(f"| {i} | {_cell(r.get('id'), 20)} | {_cell(r.get('question'), 60)} "
                     f"| {r.get('judge') or '-'} | {r.get('rank') or '-'} "
                     f"| {_cell(pages, 160)} | {_cell(r.get('error'), 60)} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return metrics


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


def _prefetch_mark(rec: dict[str, Any]) -> str:
    found = rec.get("prefetch_hit")
    return "-" if found is None else ("✓" if found else "✗")


def _prefetch_lines(records: list[dict[str, Any]], meta: dict[str, Any]) -> list[str]:
    if not any("prefetch" in r for r in records):
        return ["- 检索前置：关"]
    judged = [r for r in records if r.get("prefetch_hit") is not None and not r.get("error")]
    found = [r for r in judged if r["prefetch_hit"]]
    unused = sum(1 for r in found if r.get("score") and not r["score"]["hit"])
    missed = sum(1 for r in judged if not r["prefetch_hit"]
                 and r.get("score") and not r["score"]["hit"])
    return [(f"- 检索前置：开（k={meta.get('prefetch_k', '')}）　候选含答案页：{len(found)}/"
             f"{len(judged)}　未命中题中：候选含答案页 {unused} 题（找到了但没用好）、"
             f"不含 {missed} 题（检索没找到）")]


def write_summary(records: list[dict[str, Any]], path: Path, meta: dict[str, Any]) -> None:
    scored = [r for r in records if r.get("score") and not r.get("error")]
    hits = sum(1 for r in scored if r["score"]["hit"])
    errors = sum(1 for r in records if r.get("error"))
    secs = [float(r.get("seconds") or 0) for r in records]
    with_prefetch = any("prefetch" in r for r in records)
    with_images = any("image_count" in r for r in records)
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
        *_prefetch_lines(records, meta),
        *route_summary_lines(records),
        *([f"- 附图（{meta.get('page_image', '')}）：共 "
           f"{sum(int(r.get('image_count') or 0) for r in records)} 张"]
          if with_images else []),
        "",
        ("> 粗评分：期望答案中的每个数字都出现在回答里（1,814 与 1814、38.00 与 38 视为相同）"
         "即算命中；期望答案不含数字时按整句（忽略大小写）包含判断。仅供快速筛查，需人工复核。"
         + ("只看最终回答，不看注入的检索线索。「线索」列：检索前置候选页中是否有答案页"
            "（判定同纯检索评测：题目给了 `pages` 时按页码，否则按页面文本含期望答案）。"
            if with_prefetch else "")),
        "",
        "| # | ID | 问题 | 命中 | 耗时(s) | 轮次 | 读取页码 | " + ("线索 | " if with_prefetch else "")
        + ("附图数 | " if with_images else "") + "错误 |",
        "|---|---|---|---|---|---|---|" + ("---|" if with_prefetch else "")
        + ("---|" if with_images else "") + "---|",
    ]
    for i, r in enumerate(records, start=1):
        lines.append(f"| {i} | {_cell(r.get('id'), 20)} | {_cell(r.get('question'), 60)} "
                     f"| {_hit_mark(r)} | {float(r.get('seconds') or 0):.1f} "
                     f"| {r.get('llm_turns', '')} | {_cell(', '.join(r.get('pages_read') or []), 60)} "
                     + (f"| {_prefetch_mark(r)} " if with_prefetch else "")
                     + (f"| {r.get('image_count', 0)} " if with_images else "")
                     + f"| {_cell(r.get('error'), 60)} |")
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
        if r.get("prefetch"):
            pages = ", ".join(f"{c['doc_name']}:{c['page']}{' ✓' if c.get('relevant') else ''}"
                              for c in r["prefetch"])
            lines.append(f"**检索线索**：{pages}")
            lines.append("")
        if r.get("page_images"):
            pages = ", ".join(f"{p['doc_name']}:{p['page']}（{p['source']}）"
                              for p in r["page_images"])
            lines.append(f"**附图**：{pages}")
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


# ───────────────────────────────────────────────────────────── routing
ROUTE_FALLBACKS = {"no_period": "问题里没有年份/期间", "no_meta": "库内文档没有期间元数据",
                   "no_match": "没有期间匹配的文档"}
_H1_QUESTION_RE = re.compile(
    r"(?i)上半年|半年|中报|中期报告|中期业绩|中期(?!股息|派息)|(?<![a-z])(?:H1|1H)(?![a-z])"
    r"|interim(?!\s+dividend)|first half|half[- ]year|six months|6 months")
_FULL_YEAR_RE = re.compile(r"(?i)全年|年度报告|(?<!半)年报|full[- ]year|annual")
_ORG_SUFFIX_RE = re.compile(
    r"(?i)\b(?:group|holdings?|limited|ltd|co|inc|corp(?:oration)?|plc|company)\b\.?"
    r"|股份有限公司|有限公司|集团|控股|公司|[()（）,，.、]")
_GROUP_REGIONS = re.compile(r"(?i)集团|group|global|全球|consolidated|合并|全公司")


@dataclass
class Route:
    """Where one question searches: `scope_ids` (None = the whole store), why
    (`reason`: doc / off / routed / no_period / no_meta / no_match) and a
    printable `note`."""
    scope_ids: list[str] | None
    reason: str
    note: str
    docs: list[str] = field(default_factory=list)
    years: list[int] = field(default_factory=list)
    allowed_years: list[int] = field(default_factory=list)
    interim_only: bool = False
    report_types: list[str] = field(default_factory=list)   # types the question names
    allowed_types: list[str] = field(default_factory=list)  # ... and those that cover them

    @property
    def fallback(self) -> bool:
        return self.reason in ROUTE_FALLBACKS

    def fields(self) -> dict[str, Any]:
        """The routing fields of a results.jsonl record."""
        return {"routed_docs": self.docs, "route_reason": self.reason,
                "route_fallback": self.fallback, "route_note": self.note,
                "route_report_types": self.report_types}


def _routing_policy() -> Any:
    from superindex.nav.policy import PolicyError, RoutingPolicy

    try:
        return RoutingPolicy.load()
    except PolicyError as exc:
        print(f"routing policy ignored: {exc}", flush=True)
        return RoutingPolicy()


def question_periods(question: str, policy: Any = None) -> tuple[list[int], bool]:
    """Years the question names (`RoutingPolicy.periods_in`, plus ``FY24``,
    ``3Q24``, ``1H24`` …) and whether it asks about the first half / interim
    period only."""
    from superindex.md_ingest import _SHORT_YEAR_RE

    policy = _routing_policy() if policy is None else policy
    years = [int(p) for p in policy.periods_in(question) if p.isdigit() and len(p) == 4]
    years += [2000 + int(m.group(1)) for m in _SHORT_YEAR_RE.finditer(question)]
    interim = bool(_H1_QUESTION_RE.search(question)) and not _FULL_YEAR_RE.search(question)
    return sorted(set(years)), interim


def _doc_meta(doc: dict[str, Any]) -> dict[str, Any]:
    found = (doc.get("metadata") or {}).get("doc_meta")
    return found if isinstance(found, dict) else {}


def _mentioned(value: str | None, question: str, aliases: Any = ()) -> bool:
    """Whether `value` (a company or region; its core without Group / Limited
    / 有限公司 …, or a policy alias of it) appears in the question."""
    if not value:
        return False
    names = {value.lower(), " ".join(_ORG_SUFFIX_RE.sub(" ", value).split()).lower()}
    for key, variants in aliases:
        group = {g.lower() for g in (key, *variants)}
        if group & names:
            names |= group
    low = question.lower()
    for name in names:
        if len(name) < 2:
            continue
        if name.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", low):
                return True
        elif name in low:
            return True
    return False


def _narrow(cands: list[dict[str, Any]], key: str, question: str, aliases: Any,
            keep: Any = None) -> tuple[list[dict[str, Any]], str | None]:
    """Keep the documents whose `key` the question mentions (plus those with
    no value, or a value `keep` accepts) — only when the question mentions
    at least one; otherwise nothing is filtered."""
    hit = [d for d in cands if _mentioned(_doc_meta(d).get(key), question, aliases)]
    if not hit:
        return cands, None
    kept = [d for d in cands if d in hit or not _doc_meta(d).get(key)
            or (keep is not None and keep(_doc_meta(d).get(key)))]
    values = sorted({str(_doc_meta(d).get(key)) for d in hit})
    return kept, ", ".join(values)


def route_scope(docs: list[dict[str, Any]], question: str, doc: list[str] | None = None, *,
                enabled: bool = True, adjacent: bool = True, policy: Any = None) -> Route:
    """Pick the documents a question is searched in, from their stored
    metadata (`md_ingest.extract_doc_meta`).

    A question with `doc` keeps that scope (`_scope`). Otherwise the years it
    names select the reports of those years, plus, with `adjacent`, the next
    year's reports (they carry the comparatives); a document of unknown
    period is kept. Quarters and months never exclude a document (trend
    reports hold earlier periods). A first-half question keeps, of the
    annual / interim reports, only the interim ones — when there is one;
    other report types are not affected. Report type narrows only when the
    question names a configured type (`RoutingPolicy.report_types_in`: an
    abbreviation or full name); company and region only when the question
    mentions a stored value (a group-level region is always kept). No period
    in the question, no metadata or no match: the whole store (None)."""
    policy = _routing_policy() if policy is None and enabled else policy
    years, interim = question_periods(question, policy) if policy is not None else ([], False)
    allowed = sorted(set(years) | ({y + 1 for y in years} if adjacent else set()))
    named = policy.report_types_in(question) if policy is not None else []
    covering = sorted(policy.covering_types(named)) if named else []
    base = {"years": years, "allowed_years": allowed, "interim_only": interim,
            "report_types": named, "allowed_types": covering}
    if doc:
        ids = _scope(docs, doc)
        names = [d.get("name") or d["id"] for d in docs if d["id"] in ids]
        return Route(ids, "doc", f"题目指定文档：{', '.join(names)}", names, **base)
    if not enabled:
        return Route(None, "off", "路由关闭：全库", **base)
    if not years:
        return Route(None, "no_period", f"{ROUTE_FALLBACKS['no_period']}，全库", **base)
    if not any(_doc_meta(d).get("period") for d in docs):
        return Route(None, "no_meta", f"{ROUTE_FALLBACKS['no_meta']}，全库", **base)

    from superindex.md_ingest import REPORT_TYPES, period_year

    def pick(only_interim: bool) -> list[dict[str, Any]]:
        out = []
        for d in docs:
            meta = _doc_meta(d)
            year = period_year(meta.get("period"))
            kind = meta.get("report_type")
            if year is None or (year in allowed and (
                    not only_interim or kind == "interim" or kind not in REPORT_TYPES)):
                out.append(d)
        return out

    def known(cands: list[dict[str, Any]]) -> bool:
        return any(period_year(_doc_meta(d).get("period")) is not None for d in cands)

    cands = pick(interim)
    kinds = "年报+中报"
    if interim:
        has_interim = any(_doc_meta(d).get("report_type") == "interim" for d in cands)
        has_legacy = any(_doc_meta(d).get("report_type") in REPORT_TYPES for d in pick(False))
        if has_interim:
            kinds = "仅中报"
        elif has_legacy:
            cands, kinds = pick(False), "无对应中报，放宽为全部报告类型"
        else:
            cands, kinds = pick(False), "上半年（不按期间类型排除）"
    year_text = "/".join(map(str, years))
    if not known(cands):
        return Route(None, "no_match", f"期间 {year_text}：{ROUTE_FALLBACKS['no_match']}，全库",
                     **base)
    type_text = None
    if covering:
        hit = [d for d in cands if _doc_meta(d).get("report_type") in covering]
        if hit:
            cands = [d for d in cands if d in hit or not _doc_meta(d).get("report_type")]
            type_text = "/".join(named)
    aliases = getattr(policy, "aliases", ())
    cands, company = _narrow(cands, "company", question, aliases)
    cands, region = _narrow(cands, "region", question, aliases,
                            keep=lambda v: bool(_GROUP_REGIONS.search(v)))
    names = [d.get("name") or d["id"] for d in cands]
    note = (f"期间 {year_text}（{kinds}" + ("，含下一年" if adjacent else "") + "）"
            + (f"，类型 {type_text}" if type_text else "")
            + (f"，公司 {company}" if company else "") + (f"，地区 {region}" if region else "")
            + f" → {len(cands)} 份文档")
    return Route([d["id"] for d in cands], "routed", note, names, **base)


def route_diagnostics(record: dict[str, Any], route: Route,
                      docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Which documents the agent read pages from (`get_page_content`), and of
    those, which lie outside the route's scope or have a period year the
    question does not allow (and, when it names a report type, which are of
    another type: `read_type_mismatch`). `read_out_of_range` is None when
    neither can be judged (no scope and no year in the question). Names only,
    no content."""
    from superindex.md_ingest import period_year

    read: list[str] = []
    for call in record.get("tool_calls") or []:
        name = (call.get("arguments") or {}).get("doc_name") \
            if call.get("name") == "get_page_content" and isinstance(call.get("arguments"), dict) \
            else None
        if name and name not in read:
            read.append(str(name))
    by_name = {d.get("name"): d for d in docs}
    scope = set(route.scope_ids) if route.scope_ids is not None else None
    outside = [n for n in read if scope is not None and n in by_name
               and by_name[n]["id"] not in scope]
    mismatch = []
    for n in read:
        year = period_year(_doc_meta(by_name.get(n) or {}).get("period"))
        if route.years and year is not None and year not in route.allowed_years:
            mismatch.append(n)
    judged = scope is not None or bool(route.years)
    out = {"read_docs": read, "read_outside_route": outside, "read_year_mismatch": mismatch,
           "read_out_of_range": bool(outside or mismatch) if judged and read else
           (False if judged else None)}
    if route.allowed_types:     # the question named a report type
        out["read_type_mismatch"] = [
            n for n in read if n in by_name and _doc_meta(by_name[n]).get("report_type")
            and _doc_meta(by_name[n]).get("report_type") not in route.allowed_types]
    return out


def route_stats(records: list[dict[str, Any]]) -> dict[str, int]:
    """Counts over the records that carry routing fields."""
    routed = [r for r in records if "route_reason" in r]
    out_of_range = [r for r in routed if r.get("read_out_of_range")]
    return {
        "questions": len(routed),
        "routed": sum(1 for r in routed if r["route_reason"] == "routed"),
        "doc": sum(1 for r in routed if r["route_reason"] == "doc"),
        "off": sum(1 for r in routed if r["route_reason"] == "off"),
        "fallback": sum(1 for r in routed if r.get("route_fallback")),
        **{f"fallback_{k}": sum(1 for r in routed if r["route_reason"] == k)
           for k in ROUTE_FALLBACKS},
        "read_out_of_range": len(out_of_range),
        "read_out_of_range_wrong": sum(1 for r in out_of_range if not r.get("error")
                                       and r.get("score") and not r["score"]["hit"]),
    }


def route_summary_lines(records: list[dict[str, Any]]) -> list[str]:
    """Printable routing summary (counts only); [] without routing fields."""
    s = route_stats(records)
    if not s["questions"]:
        return []
    reasons = "、".join(f"{label} {s['fallback_' + k]}" for k, label in ROUTE_FALLBACKS.items())
    return [(f"- 期间路由：命中 {s['routed']}/{s['questions']} 题　回退全库 {s['fallback']} 题"
             f"（{reasons}）　题目指定文档 {s['doc']}　关闭 {s['off']}"),
            (f"- 读取范围：读到路由范围外/年份不符文档的题 {s['read_out_of_range']} 题，"
             f"其中粗评分判错 {s['read_out_of_range_wrong']} 题")]


def cmd_batch(args: argparse.Namespace) -> int:
    from superindex.cli import (
        _instructions,
        _page_image_mode,
        _prefetch_k,
        _settings,
        _store,
        make_client,
    )
    from superindex.engine.local_store import DocStore

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
    retrieval = bool(getattr(args, "retrieval_only", False))
    top_k = max(1, int(getattr(args, "top_k", 5) or 5))
    match = bm25.resolve_match(getattr(args, "match", None))
    prefetch_k = 0 if retrieval else _prefetch_k(args)
    image_mode = "off" if retrieval else _page_image_mode(args)
    route_on = getattr(args, "route", True) is not False
    adjacent = getattr(args, "route_adjacent", True) is not False
    policy = _routing_policy() if route_on else None
    client = None if retrieval else make_client(settings, store, instructions=_instructions(args))
    texts: dict[str, list[str]] = {}

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
    if retrieval:
        print(f"retrieval : only (no LLM)  match: {match}  top-k: {top_k}", flush=True)
    else:
        print(f"timeout   : {args.timeout:g}s  concurrency: {args.concurrency}  "
              f"prefetch: {prefetch_k or 'off'}  page images: {image_mode}", flush=True)
    print(f"routing   : {'on' if route_on else 'off'}"
          + (f"  next-year reports: {'on' if adjacent else 'off'}" if route_on else ""), flush=True)

    lock = threading.Lock()
    finished = [0]

    def run(q: Question) -> None:
        record: dict[str, Any]
        route: Route | None = None
        try:
            route = route_scope(docs, q.question, args.doc or q.doc, enabled=route_on,
                                adjacent=adjacent, policy=policy)
            scope_ids = route.scope_ids
            scope = scope_ids[0] if scope_ids and len(scope_ids) == 1 else scope_ids
            if retrieval:
                record = run_retrieval(store, q, scope_ids, top_k=top_k, match=match,
                                       texts=texts)
            else:
                hits = prefetch.search(store, q.question, scope_ids, prefetch_k)
                session = page_images.new_session(store, image_mode)
                if session is not None:
                    session.attach_prefetch(hits)
                record = run_question(client, q, scope, timeout=args.timeout,
                                      reasoning_effort=settings.reasoning_effort,
                                      message=prefetch.augment(q.question, hits),
                                      session=session)
                if prefetch_k:
                    judge, flags = judge_hits(store, q, hits, texts)
                    record["prefetch"] = [{"doc_name": h.doc_name, "page": h.page,
                                           "relevant": f} for h, f in zip(hits, flags)]
                    record["prefetch_hit"] = any(flags) if judge else None
            record.update(route.fields())
            if not retrieval:
                record.update(route_diagnostics(record, route, docs))
        except Exception as exc:  # noqa: BLE001 - e.g. an unknown doc: record it
            error = f"{type(exc).__name__}: {exc}"
            if retrieval:
                record = {"id": q.id, "question": q.question, "doc": q.doc,
                          "expected": q.expected, **q.extra, "scope": None, "match": match,
                          "top_k": top_k, "judge": "error", "rank": None, "hits": [],
                          "error": error, "seconds": 0.0}
            else:
                record = {"id": q.id, "question": q.question, "doc": q.doc,
                          "expected": q.expected, **q.extra, "scope": None, "answer": "",
                          "error": error, "seconds": 0.0,
                          "llm_turns": 0, "tool_calls": [], "pages_read": [],
                          "score": score(q.expected, "")}
        with lock:
            with (out_dir / RESULTS_FILE).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            finished[0] += 1
            if retrieval:
                status = "ERROR " + record["error"] if record["error"] else \
                    f"rank {record['rank'] or '-'}"
                pages = ", ".join(f"{h['doc_name']}:{h['page']}" for h in record["hits"])
                print(f"[{finished[0]}/{len(todo)}] {q.id}  {status}  pages: {pages or '-'}",
                      flush=True)
                return
            status = "ERROR " + record["error"] if record["error"] else _hit_mark(record)
            print(f"[{finished[0]}/{len(todo)}] {q.id}  {record['seconds']:.1f}s  "
                  f"{status}  pages: {', '.join(record['pages_read']) or '-'}", flush=True)
            if route is not None and route.reason != "doc":
                print(f"    route: {route.note}", flush=True)
            if record["answer"]:
                print(f"    {_cell(record['answer'], 160)}", flush=True)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        list(pool.map(run, todo))
    wall = time.time() - t0

    latest = read_results(out_dir)
    records = [latest[q.id] for q in questions if q.id in latest]
    if retrieval:
        metrics = write_retrieval_summary(records, out_dir / SUMMARY_FILE, {
            "started": started, "questions": str(qfile), "store": str(store),
            "match": match, "top_k": top_k})
        print(f"\n{metrics['questions']} judged question(s): "
              + "  ".join(f"{k} {v:.3f}" for k, v in metrics.items() if k != "questions"))
        for line in route_summary_lines(records)[:1]:
            print(line)
        print(f"summary   : {out_dir / SUMMARY_FILE}")
        return 0
    write_summary(records, out_dir / SUMMARY_FILE, {
        "started": started, "questions": str(qfile), "store": str(store),
        "chat_model": settings.chat_model, "wall_seconds": wall, "prefetch_k": prefetch_k,
        "page_image": image_mode,
    })
    errors = sum(1 for r in records if r.get("error"))
    print(f"\n{len(records)} question(s), {errors} error(s), {wall:.1f}s")
    for line in route_summary_lines(records):
        print(line)
    print(f"summary   : {out_dir / SUMMARY_FILE}")
    return 0
