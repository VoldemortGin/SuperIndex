#!/usr/bin/env python3
"""
PageIndex × AIA reports — local chat server.

A small dependency-free HTTP server (Python stdlib only) that exposes the
PageIndex local client as a streaming chat API, plus a single-page UI.

    GET  /                 the chat UI
    GET  /api/status       indexed documents + corpus info
    POST /api/ask          {"question": str, "doc_ids": [...]} -> SSE stream

The SSE stream carries the agent's run as typed events, so the UI can show
which document nodes the model actually opened before it answered.

Usage:
    python webapp/server.py                 # http://127.0.0.1:8787
    python webapp/server.py --port 9000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data" / "aia_reports"
STORE = ROOT / "results" / "pageindex_store"
# A PyInstaller build unpacks bundled data under sys._MEIPASS; bundle the
# folder as "webapp/static" there.
STATIC = (Path(sys._MEIPASS) / "webapp" / "static" if getattr(sys, "frozen", False)
          else Path(__file__).resolve().parent / "static")

INDEX_MODEL = os.getenv("PAGEINDEX_INDEX_MODEL", "deepseek/deepseek-flash")
CHAT_MODEL = os.getenv("PAGEINDEX_CHAT_MODEL", "deepseek/deepseek-flash")
# Reasoning effort is the single biggest latency lever we measured: on a deep
# question it cut wall clock 10.3s -> 5.8s and output tokens by 62%, with the
# answer unchanged. "low" is the default; set it to "" to send nothing and get
# the model's own default back.
REASONING_EFFORT = os.getenv("PAGEINDEX_REASONING_EFFORT", "low").strip() or None

_client = None
_client_lock = threading.Lock()
_chat_lock = threading.Lock()

INSTRUCTIONS = (
    "You are a financial analyst answering questions about AIA Group's annual "
    "and interim reports. Answer with the exact figures, units and periods "
    "stated in the documents, and name the reporting period each figure "
    "belongs to. If the documents do not contain the answer, say so plainly "
    "instead of guessing."
)


def get_client():
    """One shared client; PageIndex keeps the doc store in it."""
    global _client
    with _client_lock:
        if _client is None:
            from pageindex import PageIndexClient
            _client = PageIndexClient(
                index_model=INDEX_MODEL,
                chat_model=CHAT_MODEL,
                storage_path=str(STORE),
                instructions=INSTRUCTIONS,
            )
        return _client


def list_documents() -> list[dict]:
    try:
        docs = get_client().list_documents(limit=100).get("documents", [])
    except Exception:  # noqa: BLE001 - empty store is normal
        return []
    out = []
    for d in docs:
        out.append({
            "id": d.get("id"),
            "name": d.get("name"),
            "pages": d.get("pageNum"),
            "status": d.get("status"),
        })
    return out


def corpus_status() -> dict:
    indexed = {d["name"] for d in list_documents()}
    on_disk = sorted(p.name for p in DATA_DIR.glob("*.pdf"))
    return {
        "indexed": list_documents(),
        "pending": [n for n in on_disk if n not in indexed],
        "total_pdfs": len(on_disk),
        "index_model": INDEX_MODEL,
        "chat_model": CHAT_MODEL,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "PageIndexChat/1.0"

    def log_message(self, fmt, *args):  # quieter console
        if "/api/ask" not in (self.path or ""):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ── helpers ──────────────────────────────────────────────────────────
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str):
        if not path.is_file():
            self.send_error(404, "Not found")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ── routes ───────────────────────────────────────────────────────────
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send_file(STATIC / "index.html", "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send_json(corpus_status())
        elif path == "/api/health":
            self._send_json({"ok": True})
        elif path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents:
                self.send_error(403, "Forbidden")
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
            }.get(target.suffix, "application/octet-stream")
            self._send_file(target, ctype)
        else:
            self.send_error(404, "Not found")

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/ask":
            self.send_error(404, "Not found")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            self._send_json({"error": "invalid JSON body"}, 400)
            return

        question = (payload.get("question") or "").strip()
        doc_ids = payload.get("doc_ids") or []
        if not question:
            self._send_json({"error": "question is required"}, 400)
            return
        if isinstance(doc_ids, str):
            doc_ids = [doc_ids]
        if not doc_ids:
            self._send_json({"error": "select at least one document"}, 400)
            return

        self.stream_answer(question, doc_ids)

    # ── the streaming answer ─────────────────────────────────────────────
    def stream_answer(self, question: str, doc_ids: list[str]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(event: str, data):
            chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()

        try:
            client = get_client()
            scope = doc_ids[0] if len(doc_ids) == 1 else doc_ids
            # One run per answer; serialize so two browser tabs cannot
            # interleave runs on the same client.
            with _chat_lock:
                stream = client.chat(question, doc_id=scope, stream=True,
                                     reasoning_effort=REASONING_EFFORT)
                for ev in stream.events:
                    etype = ev.get("type")
                    if etype == "answer":
                        emit("answer", {"delta": ev.get("delta", "")})
                    elif etype == "thinking":
                        emit("thinking", {"delta": ev.get("delta", "")})
                    elif etype == "tool_call":
                        emit("tool_call", {"name": ev.get("name"),
                                           "arguments": ev.get("arguments")})
                    elif etype == "tool_result":
                        out = ev.get("output")
                        if not isinstance(out, str):
                            out = json.dumps(out, ensure_ascii=False)[:4000]
                        emit("tool_result", {"name": ev.get("name"),
                                             "output": out[:4000]})
            emit("done", {"ok": True})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                emit("error", {"message": f"{type(exc).__name__}: {exc}"})
                emit("done", {"ok": False})
            except Exception:  # noqa: BLE001 - client already gone
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    return run(args.host, args.port)


def run(host: str, port: int) -> int:
    status = corpus_status()
    print(f"PageIndex chat server")
    print(f"  index model : {status['index_model']}")
    print(f"  chat model  : {status['chat_model']}")
    print(f"  indexed     : {len(status['indexed'])} / {status['total_pdfs']} documents")
    if status["pending"]:
        print(f"  pending     : {len(status['pending'])} still indexing")
    print(f"  -> http://{host}:{port}\n")

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
