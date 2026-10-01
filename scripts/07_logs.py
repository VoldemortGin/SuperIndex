#!/usr/bin/env python3
"""
Inspect the structured debug logs written by superindex/nav/debuglog.py.

    uv run python scripts/07_logs.py                      # recent queries, one line each
    uv run python scripts/07_logs.py --kind errors        # recent exceptions, with tracebacks
    uv run python scripts/07_logs.py --failed             # only queries that failed or found nothing
    uv run python scripts/07_logs.py --id q-1a2b3c4d      # full record + any exception for that query
    uv run python scripts/07_logs.py --stats              # counts and the last error

Everything reads results/logs/*.jsonl (SUPERINDEX_LOG_DIR), which
`superindex nav-serve` and superindex.nav write as they run. Nothing here
needs the server to be up.
"""
from __future__ import annotations

import argparse
import json
import sys

from superindex.nav import debuglog


def fmt_ms(ms) -> str:
    return f"{ms/1000:.1f}s" if isinstance(ms, (int, float)) and ms else "—"


def show_query_line(rec: dict) -> None:
    mark = "✅" if rec.get("ok") else "❌"
    q = (rec.get("question") or "").replace("\n", " ")[:52]
    steps = len(rec.get("steps") or [])
    secs = len(rec.get("sections") or [])
    print(f"  {mark} {rec.get('id','?'):<12} {rec.get('ts','')[:19]}  "
          f"{fmt_ms(rec.get('ms')):>6}  {steps}步 {secs}源  {q}")
    if not rec.get("ok") and rec.get("error"):
        print(f"       └─ {rec['error']}")


def show_query_detail(rec: dict) -> None:
    print(f"\n{'='*74}")
    print(f"  查询 {rec.get('id')}   {rec.get('ts')}")
    print(f"{'='*74}")
    print(f"  问题   : {rec.get('question')}")
    print(f"  范围   : {', '.join(rec.get('scope') or []) or '(全部)'}")
    print(f"  模型   : {rec.get('model')}")
    print(f"  结果   : {'成功' if rec.get('ok') else '失败'}   {fmt_ms(rec.get('ms'))}")
    # Which business routing policy was in effect. An empty source means no
    # policy file was found, which is the answer to "why did my weight not
    # change anything?" more often than a wrong weight is.
    pol = rec.get("policy") or {}
    if pol:
        where = pol.get("source") or "(未找到策略文件，使用内置默认)"
        print(f"  策略   : {where}")
        if pol.get("describe"):
            print(f"           {pol['describe']}")
    if rec.get("error"):
        print(f"  错误   : {rec['error']}")

    stages = rec.get("stages") or {}
    if stages:
        print(f"  耗时   : " + "  ".join(f"{k}={fmt_ms(v)}" for k, v in stages.items()))

    files = rec.get("files") or []
    if files:
        print(f"\n  定位到 {len(files)} 个文件:")
        for f in files:
            print(f"    {f}")

    steps = rec.get("steps") or []
    if steps:
        print(f"\n  检索路径 {len(steps)} 步:")
        for st in steps:
            picked = ", ".join(st.get("picked") or []) or "—"
            note = f"  ({st['note']})" if st.get("note") else ""
            print(f"    [{st.get('level','?'):<7}] {st.get('where','')}")
            print(f"              → {picked}{note}")

    secs = rec.get("sections") or []
    if secs:
        print(f"\n  引用 {len(secs)} 个章节:")
        for s in secs:
            print(f"    {s.get('file')} § {s.get('title')} "
                  f"({s.get('start')}-{s.get('end')})")

    ans = rec.get("answer") or ""
    if ans:
        print(f"\n  回答（{rec.get('answer_chars', len(ans))} 字符）:")
        for line in ans[:1200].splitlines():
            print(f"    {line}")
        if len(ans) > 1200:
            print(f"    … 还有 {len(ans)-1200} 字符")


def show_error(rec: dict, *, full: bool = False) -> None:
    print(f"\n  ❌ {rec.get('id','?'):<12} {rec.get('ts','')[:19]}  "
          f"{rec.get('type')} @ {rec.get('where')}")
    # Provider messages are multi-line walls of text; keep the first line or two.
    msg = (rec.get("message") or "").strip()
    lines = [ln.strip() for ln in msg.splitlines() if ln.strip()][:2]
    for ln in lines:
        print(f"       {ln[:160]}{'…' if len(ln) > 160 else ''}")
    ctx = rec.get("context") or {}
    if ctx:
        short = {k: (str(v)[:60] + "…" if len(str(v)) > 60 else v)
                 for k, v in ctx.items()}
        print(f"       context: {json.dumps(short, ensure_ascii=False)[:200]}")
    if full:
        print("\n" + "\n".join("       " + ln for ln in
                               (rec.get("traceback") or "").splitlines()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["queries", "errors"], default="queries")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--failed", action="store_true",
                    help="only queries that failed or found nothing")
    ap.add_argument("--id", default="", help="show one query in full, plus its errors")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--json", action="store_true", help="raw records")
    args = ap.parse_args()

    if args.stats:
        st = debuglog.stats()
        print(f"  日志目录 : {st['dir']}")
        print(f"  状态     : {'开启' if st['enabled'] else '关闭'}")
        print(f"  查询记录 : {st['queries']}")
        print(f"  异常记录 : {st['errors']}")
        if st["last_error"]:
            e = st["last_error"]
            print(f"  最近异常 : {e.get('type')} @ {e.get('where')}  {e.get('ts','')[:19]}")
            print(f"             {e.get('message')}")
        return 0

    if args.id:
        recs = debuglog.read("queries", limit=1000, query_id=args.id)
        if not recs:
            print(f"  找不到查询 {args.id}", file=sys.stderr)
            return 1
        show_query_detail(recs[0])
        errs = debuglog.read("errors", limit=100, query_id=args.id)
        if errs:
            print(f"\n  关联异常 {len(errs)} 条:")
            for e in errs:
                show_error(e, full=True)
        return 0

    records = debuglog.read(args.kind, limit=args.limit, only_failed=args.failed)

    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return 0

    if not records:
        print(f"  没有记录（{debuglog.LOG_DIR}）")
        return 0

    if args.kind == "queries":
        print(f"\n  最近 {len(records)} 条查询:")
        for r in records:
            show_query_line(r)
        print(f"\n  用 --id <id> 看详情，--failed 只看失败的")
    else:
        print(f"\n  最近 {len(records)} 条异常:")
        for r in records:
            show_error(r)
        print(f"\n  用 --json 看完整 traceback")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
