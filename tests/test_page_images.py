"""Offline tests for PDF page screenshots (`superindex.page_images`,
`superindex.image_chat`, `superindex.page_render`): linking, tags, rendering
and cache, the off/auto/always choice, `get_page_image`, and — against a fake
OpenAI-compatible server — the request bodies both provider setups send.

    pytest tests/test_page_images.py
"""
from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from page_image_helpers import FakeChatServer, write_corpus, write_pdf

from superindex import (
    batch,
    cli,
    image_chat,
    page_images,
    page_render,
    prefetch,
)
from superindex.md_ingest import index_markdown
from superindex.runtime import ConfigError, LLMSettings

DOC = "report2023.md"
ENVS = (page_images.ENV, page_images.MAX_ENV, page_images.SIDE_ENV, page_images.PDF_DIR_ENV,
        prefetch.ENV, prefetch.K_ENV)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    return write_corpus(tmp_path / "corpus")


@pytest.fixture()
def store(tmp_path: Path, corpus: tuple[Path, Path]) -> Path:
    md, pdf = corpus
    path = tmp_path / "store"
    index_markdown(md, path, pdf=pdf)
    return path


def meta_of(store: Path) -> dict[str, Any]:
    from pageindex.local_store import DocStore
    (meta,) = DocStore(str(store)).list_metas()
    return meta


class Hit:
    def __init__(self, doc_id: str, page: int, doc_name: str = DOC) -> None:
        self.doc_id, self.page, self.doc_name = doc_id, page, doc_name


# ───────────────────────────────────────────────────────────── tags
def test_page_tags() -> None:
    assert page_images.page_tags("<table><tr><td>1</td></tr></table>")["has_table"]
    assert page_images.page_tags("| a | b |\n|---|---|\n| 1 | 2 |")["has_table"]
    assert not page_images.page_tags("a | b in prose")["has_table"]
    assert page_images.page_tags("<figure>chart</figure>")["has_figure"]
    tags = page_images.page_tags("<!-- PageHeader=\"x\" -->" + "字" * 299)
    assert tags["chars"] == 299 and tags["low_text"]
    assert not page_images.page_tags("字" * 300)["low_text"]
    assert page_images.tag_names({"has_table": True, "low_text": True}) == ["has_table",
                                                                            "low_text"]


# ───────────────────────────────────────────────────────────── linking
def test_index_links_pdf_and_writes_tags(store: Path, corpus: tuple[Path, Path]) -> None:
    meta = meta_of(store)
    info = meta["metadata"]
    assert info["pdf_path"] == str(corpus[1].resolve()) and info["pdf_pages"] == 3
    assert Path(info["pdf_path"]).is_absolute()
    tags = page_images.load_tags(store, meta["id"])
    assert page_images.tag_names(tags["1"]) == []
    assert page_images.tag_names(tags["2"]) == ["has_table", "low_text"]
    assert page_images.tag_names(tags["3"]) == ["has_figure", "low_text"]


def test_find_pdf_by_stem_or_sidecar(tmp_path: Path, corpus: tuple[Path, Path]) -> None:
    md, pdf = corpus
    pdfs = page_images.pdf_index(pdf.parent)
    assert page_images.find_pdf(md, pdfs) == pdf.resolve()
    upper = write_pdf(tmp_path / "other" / "REPORT2023.PDF")
    assert page_images.find_pdf(md, page_images.pdf_index(upper.parent)) == upper.resolve()
    assert page_images.find_pdf(md, None) is None
    md.with_suffix(".meta.json").write_text(json.dumps({"source": str(pdf)}), encoding="utf-8")
    assert page_images.find_pdf(md, None) == pdf.resolve()
    assert page_images.find_pdf(md, {}) == pdf.resolve()
    with pytest.raises(FileNotFoundError):
        page_images.pdf_index(tmp_path / "missing")


