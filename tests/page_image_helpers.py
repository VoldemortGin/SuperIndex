"""Test fixtures for page images: a tiny hand-written PDF with a matching
DI-style Markdown file, and a fake OpenAI-compatible chat server that records
every request body."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Page 1: prose; page 2: a table; page 3: a bar chart with a short caption.
MARKDOWN = """<!-- page: 1 -->
# Annual Report 2023

{prose}

<!-- page: 2 -->
## Financial highlights

<table>
<tr><th>Item</th><th>2023</th><th>2022</th></tr>
<tr><td>Revenue</td><td>1,234</td><td>1,100</td></tr>
<tr><td>Profit</td><td>567</td><td>498</td></tr>
</table>

<!-- page: 3 -->
## Regional revenue

<figure>
Bar chart: revenue by region
</figure>
""".format(prose=" ".join(["The group delivered steady growth across all markets this year, "
                           "with new business value rising and a strong capital position."] * 5))


def _text(x: int, y: int, size: int, text: str) -> str:
    return f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET\n"


def _page_streams() -> list[str]:
    p1 = _text(72, 720, 24, "Annual Report 2023")
    for i in range(12):
        p1 += _text(72, 680 - i * 18, 11, "The group delivered steady growth across all markets.")
    p2 = _text(72, 720, 18, "Financial highlights") + "0.5 w\n"
    rows = [("Item", "2023", "2022"), ("Revenue", "1,234", "1,100"), ("Profit", "567", "498")]
    for r, row in enumerate(rows):
        y = 660 - r * 30
        for c, cell in enumerate(row):
            p2 += _text(80 + c * 150, y + 10, 12, cell)
    for r in range(len(rows) + 1):
        p2 += f"72 {690 - r * 30} m 522 {690 - r * 30} l S\n"
    for c in range(4):
        p2 += f"{72 + c * 150} 690 m {72 + c * 150} 600 l S\n"
    p3 = _text(72, 720, 18, "Regional revenue")
    for i, (h, color) in enumerate([(220, "0.2 0.4 0.8"), (150, "0.9 0.5 0.1"),
                                    (90, "0.3 0.7 0.3")]):
        p3 += f"{color} rg {120 + i * 130} 300 80 {h} re f\n"
    p3 += "0 g\n" + _text(120, 270, 12, "Bar chart: revenue by region")
    return [p1, p2, p3]


def write_pdf(path: Path, pages: int = 3) -> Path:
    """A valid Letter-size PDF (Helvetica text, lines, filled rectangles)."""
    streams = (_page_streams() * pages)[:pages]
    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, stream in enumerate(streams):
        content = stream.encode("latin-1")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> "
                       f"/Contents {5 + 2 * i} 0 R >>".encode())
        objects.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content
                       + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


def write_corpus(folder: Path, name: str = "report2023") -> tuple[Path, Path]:
    """(Markdown file, PDF file) with the same stem, in `folder`/md and `folder`/pdf."""
    md = folder / "md" / f"{name}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(MARKDOWN, encoding="utf-8")
    return md, write_pdf(folder / "pdf" / f"{name}.pdf")


# ───────────────────────────────────────────────────────────── fake LLM
Reply = dict[str, Any]   # {"tool": name, "arguments": {...}} or {"text": "..."}


class FakeChatServer:
    """OpenAI-compatible ``/chat/completions`` (any path prefix, so it serves
    both ``<base>/v1/chat/completions`` and Azure's
    ``/openai/deployments/<name>/chat/completions``), streaming or not. Each
    request body is recorded; `script(n, body)` picks the n-th reply."""

    def __init__(self, script: Callable[[int, dict[str, Any]], Reply]) -> None:
        self.script = script
        self.requests: list[dict[str, Any]] = []
        self.paths: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not self.path.split("?")[0].endswith("/chat/completions"):
                    self.send_response(404)
                    self.end_headers()
                    return
                n = len(server.requests)
                server.requests.append(body)
                server.paths.append(self.path)
                reply = server.script(n, body)
                if body.get("stream"):
                    self._stream(body, reply, n)
                else:
                    self._json(body, reply, n)

            def _message(self, reply: Reply, n: int) -> tuple[dict[str, Any], str]:
                if "tool" in reply:
                    return ({"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call_{n}", "type": "function",
                        "function": {"name": reply["tool"],
                                     "arguments": json.dumps(reply["arguments"])}}]},
                            "tool_calls")
                return {"role": "assistant", "content": reply["text"]}, "stop"

            def _json(self, body: dict[str, Any], reply: Reply, n: int) -> None:
                message, finish = self._message(reply, n)
                data = json.dumps({
                    "id": f"chatcmpl-{n}", "object": "chat.completion", "created": 0,
                    "model": body.get("model", "fake"),
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                              "total_tokens": 15}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, body: dict[str, Any], reply: Reply, n: int) -> None:
                message, finish = self._message(reply, n)
                base = {"id": f"chatcmpl-{n}", "object": "chat.completion.chunk",
                        "created": 0, "model": body.get("model", "fake")}
                delta: dict[str, Any] = {"role": "assistant"}
                if "tool_calls" in message:
                    delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
                else:
                    delta["content"] = message["content"]
                chunks = [
                    {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
                    {**base, "choices": [], "usage": {"prompt_tokens": 10,
                                                      "completion_tokens": 5,
                                                      "total_tokens": 15}},
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for chunk in chunks:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> FakeChatServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
