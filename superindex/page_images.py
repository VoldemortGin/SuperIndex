"""PDF page screenshots for a vision model: PDF linking, page tags, the image
cache and which pages get attached.

Linking (`index --pdf-dir DIR`, env ``SUPERINDEX_PDF_DIR``): each Markdown
file is matched to ``<stem>.pdf`` under DIR (searched recursively; case of the
suffix ignored), else to the ``source`` recorded in its ``.meta.json`` sidecar
(`extractors.azure_di`). The PDF's absolute path and page count go into the
document's metadata (``pdf_path``, ``pdf_pages``, ``pdf_stamp``). No PDF: the
document simply has no images.

Page tags (``docs/<id>/page_tags.json``, written at index time, built lazily
for older stores): ``has_table`` (an HTML ``<table>`` or a pipe table),
``has_figure`` (``<figure>``), ``low_text`` (fewer than `LOW_TEXT_CHARS`
characters once tags and whitespace are gone — a scanned page, a chart or a
page Azure DI mostly missed).

Images are rendered on first use (`superindex.page_render`) and cached as
``docs/<id>/images/<max_side>/p<N>.jpg``; re-linking a different PDF clears
the cache. Any rendering failure means "no image" — the question is still
answered from the text.

Attaching (`--page-image off|auto|always` on ask / serve / batch, env
``SUPERINDEX_PAGE_IMAGE``, default off; at most ``SUPERINDEX_PAGE_IMAGE_MAX``
images per question, default 3):
  auto    prefetch candidates carrying any tag, in rank order;
  always  prefetch candidates in rank order;
and in both modes the agent may ask for a page with `get_page_image`
(`superindex.image_chat`), within the same per-question limit.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from superindex import page_render
from superindex.runtime import ConfigError

logger = logging.getLogger(__name__)

ENV = "SUPERINDEX_PAGE_IMAGE"
MAX_ENV = "SUPERINDEX_PAGE_IMAGE_MAX"
SIDE_ENV = "SUPERINDEX_PAGE_IMAGE_MAX_SIDE"
PDF_DIR_ENV = "SUPERINDEX_PDF_DIR"
MODES = ("off", "auto", "always")
DEFAULT_MAX = 3
LOW_TEXT_CHARS = 300
TAGS_FILE = "page_tags.json"
TAGS_VERSION = 1
IMAGES_DIR = "images"
TAG_NAMES = ("has_table", "has_figure", "low_text")

_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)
_FIGURE_RE = re.compile(r"<figure\b", re.IGNORECASE)
_PIPE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


# ───────────────────────────────────────────────────────────── settings
def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        raise ConfigError(f"{name}: expected a whole number, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be {minimum} or more, got {value}")
    return value


def resolve_mode(mode: str | None = None) -> str:
    """`mode` (the CLI flag), else ``SUPERINDEX_PAGE_IMAGE``, else off."""
    value = (mode if mode is not None else os.environ.get(ENV, "")).strip().lower() or "off"
    if value in ("0", "false", "no"):
        value = "off"
    if value not in MODES:
        raise ConfigError(f"page image mode must be one of {', '.join(MODES)}, got {value!r}")
    return value


def resolve_max() -> int:
    return _env_int(MAX_ENV, DEFAULT_MAX, 0)


def resolve_max_side() -> int:
    return _env_int(SIDE_ENV, page_render.DEFAULT_MAX_SIDE, 64)


# ───────────────────────────────────────────────────────────── page tags
def page_tags(markdown: str) -> dict[str, Any]:
    """Tags of one page's Markdown."""
    text = _COMMENT_RE.sub(" ", markdown or "")
    plain = _TAG_RE.sub(" ", text).replace("|", " ")
    chars = len("".join(plain.split()))
    return {"chars": chars,
            "has_table": bool(_TABLE_RE.search(text) or _PIPE_SEP_RE.search(text)),
            "has_figure": bool(_FIGURE_RE.search(text)),
            "low_text": chars < LOW_TEXT_CHARS}