def test_no_pdf_and_mismatch(tmp_path: Path, corpus: tuple[Path, Path]) -> None:
    md, _ = corpus
    res = index_markdown(md, tmp_path / "s1")
    assert res.pdf is None and res.warnings == []
    assert "pdf_path" not in meta_of(tmp_path / "s1")["metadata"]
    short = write_pdf(tmp_path / "short.pdf", pages=2)
    res = index_markdown(md, tmp_path / "s2", pdf=short)
    assert res.pdf and res.warnings == ["page count differs: PDF 2, Markdown 3"]
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    res = index_markdown(md, tmp_path / "s3", pdf=broken)
    assert res.pdf is None and "not linked" in res.warnings[0]
    plain = tmp_path / "plain.md"
    plain.write_text("# Title\n\nno page markers here\n", encoding="utf-8")
    res = index_markdown(plain, tmp_path / "s4", pdf=short)
    assert res.pdf is None and "no page markers" in res.warnings[0]


def test_relink_without_rebuilding(tmp_path: Path, corpus: tuple[Path, Path]) -> None:
    md, pdf = corpus
    path = tmp_path / "store"
    first = index_markdown(md, path)
    pages = (path / "docs" / first.doc_id / "pages.json").read_bytes()
    again = index_markdown(md, path, pdf=pdf)
    assert again.skipped and again.doc_id == first.doc_id and again.pdf == str(pdf.resolve())
    assert meta_of(path)["metadata"]["pdf_pages"] == 3
    assert (path / "docs" / first.doc_id / "pages.json").read_bytes() == pages
    # a cached image goes when the PDF is re-linked
    cache = path / "docs" / first.doc_id / page_images.IMAGES_DIR
    (cache / "1600").mkdir(parents=True)
    (cache / "1600" / "p1.jpg").write_bytes(b"old")
    other = write_pdf(tmp_path / "v2" / "report2023.pdf")
    assert index_markdown(md, path, pdf=other).pdf == str(other.resolve())
    assert not cache.exists()
    # unchanged: nothing rewritten; no PDF given: the link stays
    assert index_markdown(md, path, pdf=other).skipped
    assert index_markdown(md, path).pdf == str(other.resolve())


def test_tags_built_lazily_for_old_stores(store: Path) -> None:
    doc_id = meta_of(store)["id"]
    tags_file = store / "docs" / doc_id / page_images.TAGS_FILE
    tags_file.unlink()
    assert page_images.load_tags(store, doc_id)["2"]["has_table"]
    assert tags_file.is_file()


def test_cli_index_pdf_dir(tmp_path: Path, corpus: tuple[Path, Path],
                           monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture[str]) -> None:
    md, pdf = corpus
    monkeypatch.setenv(page_images.PDF_DIR_ENV, str(pdf.parent))
    args = cli.build_parser().parse_args(["index", str(md.parent), "--store",
                                          str(tmp_path / "s"), "--no-summary"])
    assert cli.cmd_index(args) == 0
    out = capsys.readouterr().out
    assert "(1 found)" in out and f"pdf: {pdf.resolve()}" in out


