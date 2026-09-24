"""`python -m superindex` — index Azure DI Markdown, ask questions, serve the web UI.

    python -m superindex index corpus_md/ [--store DIR] [--no-summary]
    python -m superindex ask "What was the 2021 final dividend?" [--doc NAME_OR_ID ...]
    python -m superindex search "final dividend 2021" [--doc NAME_OR_ID ...] [--top-k 5]
    python -m superindex serve [--port 8787] [--store DIR]

Models and endpoints come from `.env` (working directory, then the
executable's folder) or the CLI flags; see `.env.example`.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path
from typing import Any

from superindex.runtime import (
    ConfigError,
    LLMSettings,
    configure_litellm,
    default_store,
    load_env,
    set_offline_defaults,
)


def _add_llm_flags(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("LLM (default: .env)")
    g.add_argument("--index-model", help="LiteLLM model for summaries (PAGEINDEX_INDEX_MODEL)")
    g.add_argument("--chat-model", help="LiteLLM model for answering (PAGEINDEX_CHAT_MODEL)")
    g.add_argument("--base-url", help="OpenAI-compatible / Ollama endpoint (PAGEINDEX_BASE_URL)")
    g.add_argument("--api-key", help="key for --base-url (PAGEINDEX_API_KEY_OVERRIDE)")
    g.add_argument("--reasoning-effort",
                   help='chat reasoning effort (PAGEINDEX_REASONING_EFFORT); "" sends none')


def _settings(args: argparse.Namespace) -> LLMSettings:
    return LLMSettings.resolve(
        index_model=getattr(args, "index_model", None),
        chat_model=getattr(args, "chat_model", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
        reasoning_effort=getattr(args, "reasoning_effort", None),
    )


def _store(args: argparse.Namespace) -> Path:
    return Path(args.store).expanduser() if args.store else default_store()


def make_client(settings: LLMSettings, store: Path, instructions: str | None = None) -> Any:
    """A PageIndexClient over the store, on the configured chat model, whose
    agent also has the `search_pages` keyword tool."""
    chat_model = settings.require("chat")
    configure_litellm()
    from pageindex import PageIndexClient

    from superindex import agent_search

    agent_search.install()
    instructions = "\n\n".join(t for t in (instructions, agent_search.GUIDANCE) if t)

    return PageIndexClient(
        index_model=settings.index_model or chat_model,
        chat_model=chat_model,
        storage_path=str(store),
        index_backend=settings.index_backend(),
        chat_backend=settings.chat_backend(),
        instructions=instructions,
    )


# ───────────────────────────────────────────────────────────── index
def cmd_index(args: argparse.Namespace) -> int:
    from superindex.md_ingest import find_markdown, index_markdown

    settings = _settings(args)
    summary_model = None
    if not args.no_summary:
        summary_model = settings.require("index")
        configure_litellm()
    store = _store(args)
    files = find_markdown(Path(args.path).expanduser())
    if not files:
        print(f"no Markdown files under {args.path}", file=sys.stderr)
        return 1
    print(f"store   : {store}")
    print(f"summary : {summary_model or 'off'}")
    failed = 0
    for md in files:
        t0 = time.time()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                res = index_markdown(md, store, summary_model=summary_model,
                                     backend=settings.index_backend(),
                                     concurrency=args.concurrency,
                                     page_chars=args.page_chars, force=args.force)
        except Exception as exc:  # noqa: BLE001 - keep going with the next file
            failed += 1
            print(f"  FAIL  {md.name}: {type(exc).__name__}: {exc}", flush=True)
            continue
        verb = "skip " if res.skipped else "index"
        pages = f"{res.pages} pages" + ("" if res.has_markers else " (pseudo)")
        print(f"  {verb} {res.name} -> {res.doc_id}  {pages}, {res.nodes} nodes"
              f"  ({time.time() - t0:.1f}s)", flush=True)
    return 1 if failed else 0


# ───────────────────────────────────────────────────────────── ask
def _resolve_docs(docs: list[dict[str, Any]], wanted: list[str]) -> list[str]:
    ids = []
    for w in wanted:
        match = [d["id"] for d in docs if w in (d.get("id"), d.get("name"))]
        if not match:
            match = [d["id"] for d in docs if w.lower() in (d.get("name") or "").lower()]
        if not match:
            raise ConfigError(f"no indexed document matches {w!r}; "
                              f"available: {', '.join(d.get('name') or '' for d in docs)}")
        ids.extend(m for m in match if m not in ids)
    return ids


def cmd_ask(args: argparse.Namespace) -> int:
    settings = _settings(args)
    client = make_client(settings, _store(args), instructions=args.instructions)
    if not client.list_documents(limit=1).get("documents"):
        print(f"no documents in {_store(args)} — run `index` first", file=sys.stderr)
        return 1
    scope: str | list[str] | None = None
    if args.doc:
        ids = _resolve_docs(client.list_documents(limit=100).get("documents", []), args.doc)
        scope = ids[0] if len(ids) == 1 else ids
    stream = client.chat(args.question, doc_id=scope, stream=True,
                         reasoning_effort=settings.reasoning_effort)
    for ev in stream.events:
        etype = ev.get("type")
        if etype == "answer":
            print(ev.get("delta", ""), end="", flush=True)
        elif etype == "tool_call" and args.verbose:
            arguments = json.dumps(ev.get("arguments"), ensure_ascii=False)
            print(f"\n[tool] {ev.get('name')} {arguments}", file=sys.stderr, flush=True)
    print()
    return 0


# ───────────────────────────────────────────────────────────── search
def cmd_search(args: argparse.Namespace) -> int:
    from pageindex.local_store import DocStore

    from superindex import bm25

    store = _store(args)
    docs = [m for m in DocStore(str(store)).list_metas() if m.get("status") == "completed"]
    if not docs:
        print(f"no documents in {store} — run `index` first", file=sys.stderr)
        return 1
    scope = _resolve_docs(docs, args.doc) if args.doc else None
    result = bm25.search(store, args.query, doc_ids=scope, top_k=args.top_k)
    for name in result.built:
        print(f"(built missing keyword index for {name})", file=sys.stderr)
    if args.json:
        print(json.dumps([h.to_dict() for h in result.hits], ensure_ascii=False, indent=2))
        return 0
    if not result.hits:
        print(f"no match in {result.searched} document(s)")
        return 0
    for rank, hit in enumerate(result.hits, start=1):
        label = f" (printed {hit.page_label})" if hit.page_label else ""
        print(f"{rank}. [{hit.score:.2f}] {hit.doc_name}  p.{hit.page}{label}")
        if hit.section:
            print(f"   section: {hit.section}")
        print(f"   {hit.snippet}\n")
    return 0


# ───────────────────────────────────────────────────────────── serve
def cmd_serve(args: argparse.Namespace) -> int:
    settings = _settings(args)
    store = _store(args)
    from webapp import server

    client = make_client(settings, store, instructions=args.instructions or server.INSTRUCTIONS)

    def corpus_status() -> dict[str, Any]:
        # The stock status lists PDFs under data/ as "pending"; a Markdown
        # store has no such folder, so everything stored is the corpus.
        indexed = server.list_documents()
        return {"indexed": indexed, "pending": [], "total_pdfs": len(indexed),
                "index_model": settings.index_model or settings.chat_model,
                "chat_model": settings.chat_model}

    server.STORE = store
    server._client = client
    server.corpus_status = corpus_status
    server.REASONING_EFFORT = settings.reasoning_effort
    print(f"store   : {store}")
    return server.run(args.host, args.port)


# ───────────────────────────────────────────────────────────── main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="superindex", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="index a Markdown file or a folder of them")
    p.add_argument("path", help="a .md file or a directory (searched recursively)")
    p.add_argument("--store", help="store directory (SUPERINDEX_STORE)")
    p.add_argument("--no-summary", action="store_true",
                   help="build the tree without any LLM call (no summaries/description)")
    p.add_argument("--force", action="store_true", help="re-index unchanged files")
    p.add_argument("--concurrency", type=int, default=8,
                   help="simultaneous summary calls (default 8)")
    p.add_argument("--page-chars", type=int, default=4000,
                   help="pseudo-page size for Markdown without page markers")
    _add_llm_flags(p)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("ask", help="ask a question over the indexed documents")
    p.add_argument("question")
    p.add_argument("--doc", action="append",
                   help="document name, id or name fragment (repeatable; default: all)")
    p.add_argument("--store", help="store directory (SUPERINDEX_STORE)")
    p.add_argument("--instructions", help="extra standing guidance for the answering agent")
    p.add_argument("-v", "--verbose", action="store_true", help="print tool calls to stderr")
    _add_llm_flags(p)
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("search", help="keyword (BM25) search over the store, no LLM")
    p.add_argument("query")
    p.add_argument("--doc", action="append",
                   help="document name, id or name fragment (repeatable; default: all)")
    p.add_argument("--top-k", type=int, default=5, help="pages to show (default 5)")
    p.add_argument("--store", help="store directory (SUPERINDEX_STORE)")
    p.add_argument("--json", action="store_true", help="print the hits as JSON")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("serve", help="start the web chat UI over the store")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--store", help="store directory (SUPERINDEX_STORE)")
    p.add_argument("--instructions", help="replace the web UI's standing guidance")
    _add_llm_flags(p)
    p.set_defaults(func=cmd_serve)
    return ap


def main(argv: list[str] | None = None) -> int:
    set_offline_defaults()
    load_env()
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles default to a legacy code page; answers are UTF-8.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    from pageindex.errors import PageIndexAPIError

    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (PageIndexAPIError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