def build_tags(pages: list[str]) -> dict[str, Any]:
    return {"version": TAGS_VERSION, "low_text_chars": LOW_TEXT_CHARS,
            "pages": {str(i): page_tags(text) for i, text in enumerate(pages, start=1)}}


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def write_tags(doc_dir: Path, pages: list[str]) -> dict[str, Any]:
    """Build and atomically write ``page_tags.json`` into a document folder."""
    tags = build_tags(pages)
    _write_json(doc_dir / TAGS_FILE, tags)
    return tags


def _doc_dir(store: str | os.PathLike[str], doc_id: str) -> Path:
    return Path(store).expanduser() / "docs" / doc_id


def load_tags(store: str | os.PathLike[str], doc_id: str) -> dict[str, dict[str, Any]]:
    """Tags by page number (as a string); built from ``pages.json`` and saved
    (best effort) when missing or stale."""
    doc_dir = _doc_dir(store, doc_id)
    try:
        data = json.loads((doc_dir / TAGS_FILE).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and data.get("version") == TAGS_VERSION
                and data.get("low_text_chars") == LOW_TEXT_CHARS):
            return data["pages"]
    except (OSError, ValueError, KeyError):
        pass
    from superindex import bm25

    try:
        raw = json.loads((doc_dir / "pages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    texts = bm25._page_texts(raw if isinstance(raw, list) else [])
    try:
        return write_tags(doc_dir, texts)["pages"]
    except OSError:
        return build_tags(texts)["pages"]


def tag_names(tags: dict[str, Any] | None) -> list[str]:
    return [t for t in TAG_NAMES if tags and tags.get(t)]


# ───────────────────────────────────────────────────────────── PDF linking
def pdf_index(pdf_dir: Path) -> dict[str, list[Path]]:
    """PDFs under `pdf_dir` (recursive) by lower-cased stem."""
    found: dict[str, list[Path]] = {}
    if not pdf_dir.is_dir():
        raise FileNotFoundError(f"No such PDF directory: {pdf_dir}")
    for path in sorted(pdf_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() == ".pdf":
            found.setdefault(path.stem.lower(), []).append(path.resolve())
    return found


def _sidecar_source(md_path: Path) -> Path | None:
    sidecar = md_path.with_suffix(".meta.json")
    try:
        source = json.loads(sidecar.read_text(encoding="utf-8")).get("source")
    except (OSError, ValueError, AttributeError):
        return None
    if not source:
        return None
    path = Path(str(source)).expanduser()
    return path.resolve() if path.is_file() else None


def find_pdf(md_path: Path, pdfs: dict[str, list[Path]] | None) -> Path | None:
    """The PDF a Markdown file was extracted from: ``<stem>.pdf`` in `pdfs`
    (`pdf_index`; one in a folder of the same name wins a tie), else the
    ``.meta.json`` sidecar's ``source``; None when neither exists."""
    matches = (pdfs or {}).get(md_path.stem.lower()) or []
    if matches:
        same = [p for p in matches if p.parent.name == md_path.parent.name]
        return (same or matches)[0]
    return _sidecar_source(md_path)


def _stamp(pdf: Path) -> str:
    st = pdf.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def pdf_metadata(pdf: Path) -> dict[str, Any]:
    """The doc metadata fields for a linked PDF (raises if it cannot be read)."""
    pdf = pdf.resolve()
    return {"pdf_path": str(pdf), "pdf_pages": page_render.page_count(pdf),
            "pdf_stamp": _stamp(pdf)}


def clear_images(store: str | os.PathLike[str], doc_id: str) -> None:
    shutil.rmtree(_doc_dir(store, doc_id) / IMAGES_DIR, ignore_errors=True)


def linked_pdf(meta: dict[str, Any] | None) -> tuple[Path, int] | None:
    """(PDF path, page count) of a document's linked PDF, if it still exists."""
    info = (meta or {}).get("metadata") or {}
    path, pages = info.get("pdf_path"), info.get("pdf_pages")
    if not path or not isinstance(pages, int):
        return None
    pdf = Path(path)
    return (pdf, pages) if pdf.is_file() else None


# ───────────────────────────────────────────────────────────── images
def page_image(store: str | os.PathLike[str], doc_id: str, pdf: Path, page: int,
               max_side: int) -> bytes | None:
    """The page's JPEG, from the cache or rendered now (and cached); None if
    rendering fails."""
    path = _doc_dir(store, doc_id) / IMAGES_DIR / str(max_side) / f"p{page}{page_render.SUFFIX}"
    try:
        return path.read_bytes()
    except OSError:
        pass
    try:
        data = page_render.render_page(pdf, page, max_side)
    except Exception as exc:  # noqa: BLE001 - no image, answer from the text
        logger.warning("could not render %s page %s: %s: %s", pdf, page,
                       type(exc).__name__, exc)
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        pass
    return data


@dataclass
class Attached:
    doc_id: str
    doc_name: str
    page: int
    source: str              # "auto" | "always" | "tool"
    image: bytes = field(repr=False)
    call_id: str | None = None

    def record(self) -> dict[str, Any]:
        return {"doc_name": self.doc_name, "page": self.page, "source": self.source}


class Session:
    """One question's attached images: the per-question limit, no page twice."""

    def __init__(self, store: str | os.PathLike[str], mode: str, limit: int,
                 max_side: int) -> None:
        from superindex.engine.local_store import DocStore

        self.store = Path(store).expanduser()
        self.mode = mode
        self.limit = limit
        self.max_side = max_side
        self.attached: list[Attached] = []
        self._docs = DocStore(str(self.store))
        self._lock = threading.Lock()

    def _pdf(self, doc_id: str) -> tuple[Path, int] | None:
        return linked_pdf(self._docs.get_meta(doc_id))

    def has(self, doc_id: str, page: int) -> bool:
        return any(a.doc_id == doc_id and a.page == page for a in self.attached)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - len(self.attached))

    def eligible(self, hits: list[Any]) -> list[Any]:
        """The prefetch hits this mode would attach, in rank order (before the
        limit): pages with a linked PDF page — for auto, carrying a tag."""
        out, seen = [], set()
        for hit in hits:
            key = (hit.doc_id, hit.page)
            if key in seen:
                continue
            seen.add(key)
            pdf = self._pdf(hit.doc_id)
            if pdf is None or not 1 <= hit.page <= pdf[1]:
                continue
            if self.mode == "auto" and not tag_names(
                    load_tags(self.store, hit.doc_id).get(str(hit.page))):
                continue
            out.append(hit)
        return out

    def attach_prefetch(self, hits: list[Any]) -> list[Attached]:
        """Render and attach the prefetch pages (`eligible`, up to the limit);
        a page that fails to render is skipped for the next one."""
        added = []
        for hit in self.eligible(hits):
            if not self.remaining:
                break
            got = self._attach(hit.doc_id, hit.doc_name, hit.page, self.mode)
            if isinstance(got, Attached):
                added.append(got)
        return added

    def request(self, doc_id: str, doc_name: str, page: int,
                call_id: str | None = None) -> Attached | str:
        """A page the agent asked for: the attached image, or why not
        ("duplicate", "limit", "no_pdf", "out_of_range", "render_failed")."""
        return self._attach(doc_id, doc_name, page, "tool", call_id)

    def _attach(self, doc_id: str, doc_name: str, page: int, source: str,
                call_id: str | None = None) -> Attached | str:
        with self._lock:
            if self.has(doc_id, page):
                return "duplicate"
            if not self.remaining:
                return "limit"
            pdf = self._pdf(doc_id)
            if pdf is None:
                return "no_pdf"
            if not 1 <= page <= pdf[1]:
                return "out_of_range"
            data = page_image(self.store, doc_id, pdf[0], page, self.max_side)
            if data is None:
                return "render_failed"
            item = Attached(doc_id, doc_name, page, source, data, call_id)
            self.attached.append(item)
            return item

    def records(self) -> list[dict[str, Any]]:
        return [a.record() for a in self.attached]


def new_session(store: str | os.PathLike[str], mode: str) -> Session | None:
    """A fresh per-question session, or None when images are off."""
    if mode == "off":
        return None
    return Session(store, mode, resolve_max(), resolve_max_side())
