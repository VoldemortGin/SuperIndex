#!/usr/bin/env python3
"""
Stage 2 — Retrieval QA test over the AIA annual reports with PageIndex.

This is the paid part: it builds the full local index (LLM-generated node
summaries + tree optimisation) and then answers questions by having the chat
model reason over the tree.

Credentials come from the environment (or a .env file):
    OPENAI_API_KEY            OpenAI
    ANTHROPIC_API_KEY         Anthropic
    DEEPSEEK_API_KEY          DeepSeek
    <PROVIDER>_API_BASE       optional OpenAI-compatible gateway override

Usage:
    python scripts/02_qa_test.py
    python scripts/02_qa_test.py --index-model gpt-5-mini --chat-model gpt-5
    python scripts/02_qa_test.py --chat-model deepseek/deepseek-chat --base-url https://api.deepseek.com
    python scripts/02_qa_test.py --skip-index          # reuse an existing store
    python scripts/02_qa_test.py --only Q05 Q12        # run a subset
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))       # so `extractors` and `nav` are importable
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data" / "aia_reports"
RESULTS = ROOT / "results"
STORE = RESULTS / "pageindex_store"
QUESTIONS = Path(__file__).resolve().parent / "questions.json"

DEFAULT_INDEX_MODEL = "gpt-5-mini"
DEFAULT_CHAT_MODEL = "gpt-5"

# Multi-document question targets: a question's "doc" field may name a single
# PDF, or one of these groupings.
DOC_SETS = {
    "ALL": None,  # resolved to every indexed PDF
    "MIXED_FY2025": [
        "AIA_Annual_Report_FY2025.pdf",
        "AIA_Interim_Report_1H2025.pdf",
    ],
}


def build_client(args):
    from superindex.engine import SuperIndexClient

    index_backend = {}
    chat_backend = {}
    if args.base_url:
        index_backend["api_base"] = args.base_url
        chat_backend["base_url"] = args.base_url
    if args.api_key:
        index_backend["api_key"] = args.api_key
        chat_backend["api_key"] = args.api_key

    return SuperIndexClient(
        index_model=args.index_model,
        chat_model=args.chat_model,
        storage_path=str(STORE),
        index_backend=index_backend or None,
        chat_backend=chat_backend or None,
        instructions=(
            "You are auditing an annual report. Answer with the exact figures, "
            "units and periods stated in the document. If the document does not "
            "contain the answer, say so explicitly instead of guessing."
        ),
    )


def index_documents(client, pdfs, force: bool, attempts: int = 3) -> dict[str, str]:
    """Return {pdf_name: doc_id}, indexing anything not already stored.

    A document that fails is retried with backoff and then skipped, so one bad
    document (or one transient provider error) cannot abort the whole run.
    """
    existing = {}
    try:
        for meta in client.list_documents().get("documents", []):
            existing[meta.get("name")] = meta.get("id")
    except Exception as exc:  # noqa: BLE001 - store may be empty
        print(f"  (could not list existing documents: {exc})")

    doc_ids: dict[str, str] = {}
    failed: list[str] = []
    for pdf in pdfs:
        if not force and pdf.name in existing:
            doc_ids[pdf.name] = existing[pdf.name]
            print(f"  reuse  {pdf.name} -> {existing[pdf.name]}", flush=True)
            continue

        for attempt in range(1, attempts + 1):
            t0 = time.time()
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    res = client.submit_document(str(pdf))
                doc_ids[pdf.name] = res["doc_id"]
                print(f"  index  {pdf.name} -> {res['doc_id']}  ({time.time() - t0:.1f}s)",
                      flush=True)
                break
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).replace("\n", " ")[:180]
                if attempt < attempts:
                    wait = 30 * attempt
                    print(f"  RETRY  {pdf.name} attempt {attempt}/{attempts} failed: {msg}"
                          f" — waiting {wait}s", flush=True)
                    time.sleep(wait)
                else:
                    failed.append(pdf.name)
                    print(f"  FAIL   {pdf.name} after {attempts} attempts: {msg}", flush=True)

    if failed:
        print(f"\n  !! {len(failed)} document(s) could not be indexed: {', '.join(failed)}")
    return doc_ids


def apply_concurrency(n: int) -> None:
    """Cap PageIndex's summary fan-out.

    utils.SUMMARY_CONCURRENCY defaults to 64 simultaneous model calls, which is
    a large enough burst to trip some providers' rate/balance guards.
    """
    import superindex.engine.utils as utils
    if n and n > 0:
        utils.SUMMARY_CONCURRENCY = n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-model", default=os.getenv("PAGEINDEX_INDEX_MODEL", DEFAULT_INDEX_MODEL))
    ap.add_argument("--chat-model", default=os.getenv("PAGEINDEX_CHAT_MODEL", DEFAULT_CHAT_MODEL))
    ap.add_argument("--base-url", default=os.getenv("PAGEINDEX_BASE_URL"))
    ap.add_argument("--api-key", default=os.getenv("PAGEINDEX_API_KEY_OVERRIDE"))
    ap.add_argument("--skip-index", action="store_true", help="reuse the existing local store")
    ap.add_argument("--force-index", action="store_true", help="re-index even if stored")
    ap.add_argument("--only", nargs="*", default=None, help="question ids to run, e.g. Q05 Q12")
    ap.add_argument("--docs", nargs="*", default=None,
                    help="restrict indexing to documents matching these substrings, e.g. 1H2021 FY2025")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="max simultaneous summary calls (default 8; PageIndex's own default is 64)")
    ap.add_argument("--questions", default="questions.json",
                    help="question file under scripts/ (e.g. questions_3docs.json)")
    ap.add_argument("--extractor", choices=["auto", "azure-di", "text-layer"],
                    default="auto",
                    help="which PDF text extractor to use. 'auto' (default) picks "
                         "Azure Document Intelligence whenever AZURE_DI_ENDPOINT "
                         "and AZURE_DI_KEY are set in .env, otherwise the PDF text "
                         "layer. Force one explicitly to compare.")
    ap.add_argument("--out", default="qa_results.json")
    args = ap.parse_args()

    # Resolve the extractor BEFORE the client is built, because indexing reads
    # text through PageIndex's LocalAPI and we swap its extractor out here.
    if args.extractor == "text-layer":
        # blank the Azure vars for this process only
        for var in ("AZURE_DI_ENDPOINT", "AZURE_DI_KEY",
                    "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
                    "AZURE_DOCUMENT_INTELLIGENCE_KEY"):
            os.environ.pop(var, None)
    elif args.extractor == "azure-di":
        from superindex.extractors.backend import is_azure_configured
        if not is_azure_configured():
            print("--extractor azure-di 需要 AZURE_DI_ENDPOINT 与 AZURE_DI_KEY，"
                  "但 .env 里没有配置。", file=sys.stderr)
            return 1
    from superindex.extractors.backend import install_into_pageindex
    backend = install_into_pageindex()
    print()

    qfile = Path(__file__).resolve().parent / args.questions
    spec = json.loads(qfile.read_text(encoding="utf-8"))
    questions = spec["questions"]
    if args.only:
        wanted = {q.upper() for q in args.only}
        questions = [q for q in questions if q["id"].upper() in wanted]

    pdfs = sorted(DATA_DIR.glob("*.pdf"))
    if args.docs:
        keys = [k.lower() for k in args.docs]
        pdfs = [p for p in pdfs if any(k in p.name.lower() for k in keys)]
    if not pdfs:
        print("No PDFs selected.", file=sys.stderr)
        return 1
    RESULTS.mkdir(parents=True, exist_ok=True)

    print(f"index model : {args.index_model}")
    print(f"chat model  : {args.chat_model}")
    print(f"base url    : {args.base_url or '(provider default)'}")
    print(f"concurrency : {args.concurrency}")
    print(f"store       : {STORE}")
    print()

    apply_concurrency(args.concurrency)
    client = build_client(args)

    doc_ids: dict[str, str] = {}
    if not args.skip_index:
        print("Indexing documents (LLM summaries + optimisation) ...", flush=True)
        doc_ids = index_documents(client, pdfs, force=args.force_index)
    else:
        for meta in client.list_documents().get("documents", []):
            doc_ids[meta.get("name")] = meta.get("id")

    print(f"\nRunning {len(questions)} question(s) ...\n")
    records = []
    for q in questions:
        target = q["doc"]
        if target == "ALL":
            names = [p.name for p in pdfs if p.name in doc_ids]
        elif target in DOC_SETS:
            names = [n for n in DOC_SETS[target] if n in doc_ids]
        else:
            names = [target] if target in doc_ids else []
        ids = [doc_ids[n] for n in names]
        if not ids:
            records.append({**q, "answer": None, "error": f"no indexed document for {target}"})
            print(f"[{q['id']}] SKIP - no indexed document for {target}")
            continue

        t0 = time.time()
        try:
            answer = client.chat(q["question"], doc_id=ids if len(ids) > 1 else ids[0])
            err = None
        except Exception as exc:  # noqa: BLE001
            answer, err = None, f"{type(exc).__name__}: {exc}"
        elapsed = time.time() - t0

        records.append({**q, "answer": answer, "error": err,
                        "docs_used": ids, "seconds": round(elapsed, 2)})
        print(f"[{q['id']}] {q['question']}")
        print(f"      expected: {q['expected']}")
        print(f"      answer  : {(answer or err)}")
        print(f"      ({elapsed:.1f}s)\n")

    out_file = RESULTS / args.out
    out_file.write_text(json.dumps({
        "index_model": args.index_model,
        "chat_model": args.chat_model,
        "base_url": args.base_url,
        "extractor": backend.name,
        "extractor_detail": backend.detail,
        "documents": doc_ids,
        "results": records,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        shown = out_file.relative_to(ROOT)
    except ValueError:
        shown = out_file          # --out may point outside the project
    print(f"Wrote {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
