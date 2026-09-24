"""
SuperIndex — local chat server.

A small dependency-free HTTP server (Python stdlib only) that exposes the
SuperIndex local client as a streaming chat API, plus a single-page UI.

    GET  /                 the chat UI
    GET  /api/status       indexed documents + corpus info
    POST /api/ask          {"question": str, "doc_ids": [...]} -> SSE stream

The SSE stream carries the agent's run as typed events, so the UI can show
which document nodes the model actually opened before it answered.

Usage (`superindex serve` is the same, with all its options):
    python -m superindex.webapp.server                 # http://127.0.0.1:8787
    python -m superindex.webapp.server --port 9000
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

from superindex.runtime import DEFAULT_INSTRUCTIONS, default_store


def _static_dir() -> Path:
    # A PyInstaller build unpacks bundled data under sys._MEIPASS (the spec
    # bundles the folder as "superindex/webapp/static"); an installed package
    # carries it next to this module.
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "superindex" / "webapp" / "static"
    return Path(str(resources.files("superindex.webapp") / "static"))


STORE = default_store()
STATIC = _static_dir()

# `superindex serve` sets the models, the store and the reasoning effort from
# its settings before `run()`; these are only what `get_client` falls back to.
INDEX_MODEL: str | None = None
CHAT_MODEL: str | None = None
REASONING_EFFORT: str | None = None
# Pages keyword-searched before each question and handed to the agent as
# hints (superindex.prefetch); `superindex serve` sets it, 0 is off.
PREFETCH_K = 0
# PDF page screenshots for a vision model (superindex.page_images): "off",
# "auto" or "always"; `superindex serve` sets it.
PAGE_IMAGE = "off"

_client = None
_client_lock = threading.Lock()
_chat_lock = threading.Lock()

INSTRUCTIONS = DEFAULT_INSTRUCTIONS


def get_client():
    """One shared client; the engine keeps the doc store in it."""
    global _client
    with _client_lock:
        if _client is None:
            from superindex.engine import SuperIndexClient
            _client = SuperIndexClient(
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
    indexed = list_documents()
    return {
        "indexed": indexed,
        "pending": [],
        "total_pdfs": len(indexed),
        "index_model": INDEX_MODEL,
        "chat_model": CHAT_MODEL,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SuperIndexChat/1.0"

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
            message = question
            from superindex import image_chat, page_images

            session = page_images.new_session(STORE, PAGE_IMAGE)
            if PREFETCH_K:
                from superindex import prefetch

                message, hits = prefetch.prepare(STORE, question, doc_ids, PREFETCH_K)
                event = {"candidates": prefetch.candidates(hits)}
                if session is not None:
                    session.attach_prefetch(hits)
                    event["images"] = session.records()
                emit("prefetch", event)
            # One run per answer; serialize so two browser tabs cannot
            # interleave runs on the same client.
            with _chat_lock:
                stream = image_chat.chat(client, message, doc_id=scope,
                                         reasoning_effort=REASONING_EFFORT, session=session)
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
    from superindex.cli import main as cli_main

    return cli_main(["serve", *sys.argv[1:]])


def run(host: str, port: int) -> int:
    status = corpus_status()
    print("SuperIndex chat server")
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
