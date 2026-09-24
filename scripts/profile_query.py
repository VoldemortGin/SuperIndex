#!/usr/bin/env python3
"""
Diagnose query latency for the AIA PageIndex setup.

Measures three things that dominate response time:
  1. tool payload sizes (they become the prompt on every agent turn)
  2. the per-phase wall clock of a real query, from the event stream
  3. how many LLM round trips the agent makes

Usage:
    uv run python scripts/profile_query.py
    uv run python scripts/profile_query.py --question "FY2021 的 VONB 是多少？" --docs FY2021
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

STORE = ROOT / "results" / "pageindex_store"
DATA_DIR = ROOT / "data" / "aia_reports"


def make_client():
    from superindex.engine import SuperIndexClient
    return SuperIndexClient(
        index_model="deepseek/deepseek-flash",
        chat_model="deepseek/deepseek-flash",
        storage_path=str(STORE),
    )


def resolve(client, needle: str):
    docs = client.list_documents(limit=100).get("documents", [])
    for d in docs:
        if needle.lower() in (d.get("name") or "").lower():
            return d
    return None


def profile_payloads(client, doc):
    """The two tool payloads, measured as the model would receive them."""
    from superindex.engine.agent_tools import call_tool

    print("=" * 78)
    print("1. 工具返回体大小（每次 agent 回合都会进 prompt）")
    print("=" * 78)

    t0 = time.time()
    raw, _ = call_tool(client, "get_document_structure", {"doc_name": doc["name"]})
    t_struct = time.time() - t0
    env = json.loads(raw)
    data = env.get("data") or {}
    parts = data.get("total_parts", 1)
    print(f"get_document_structure : {len(raw):>8,} 字符   {t_struct:6.2f}s   "
          f"total_parts={parts}")
    if parts and parts > 1:
        print(f"    !! 结构被切成 {parts} 片 —— 模型要读完整棵树需调用 {parts} 次")

    t0 = time.time()
    raw2, _ = call_tool(client, "get_page_content",
                        {"doc_name": doc["name"], "pages": "14-16"})
    t_page = time.time() - t0
    env2 = json.loads(raw2)
    print(f"get_page_content(14-16) : {len(raw2):>8,} 字符   {t_page:6.2f}s")
    print()
    print(f"合计一次典型检索的上下文增量: ~{(len(raw) + len(raw2)):,} 字符 "
          f"(≈{(len(raw) + len(raw2)) / 4:,.0f} tokens)")
    return len(raw), len(raw2)


def profile_query(client, doc, question):
    print()
    print("=" * 78)
    print("2. 真实查询的分阶段耗时")
    print("=" * 78)
    print(f"问题: {question}")
    print()

    t0 = time.time()
    stream = client.chat(question, doc_id=doc["id"], stream=True)
    turns = 0
    first = None
    last = t0
    rows = []
    for ev in stream.events:
        now = time.time() - t0
        et = ev.get("type")
        if first is None:
            first = now
        gap = now - (last - t0)
        if et == "tool_call":
            turns += 1
            rows.append((now, gap, f"tool_call   {ev.get('name')}"))
        elif et == "tool_result":
            out = ev.get("output")
            size = len(out) if isinstance(out, str) else len(json.dumps(out))
            rows.append((now, gap, f"tool_result {ev.get('name')}  {size:,} 字符"))
        last = time.time() + t0

    total = time.time() - t0
    for at, gap, label in rows:
        print(f"  {at:7.2f}s  (+{gap:6.2f}s)  {label}")

    print()
    print(f"  首字节延迟 : {first:6.2f}s")
    print(f"  工具调用数 : {turns}")
    print(f"  总耗时     : {total:6.2f}s")
    print(f"  LLM 往返   : 至少 {turns + 1} 次（决策→读工具→…→作答）")
    return total, turns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default="What was the total dividend per share for 2021?")
    ap.add_argument("--docs", default="FY2021")
    args = ap.parse_args()

    client = make_client()
    doc = resolve(client, args.docs)
    if not doc:
        print(f"no indexed document matching {args.docs!r}", file=sys.stderr)
        return 1

    print(f"文档: {doc['name']}  ({doc.get('pageNum')} 页)")
    print(f"chat 模型: deepseek/deepseek-flash")
    print()
    profile_payloads(client, doc)
    profile_query(client, doc, args.question)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
