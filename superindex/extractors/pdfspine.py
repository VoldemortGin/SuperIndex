"""Offline PDF → Markdown with pdfspine (optional: ``pip install "superindex[pdfspine]"``).

pdfspine is a pure-Rust PDF library (Apache-2.0, no network, no GPU). Its
Markdown export keeps tables as GFM tables and turns larger font sizes into
``#`` headings. The output has the same shape as the text-layer path:
``<!-- page: N -->`` before every page (empty pages keep their marker, so page
numbers match the PDF) and pages joined by a blank line. Encrypted PDFs are
decrypted in memory only; nothing but the returned Markdown leaves this module.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from superindex.extractors.azure_di import PAGE_MARKER
from superindex.md_ingest import PAGE_MARKER_RE

EXTRACTOR = "pdfspine"
OK = "ok"
NO_TEXT_LAYER = "skipped_no_text_layer"
WRONG_PASSWORD = "skipped_wrong_password"

_SURROGATES_RE = re.compile("[\ud800-\udfff]")
_TABLE_ROW_RE = re.compile(r"^\|.*\|[ \t]*$", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(r"^\|(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#{1,6} \S", re.MULTILINE)


@dataclass
class PdfspineResult:
    """`status` is OK, NO_TEXT_LAYER or WRONG_PASSWORD; `markdown` is empty unless OK."""
    status: str
    markdown: str = ""
    pages: int | None = None
    empty_pages: list[int] = field(default_factory=list)
    encrypted: bool = False
    password_used: bool = False


def require_pdfspine() -> ModuleType:
    """The pdfspine module, or an ImportError that says how to install it."""
    try:
        import pdfspine
    except ImportError as exc:
        raise ImportError('pdfspine is not installed: pip install "superindex[pdfspine]" '
                          "(or pip install pdfspine==0.12.0)") from exc
    return pdfspine


def page_markdowns(doc: Any) -> list[str]:
    """Each page's Markdown, with one heading scale for the whole document.

    `Document.to_markdown` drops empty pages, which would shift page numbers,
    so its per-page steps are run here directly; versions without them fall
    back to `Page.to_markdown` (heading levels then computed page by page)."""
    try:
        from pdfspine._markdown import (
            MarkdownOptions,
            compute_heading_scale,
            render_page,
        )

        options = MarkdownOptions()
        sources = [page._markdown_source(None, options) for page in doc]
    except (ImportError, AttributeError):  # private steps moved in another pdfspine version
        return [page.to_markdown().strip() for page in doc]
    scale = compute_heading_scale((data for data, _ in sources), options)
    return [render_page(data, regions, scale, options, page_number=number).strip()
            for number, (data, regions) in enumerate(sources)]


def convert_pdf(pdf: Path, passwords: Iterable[str] = ()) -> PdfspineResult:
    """Convert one PDF. A PDF that needs a password is opened with the first of
    `passwords` that works (WRONG_PASSWORD when none does); one whose pages are
    all empty (scanned) is NO_TEXT_LAYER."""
    pdfspine = require_pdfspine()
    with pdfspine.open(pdf) as doc:
        # owner-password-only PDFs open without a password but still report their encryption
        encrypted = bool(doc.needs_pass or (doc.metadata or {}).get("encryption"))
        used = False
        if doc.needs_pass:
            if not any(doc.authenticate(p) for p in passwords if p):
                return PdfspineResult(WRONG_PASSWORD, encrypted=True)
            used = True
        texts = [_SURROGATES_RE.sub("�", text) for text in page_markdowns(doc)]
    empty_pages = [n for n, text in enumerate(texts, start=1) if not text.strip()]
    if len(empty_pages) == len(texts):
        return PdfspineResult(NO_TEXT_LAYER, pages=len(texts), empty_pages=empty_pages,
                              encrypted=encrypted, password_used=used)
    parts: list[str] = []
    for number, text in enumerate(texts, start=1):
        parts += [PAGE_MARKER.format(n=number), text]
    return PdfspineResult(OK, "\n\n".join(parts), len(texts), empty_pages, encrypted, used)


def markdown_stats(markdown: str) -> dict[str, int]:
    """Counts for comparing extractors (no content): pages, empty pages, GFM
    tables and their rows (header included), headings, characters of page text."""
    parts = PAGE_MARKER_RE.split(markdown)
    pages = [parts[i + 1] for i in range(1, len(parts), 2)] or [markdown]
    separators = len(_TABLE_SEPARATOR_RE.findall(markdown))
    return {"pages": len(pages),
            "empty_pages": sum(1 for text in pages if not text.strip()),
            "tables": separators,
            "table_rows": len(_TABLE_ROW_RE.findall(markdown)) - separators,
            "headings": len(_HEADING_RE.findall(markdown)),
            "chars": sum(len(text.strip()) for text in pages)}


__all__ = [
    "EXTRACTOR",
    "NO_TEXT_LAYER",
    "OK",
    "WRONG_PASSWORD",
    "PdfspineResult",
    "convert_pdf",
    "markdown_stats",
    "page_markdowns",
    "require_pdfspine",
]
