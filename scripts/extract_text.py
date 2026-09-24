#!/usr/bin/env python3
"""
Helper — extract text from the AIA PDFs and grep for key metrics.

Used to establish ground truth for the retrieval test, independently of
PageIndex itself (page text comes straight from pypdfium2).

Usage:
    uv run python scripts/extract_text.py "value of new business"
    uv run python scripts/extract_text.py --doc FY2025 "dividend per share"
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pypdfium2 as pdfium

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "aia_reports"


def page_texts(pdf_path: Path) -> list[str]:
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        return [p.get_textpage().get_text_range() for p in doc]
    finally:
        doc.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", help="regex (case-insensitive) to search for")
    ap.add_argument("--doc", default=None, help="filter to docs whose name contains this (e.g. FY2025)")
    ap.add_argument("--context", type=int, default=220, help="characters of context around each hit")
    ap.add_argument("--max-hits", type=int, default=6, help="max hits to print per document")
    args = ap.parse_args()

    rx = re.compile(args.pattern, re.IGNORECASE)

    for pdf in sorted(DATA_DIR.glob("*.pdf")):
        if args.doc and args.doc not in pdf.name:
            continue
        texts = page_texts(pdf)
        hits = 0
        for pno, text in enumerate(texts, start=1):
            flat = re.sub(r"\s+", " ", text)
            for m in rx.finditer(flat):
                if hits >= args.max_hits:
                    break
                lo = max(0, m.start() - args.context // 2)
                hi = min(len(flat), m.end() + args.context)
                snippet = flat[lo:hi].strip()
                print(f"\n=== {pdf.stem} | page {pno} ===")
                print(f"...{snippet}...")
                hits += 1
            if hits >= args.max_hits:
                break
        if hits == 0 and not args.doc:
            continue
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
