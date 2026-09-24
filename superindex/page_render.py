"""PDF page rendering behind one small interface.

The rest of superindex only calls `page_count` and `render_page`; the backend
(currently pypdfium2, with Pillow for the JPEG encoding) is imported lazily,
so a build without it still indexes and answers — callers treat any exception
as "no image".

JPEG, not PNG: a vision model is billed by pixel dimensions, not bytes, so the
format only changes the payload size — and a JPEG of a rendered page (text,
tables, charts, photos) is typically several times smaller than the PNG at the
same size, with no visible loss for reading at quality 80.
"""
from __future__ import annotations

import io
import os
import threading
from pathlib import Path

DEFAULT_MAX_SIDE = 1600
JPEG_QUALITY = 80
MIME = "image/jpeg"
SUFFIX = ".jpg"

# pdfium is not thread-safe: `serve` and `batch --concurrency` render from
# several threads.
_LOCK = threading.Lock()


def page_count(pdf_path: str | os.PathLike[str]) -> int:
    """Number of pages in the PDF."""
    import pypdfium2 as pdfium

    with _LOCK:
        pdf = pdfium.PdfDocument(str(Path(pdf_path)))
        try:
            return len(pdf)
        finally:
            pdf.close()


def render_page(pdf_path: str | os.PathLike[str], page_index: int,
                max_side: int = DEFAULT_MAX_SIDE) -> bytes:
    """Page `page_index` (1-based, the physical PDF page) as JPEG bytes, scaled
    so its longer side is `max_side` pixels."""
    import pypdfium2 as pdfium

    with _LOCK:
        pdf = pdfium.PdfDocument(str(Path(pdf_path)))
        try:
            if not 1 <= page_index <= len(pdf):
                raise IndexError(f"page {page_index} is outside 1..{len(pdf)}")
            page = pdf[page_index - 1]
            try:
                width, height = page.get_size()
                scale = max(1, max_side) / max(width, height, 1.0)
                image = page.render(scale=scale).to_pil().convert("RGB")
            finally:
                page.close()
        finally:
            pdf.close()
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()
