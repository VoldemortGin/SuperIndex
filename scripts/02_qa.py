#!/usr/bin/env python3
"""
Stage 2 - Retrieval QA over the indexed AIA annual reports.

Requires an LLM API key (e.g. OPENAI_API_KEY) because both indexing with
summaries and the chat/retrieval agent call a model.

Usage:
    export OPENAI_API_KEY=sk-...
    uv run python scripts/02_qa.py --doc-id <id> --questions scripts/questions.json

The client keeps its document store in ./.pageindex by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUESTIONS = ROOT / "scripts" / "questions.json"
OUT_DIR = ROOT / "results"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-id", action="append", default=None,
                    help="document id to scope the chat (repeatable)")
    ap.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    ap.add_argument("--chat-model", default=os.environ.get("PI_CHAT_MODEL"))
    ap.add_argument("--index-model", default=os.environ.get("PI_INDEX_MODEL"))
    ap.add_argument("--storage-path", default=str(ROOT / ".pageindex"))
    ap.add_argument("--out", default=str(OUT_DIR / "qa_results.json"))
    args = ap.parse_args()

    from superindex.engine import SuperIndexClient

    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    if not isinstance(questions, list):
        questions = questions.get("questions", [])

    kwargs = {"storage_path": args.storage_path}
    if args.chat_model:
        kwargs["chat_model"] = args.chat_model
    if args.index_model:
        kwargs["index_model"] = args.index_model
    client = SuperIndexClient(**kwargs)

    doc_ids = args.doc_id
    if not doc_ids:
        metas = client.list_documents()
        docs = metas.get("documents", metas) if isinstance(metas, dict) else metas
        doc_ids = [d["id"] for d in docs]
    if not doc_ids:
        print(f"No documents in {args.storage_path}. Index Markdown into it first: "
              f"uv run superindex index <file-or-folder> --store {args.storage_path}",
              file=sys.stderr)
        return 1

    print(f"Asking {len(questions)} question(s) across {len(doc_ids)} document(s)\n")

    results = []
    for q in questions:
        item = q if isinstance(q, dict) else {"question": q}
        question = item["question"]
        scope = item.get("doc_id") or doc_ids
        print(f"Q: {question}")
        t0 = time.time()
        try:
            answer = client.chat(question, doc_id=scope)
            err = None
        except Exception as exc:  # noqa: BLE001
            answer, err = None, f"{type(exc).__name__}: {exc}"
        elapsed = round(time.time() - t0, 2)
        if err:
            print(f"   ERROR ({elapsed}s): {err}\n")
        else:
            print(f"   A ({elapsed}s): {answer}\n")
        results.append({
            "id": item.get("id"),
            "question": question,
            "expected": item.get("expected"),
            "source": item.get("source"),
            "answer": answer,
            "error": err,
            "seconds": elapsed,
        })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
