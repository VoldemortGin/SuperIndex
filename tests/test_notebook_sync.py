"""The PERSIST_DIR sync cell of notebooks/batch_qa.ipynb: only changed files
are copied, and doc dirs deleted locally are only reported (never deleted) in PERSIST_DIR.

    pytest tests/test_notebook_sync.py
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _sync_cell(work: Path, persist: Path) -> dict[str, Any]:
    cells = json.loads((ROOT / "notebooks" / "batch_qa.ipynb").read_text(encoding="utf-8"))["cells"]
    source = next("".join(c["source"]) for c in cells if "def sync_to_persist" in "".join(c["source"]))
    source = source.replace("\nrestore_from_persist()", "\n")
    ns: dict[str, Any] = {"Path": Path, "os": os, "shutil": shutil,
                          "WORK_DIR": work, "PERSIST_DIR": persist}
    exec(source, ns)  # noqa: S102 - the notebook's own cell
    return ns


def test_sync_copies_only_changed_files(tmp_path: Path, capsys: Any) -> None:
    work, persist = tmp_path / "work", tmp_path / "persist"
    for name in ("a", "b"):
        path = work / "store" / "docs" / f"pi-{name}" / "doc.json"
        path.parent.mkdir(parents=True)
        path.write_text(name, encoding="utf-8")
    ns = _sync_cell(work, persist)
    ns["sync_to_persist"]("first")
    assert "复制 2/2" in capsys.readouterr().out
    ns["sync_to_persist"]("again")
    assert "复制 0/2" in capsys.readouterr().out

    changed = work / "store" / "docs" / "pi-a" / "doc.json"
    changed.write_text("aa", encoding="utf-8")
    shutil.rmtree(work / "store" / "docs" / "pi-b")
    ns["sync_to_persist"]("changed")
    out = capsys.readouterr().out
    assert "复制 1/1" in out and "1 个本地已不存在的旧文档目录（pi-b" in out and "未删除" in out
    assert (persist / "store" / "docs" / "pi-a" / "doc.json").read_text(encoding="utf-8") == "aa"
    assert (persist / "store" / "docs" / "pi-b" / "doc.json").is_file()   # nothing deleted


def test_restore_then_sync_copies_nothing(tmp_path: Path, capsys: Any) -> None:
    work, persist = tmp_path / "work", tmp_path / "persist"
    (persist / "md").mkdir(parents=True)
    (persist / "md" / "x.md").write_text("x", encoding="utf-8")
    ns = _sync_cell(work, persist)
    ns["restore_from_persist"]()
    assert (work / "md" / "x.md").read_text(encoding="utf-8") == "x"
    ns["sync_to_persist"]("after restore")
    assert "复制 0/1" in capsys.readouterr().out