# ───────────────────────────────────────────────────────────── rendering
def test_render_and_cache(store: Path, corpus: tuple[Path, Path],
                          monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = corpus[1]
    assert page_render.page_count(pdf) == 3
    data = page_render.render_page(pdf, 2, 800)
    from PIL import Image
    image = Image.open(io.BytesIO(data))
    assert image.format == "JPEG" and max(image.size) == 800
    with pytest.raises(IndexError):
        page_render.render_page(pdf, 4)

    doc_id = meta_of(store)["id"]
    first = page_images.page_image(store, doc_id, pdf, 1, 640)
    assert first and (store / "docs" / doc_id / "images" / "640" / "p1.jpg").is_file()

    def boom(*_: Any) -> bytes:
        raise RuntimeError("renderer gone")

    monkeypatch.setattr(page_render, "render_page", boom)
    assert page_images.page_image(store, doc_id, pdf, 1, 640) == first     # cached
    assert page_images.page_image(store, doc_id, pdf, 2, 640) is None       # degrades


# ───────────────────────────────────────────────────────────── choice
def test_modes_and_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    assert page_images.resolve_mode() == "off"
    monkeypatch.setenv(page_images.ENV, "AUTO")
    assert page_images.resolve_mode() == "auto"
    assert page_images.resolve_mode("always") == "always"
    with pytest.raises(ConfigError):
        page_images.resolve_mode("sometimes")
    assert page_images.resolve_max() == 3 and page_images.resolve_max_side() == 1600
    monkeypatch.setenv(page_images.MAX_ENV, "x")
    with pytest.raises(ConfigError):
        page_images.resolve_max()
    for command in (["ask", "q"], ["serve"], ["batch", "q.jsonl"]):
        args = cli.build_parser().parse_args([*command, "--page-image", "always"])
        assert cli._page_image_mode(args) == "always"
    assert page_images.new_session("x", "off") is None


def test_auto_always_and_limit(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = meta_of(store)["id"]
    hits = [Hit(doc_id, p) for p in (1, 2, 3, 2, 9)]
    auto = page_images.new_session(store, "auto")
    assert auto is not None
    assert [(a.page, a.source) for a in auto.attach_prefetch(hits)] == [(2, "auto"), (3, "auto")]
    monkeypatch.setenv(page_images.MAX_ENV, "2")
    always = page_images.new_session(store, "always")
    assert always is not None and always.limit == 2
    assert [a.page for a in always.attach_prefetch(hits)] == [1, 2]
    assert always.records() == [{"doc_name": DOC, "page": 1, "source": "always"},
                                {"doc_name": DOC, "page": 2, "source": "always"}]
    assert always.request(doc_id, DOC, 3) == "limit"


def test_request_dedupe_and_errors(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc_id = meta_of(store)["id"]
    session = page_images.new_session(store, "auto")
    assert session is not None
    got = session.request(doc_id, DOC, 1, "call_1")
    assert isinstance(got, page_images.Attached) and got.source == "tool"
    assert session.request(doc_id, DOC, 1) == "duplicate"
    assert session.request(doc_id, DOC, 4) == "out_of_range"
    assert session.request("pi-nope", "x", 1) == "no_pdf"
    monkeypatch.setattr(page_render, "render_page", lambda *a: 1 / 0)
    assert session.request(doc_id, DOC, 2) == "render_failed"
    assert len(session.attached) == 1


# ───────────────────────────────────────────────────────────── tool + filter
def make_real_client(store: Path) -> Any:
    return cli.make_client(LLMSettings(None, "openai/fake", None, "sk", None), store)


def test_get_page_image_replies(store: Path) -> None:
    client = make_real_client(store)
    session = page_images.new_session(store, "auto")
    assert session is not None

    def call(name: str, page: Any) -> dict[str, Any]:
        return json.loads(image_chat.request_page(client, session, None, name, page, "c"))

    assert call(DOC, 2)["attached"] is True
    again = call(DOC, 2)
    assert again["success"] and again["attached"] is False
    assert call(DOC, 7)["errorCode"] == "OUT_OF_RANGE"
    assert call("nope.md", 1)["error"]
    assert call(DOC, "x")["errorCode"] == "INVALID_INPUT"
    call(DOC, 1)
    call(DOC, 3)
    assert call(DOC, 3)["attached"] is False                   # dedupe before the limit
    session.attached.pop()
    session.limit = 2
    assert call(DOC, 3)["errorCode"] == "LIMIT"
    assert len(session.attached) == 2


def test_input_filter_places_images_after_tool_results(store: Path) -> None:
    from agents.run_config import CallModelData, ModelInputData

    doc_id = meta_of(store)["id"]
    session = page_images.new_session(store, "auto")
    assert session is not None
    session.request(doc_id, DOC, 1, "c1")
    session.request(doc_id, DOC, 2, "c2")
    items = [{"role": "user", "content": "q"},
             {"type": "function_call", "call_id": "c1", "name": "get_page_image",
              "arguments": "{}"},
             {"type": "function_call", "call_id": "c2", "name": "get_page_image",
              "arguments": "{}"},
             {"type": "function_call_output", "call_id": "c1", "output": "ok"},
             {"type": "function_call_output", "call_id": "c2", "output": "ok"}]
    data = CallModelData(model_data=ModelInputData(input=items, instructions="sys"),
                         agent=None, context=None)  # type: ignore[arg-type]
    out = image_chat.input_filter(session)(data)
    assert out.instructions == "sys" and len(out.input) == 7
    labels = [m["content"][0]["text"] for m in out.input[5:]]
    assert labels == [f"[PDF 原页截图] {DOC} 第 1 页", f"[PDF 原页截图] {DOC} 第 2 页"]
    assert out.input[5]["content"][1]["image_url"].startswith("data:image/jpeg;base64,")
    assert len(items) == 5                                     # the run's own list untouched


# ───────────────────────────────────────────────────────────── fake server
def _parts(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return content if isinstance(content, list) else []


def _image_urls(message: dict[str, Any]) -> list[str]:
    return [p["image_url"]["url"] for p in _parts(message) if p.get("type") == "image_url"]


def _assert_jpeg(url: str) -> None:
    head, _, payload = url.partition(",")
    assert head == "data:image/jpeg;base64"
    assert base64.b64decode(payload)[:3] == b"\xff\xd8\xff"


@pytest.mark.parametrize("provider", ["openai", "azure"])
def test_ask_sends_images_to_the_endpoint(provider: str, store: Path,
                                          monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    def script(n: int, body: dict[str, Any]) -> dict[str, Any]:
        if n == 0:
            return {"tool": "get_page_image", "arguments": {"doc_name": DOC, "page": 1}}
        return {"text": "Revenue was 1,234 (page 2 image)."}

    for name in ("PAGEINDEX_BASE_URL", "PAGEINDEX_API_KEY_OVERRIDE",
                 "PAGEINDEX_REASONING_EFFORT", "PAGEINDEX_CHAT_MODEL"):
        monkeypatch.setenv(name, "")
    with FakeChatServer(script) as srv:
        if provider == "openai":
            llm = ["--chat-model", "openai/fake-vision", "--base-url",
                   f"http://127.0.0.1:{srv.port}/v1", "--api-key", "sk-test"]
        else:
            monkeypatch.setenv("AZURE_API_BASE", f"http://127.0.0.1:{srv.port}")
            monkeypatch.setenv("AZURE_API_KEY", "azure-test")
            monkeypatch.setenv("AZURE_API_VERSION", "2024-10-21")
            llm = ["--chat-model", "azure/vision-deploy"]
        args = cli.build_parser().parse_args([
            "ask", "What was revenue in 2023 in the financial highlights table?",
            "--store", str(store), "--page-image", "auto", "-v", *llm])
        assert cli.cmd_ask(args) == 0
    captured = capsys.readouterr()
    assert "Revenue was 1,234" in captured.out
    assert "[page-image] prefetch: report2023.md p.2 (auto), report2023.md p.3 (auto)" \
        in captured.err
    assert "report2023.md p.1 (tool)" in captured.err

    expected_path = ("/v1/chat/completions" if provider == "openai"
                     else "/openai/deployments/vision-deploy/chat/completions")
    assert len(srv.requests) == 2 and all(p.startswith(expected_path) for p in srv.paths)
    first, second = srv.requests
    assert first["stream"] is True
    assert "get_page_image" in [t["function"]["name"] for t in first["tools"]]
    assert "PAGE IMAGES:" in first["messages"][0]["content"]
    # prefetch images: in the question's user message, after its text
    question = first["messages"][-1]
    assert question["role"] == "user"
    kinds = [p["type"] for p in _parts(question)]
    assert kinds == ["text", "text", "image_url", "text", "image_url"]
    assert "问题：What was revenue" in question["content"][0]["text"]
    for url in _image_urls(question):
        _assert_jpeg(url)
    # tool image: tool message stays text, the image follows as a user message
    roles = [m["role"] for m in second["messages"]]
    assert roles[-3:] == ["assistant", "tool", "user"]
    tool_msg, image_msg = second["messages"][-2:]
    assert isinstance(tool_msg["content"], str) and "image_url" not in tool_msg["content"]
    assert json.loads(tool_msg["content"])["attached"] is True
    assert image_msg["content"][0]["text"] == f"[PDF 原页截图] {DOC} 第 1 页"
    (url,) = _image_urls(image_msg)
    _assert_jpeg(url)
    assert len(_image_urls(second["messages"][-4])) == 2       # prefetch images kept


def test_ask_off_sends_no_images(store: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("PAGEINDEX_REASONING_EFFORT", "PAGEINDEX_CHAT_MODEL"):
        monkeypatch.setenv(name, "")
    with FakeChatServer(lambda n, body: {"text": "1,234"}) as srv:
        args = cli.build_parser().parse_args([
            "ask", "revenue 2023", "--store", str(store), "--chat-model", "openai/fake",
            "--base-url", f"http://127.0.0.1:{srv.port}/v1", "--api-key", "sk"])
        assert cli.cmd_ask(args) == 0
    (body,) = srv.requests
    assert "get_page_image" not in [t["function"]["name"] for t in body["tools"]]
    assert isinstance(body["messages"][-1]["content"], str)
    assert "1,234" in capsys.readouterr().out


# ───────────────────────────────────────────────────────────── batch / web
class FakeStream:
    events = ({"type": "answer", "delta": "1,234"},)


def test_batch_records_images(tmp_path: Path, store: Path,
                              monkeypatch: pytest.MonkeyPatch) -> None:
    qfile = tmp_path / "q.jsonl"
    qfile.write_text(json.dumps({"id": "A", "question": "revenue 2023 table",
                                 "expected": "1,234"}) + "\n", encoding="utf-8")
    sessions: list[Any] = []

    def fake_chat(client: Any, message: str, *, session: Any, **_: Any) -> FakeStream:
        sessions.append(session)
        return FakeStream()

    monkeypatch.setattr(cli, "make_client", lambda *a, **k: object())
    monkeypatch.setattr(image_chat, "chat", fake_chat)
    out = tmp_path / "out"
    args = cli.build_parser().parse_args(["batch", str(qfile), "--store", str(store),
                                          "--out", str(out), "--chat-model", "fake/model",
                                          "--page-image", "always"])
    assert batch.cmd_batch(args) == 0
    rec = batch.read_results(out)["A"]
    assert rec["image_count"] == len(rec["page_images"]) == 3
    assert {p["source"] for p in rec["page_images"]} == {"always"}
    summary = (out / batch.SUMMARY_FILE).read_text(encoding="utf-8")
    assert "附图数 |" in summary and "- 附图（always）：共 3 张" in summary
    assert "**附图**：report2023.md:" in summary

    args = cli.build_parser().parse_args(["batch", str(qfile), "--store", str(store),
                                          "--out", str(tmp_path / "off"), "--chat-model",
                                          "fake/model"])
    assert batch.cmd_batch(args) == 0
    assert sessions[-1] is None
    assert "image_count" not in batch.read_results(tmp_path / "off")["A"]
    assert "附图数" not in (tmp_path / "off" / batch.SUMMARY_FILE).read_text(encoding="utf-8")


def test_web_prefetch_event_lists_images(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from webapp import server

    monkeypatch.setattr(server, "get_client", lambda: object())
    monkeypatch.setattr(server, "STORE", store)
    monkeypatch.setattr(server, "PREFETCH_K", 3)
    monkeypatch.setattr(server, "PAGE_IMAGE", "auto")
    monkeypatch.setattr(image_chat, "chat", lambda *a, **k: FakeStream())
    handler = server.Handler.__new__(server.Handler)
    handler.wfile = io.BytesIO()
    handler.send_response = lambda *a: None             # type: ignore[method-assign]
    handler.send_header = lambda *a: None               # type: ignore[method-assign]
    handler.end_headers = lambda: None                  # type: ignore[method-assign]
    handler.stream_answer("revenue 2023 table", [meta_of(store)["id"]])
    body = handler.wfile.getvalue().decode("utf-8")
    data = json.loads(body.split("\n\n")[0].split("data: ", 1)[1])
    assert [(i["page"], i["source"]) for i in data["images"]] == [(2, "auto"), (3, "auto")]
