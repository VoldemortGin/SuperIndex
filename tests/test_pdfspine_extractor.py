"""superindex.extractors.pdfspine (offline PDF → Markdown with pdfspine) and the
notebooks that use it. Tests needing pdfspine skip when it is not installed
(`uv sync --extra pdfspine`).

    pytest tests/test_pdfspine_extractor.py
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from page_image_helpers import write_pdf

from superindex.extractors import pdfspine as ext
from superindex.md_ingest import PAGE_MARKER_RE

ROOT = Path(__file__).resolve().parent.parent


def _pages(markdown: str) -> list[tuple[int, str]]:
    parts = PAGE_MARKER_RE.split(markdown)
    return [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]


def _save(doc, path: Path, **kwargs) -> Path:
    doc.save(str(path), **kwargs)
    return path


@pytest.fixture
def pdfspine():
    return pytest.importorskip("pdfspine")


def test_convert_pages_tables_headings(tmp_path: Path, pdfspine) -> None:
    res = ext.convert_pdf(write_pdf(tmp_path / "report.pdf"))
    assert res.status == ext.OK and res.pages == 3 and res.empty_pages == []
    assert not res.encrypted and not res.password_used
    pages = _pages(res.markdown)
    assert [n for n, _ in pages] == [1, 2, 3]
    assert "# Annual Report 2023" in pages[0][1]
    assert "| Revenue | 1,234 | 1,100 |" in pages[1][1]
    stats = ext.markdown_stats(res.markdown)
    assert stats["pages"] == 3 and stats["tables"] == 1 and stats["table_rows"] == 3 and stats["headings"] >= 3


def test_empty_page_keeps_its_marker(tmp_path: Path, pdfspine) -> None:
    doc = pdfspine.open(write_pdf(tmp_path / "src.pdf"))
    doc.new_page(1)
    res = ext.convert_pdf(_save(doc, tmp_path / "gap.pdf"))
    assert res.status == ext.OK and res.pages == 4 and res.empty_pages == [2]
    pages = _pages(res.markdown)
    assert [n for n, _ in pages] == [1, 2, 3, 4] and not pages[1][1].strip()
    assert "| Revenue |" in pages[2][1]


def test_no_text_layer(tmp_path: Path, pdfspine) -> None:
    doc = pdfspine.open()
    doc.new_page()
    page = doc.new_page()
    page.draw_rect(pdfspine.Rect(100, 100, 300, 300), fill=(0.5, 0.5, 0.5))
    res = ext.convert_pdf(_save(doc, tmp_path / "scan.pdf"))
    assert res.status == ext.NO_TEXT_LAYER and res.pages == 2 and res.empty_pages == [1, 2]
    assert res.markdown == ""


def test_password_candidates(tmp_path: Path, pdfspine) -> None:
    pdf = _save(pdfspine.open(write_pdf(tmp_path / "src.pdf")), tmp_path / "locked.pdf",
                encryption=pdfspine.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner-pw")
    for passwords in ((), ("", "wrong")):
        res = ext.convert_pdf(pdf, passwords)
        assert res.status == ext.WRONG_PASSWORD and res.encrypted and res.markdown == ""
    res = ext.convert_pdf(pdf, ["wrong", "secret"])
    assert res.status == ext.OK and res.encrypted and res.password_used and res.pages == 3
    assert "| Revenue | 1,234 | 1,100 |" in res.markdown
    assert sorted(p.name for p in tmp_path.iterdir()) == ["locked.pdf", "src.pdf"]  # no decrypted copy


def test_owner_password_only_opens_without_password(tmp_path: Path, pdfspine) -> None:
    pdf = _save(pdfspine.open(write_pdf(tmp_path / "src.pdf")), tmp_path / "owner.pdf",
                encryption=pdfspine.PDF_ENCRYPT_AES_128, user_pw="", owner_pw="owner-pw")
    res = ext.convert_pdf(pdf)
    assert res.status == ext.OK and res.encrypted and not res.password_used


def test_markdown_stats() -> None:
    md = ("<!-- page: 1 -->\n\n# Title\n\nSome text\n\n<!-- page: 2 -->\n\n\n\n<!-- page: 3 -->\n\n"
          "## Table\n\n| a | b |\n| --- | :---: |\n| 1 | 2 |\n| 3 | 4 |\n")
    assert ext.markdown_stats(md) == {"pages": 3, "empty_pages": 1, "tables": 1, "table_rows": 3,
                                      "headings": 2, "chars": len("# Title\n\nSome text") + len(
                                          "## Table\n\n| a | b |\n| --- | :---: |\n| 1 | 2 |\n| 3 | 4 |")}
    assert ext.markdown_stats("plain text, no markers")["pages"] == 1


def test_missing_pdfspine_says_how_to_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pdfspine", None)
    with pytest.raises(ImportError, match=r"superindex\[pdfspine\]"):
        ext.convert_pdf(Path("any.pdf"))


@pytest.mark.parametrize("name", ["pdfspine_ingest.ipynb", "batch_qa.ipynb"])
def test_notebook_cells_compile(name: str) -> None:
    nb = json.loads((ROOT / "notebooks" / name).read_text(encoding="utf-8"))
    assert nb["nbformat"] == 4
    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            ast.parse("".join(line for line in cell["source"] if not line.lstrip().startswith("%")))
    setup = [c for c in nb["cells"] if "databricks-setup" in c.get("metadata", {}).get("tags", [])]
    assert setup and "".join(setup[0]["source"]).startswith("%pip install")
    if name == "pdfspine_ingest.ipynb":
        assert "pdfspine==0.12.0" in "".join(setup[0]["source"])
