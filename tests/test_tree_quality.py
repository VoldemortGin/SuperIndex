"""Tree quality regression on real PDFs — offline, no LLM.

Builds each PDF's tree the way `index_markdown` does by default (pdfspine
Markdown + flash structure, `md_ingest.build_doc_tree`) and checks node
counts, repeated titles and fake titles. The PDFs are not in the repository:
point ``SUPERINDEX_TREE_QA_DIR`` at a folder of them (the ten of the flash vs
pdfspine comparison, e.g. ``A_geely_ar2024_tc.pdf``, have expected node
counts below; any other PDF gets the generic bounds). Skipped without it, or
without pdfspine.

    SUPERINDEX_TREE_QA_DIR=/path/to/pdfs pytest tests/test_tree_quality.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex import page_render, tree_rules  # noqa: E402
from superindex.md_ingest import (  # noqa: E402
    _preorder,
    _public_tree,
    build_doc_tree,
    parse_pages,
)

QA_DIR = Path(os.environ.get("SUPERINDEX_TREE_QA_DIR", "").strip() or ROOT / "data" / "tree_qa")
PDFS = sorted(QA_DIR.glob("*.pdf")) if QA_DIR.is_dir() else []
# node counts measured on the comparison set (flash + rules, 2026-10)
EXPECTED_NODES = {"A_geely_ar2024_tc": 163, "B_aia_1h26pres_en": 68, "C_aia_ar2024_en": 374,
                  "D_aia_ir2024_en": 174, "E_aialife_disc2024_sc": 78, "F_aia_fy23pres_en": 80,
                  "G_geely_esg2025_tc": 130, "H_aia_ir2022ann_en": 124,
                  "I_solvency2019q2_sc": 9, "J_aia_1h22transcript_en": 7}


@pytest.fixture(autouse=True)
def no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("tree building called an LLM")
    monkeypatch.setattr(litellm, "completion", boom)
    monkeypatch.setattr(litellm, "acompletion", boom)


@pytest.mark.skipif(not PDFS, reason=f"no PDFs in {QA_DIR} (set SUPERINDEX_TREE_QA_DIR)")
@pytest.mark.parametrize("pdf", PDFS, ids=[p.stem for p in PDFS])
def test_tree_quality(pdf: Path) -> None:
    pdfspine = pytest.importorskip("superindex.extractors.pdfspine")
    result = pdfspine.convert_pdf(pdf)
    if result.status != "ok":
        pytest.skip(f"pdfspine: {result.status}")
    parsed = parse_pages(result.markdown)
    pages = len(parsed.pages)
    tree, info = build_doc_tree(parsed, pdf.stem, pdf, "flash", page_render.page_count(pdf))
    nodes = _preorder(_public_tree(tree))
    titles = Counter(" ".join(n["title"].split()) for n in nodes)
    dup = sum(c for c in titles.values() if c >= 3) / len(nodes)
    fake = tree_rules.fake_ratio(tree)
    longest = max(n["end_index"] - n["start_index"] + 1 for n in nodes if not n.get("nodes"))
    print(f"{pdf.stem}: {info['tree_builder']} {len(nodes)} nodes, top {len(tree)}, "
          f"dup {dup:.3f}, fake {fake:.3f}, longest leaf {longest}p")
    assert 1 <= len(nodes) <= tree_rules.MAX_NODES_PER_PAGE * pages
    if expected := EXPECTED_NODES.get(pdf.stem):
        assert info["tree_builder"] == "flash"
        assert 0.7 * expected <= len(nodes) <= 1.3 * expected
    assert dup <= 0.05
    assert fake <= 0.05
    if info["tree_builder"] == "flash":
        assert longest <= tree_rules.MAX_LEAF_PAGES
