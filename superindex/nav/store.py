"""Two-level index: a corpus tree over directories/files, plus one chapter tree
per document.

On-disk layout (deliberately split so routing stays cheap at scale):

    index_dir/
    ├── manifest.json          dirs + file metadata + summaries  (small, loaded always)
    └── trees/<key>.json       one chapter tree per document      (loaded on demand)

The manifest is the routing index. With a thousand files it is still only a few
hundred KB, so it can be held in memory and re-read per query without trouble.
Chapter trees are big and only needed for the handful of files that survive
routing, so they are fetched lazily by key.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


def file_key(rel_path: str) -> str:
    """Stable, filesystem-safe key for a relative path."""
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


@dataclass
class DirEntry:
    rel_path: str
    name: str
    parent: Optional[str]
    child_dirs: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    summary: str = ""
    # A short retrieval label generated from the contents. Folder names are
    # often chosen for the organisation chart rather than for the documents
    # inside, so routing shows this instead when it is available.
    topic: str = ""
    n_files: int = 0          # recursive
    n_dirs: int = 0           # recursive

    @property
    def n_children(self) -> int:
        return len(self.child_dirs) + len(self.files)


@dataclass
class FileEntry:
    rel_path: str
    name: str
    parent: str
    ext: str
    size: int = 0
    mtime: float = 0.0
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    n_chapters: int = 0
    max_depth: int = 0
    tree_key: str = ""


@dataclass
class Chapter:
    title: str
    level: int
    start: int            # markdown: 1-based line; pdf: 1-based page
    end: int
    summary: str = ""
    children: list["Chapter"] = field(default_factory=list)

    def walk(self, depth: int = 1):
        yield self, depth
        for c in self.children:
            yield from c.walk(depth + 1)

    def flatten(self) -> list["Chapter"]:
        return [n for n, _ in self.walk()]

    @staticmethod
    def from_dict(d: dict) -> "Chapter":
        return Chapter(
            title=d.get("title", ""),
            level=int(d.get("level", 1)),
            start=int(d.get("start", 1)),
            end=int(d.get("end", 1)),
            summary=d.get("summary", ""),
            children=[Chapter.from_dict(c) for c in (d.get("children") or [])],
        )

    def to_dict(self) -> dict:
        out = {"title": self.title, "level": self.level,
               "start": self.start, "end": self.end}
        if self.summary:
            out["summary"] = self.summary
        if self.children:
            out["children"] = [c.to_dict() for c in self.children]
        return out


@dataclass
class Manifest:
    root: str = ""
    built_at: float = 0.0
    # Which tree-building logic produced these trees. Bumping it invalidates
    # every cached tree, so improving extraction cannot silently leave old
    # shapes in place just because the source files did not change.
    builder_version: int = 0
    dirs: dict[str, DirEntry] = field(default_factory=dict)
    files: dict[str, FileEntry] = field(default_factory=dict)

    # ---- persistence ----------------------------------------------------
    def save(self, index_dir: Path) -> None:
        index_dir = Path(index_dir)
        (index_dir / "trees").mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "root": self.root,
            "built_at": self.built_at,
            "builder_version": self.builder_version,
            "dirs": {k: asdict(v) for k, v in self.dirs.items()},
            "files": {k: asdict(v) for k, v in self.files.items()},
        }
        _atomic_write(index_dir / "manifest.json", payload)

    @staticmethod
    def load(index_dir: Path) -> "Manifest":
        raw = json.loads((Path(index_dir) / "manifest.json").read_text(encoding="utf-8"))
        m = Manifest(root=raw.get("root", ""),
                     built_at=raw.get("built_at", 0.0),
                     builder_version=int(raw.get("builder_version", 0)))
        for k, v in (raw.get("dirs") or {}).items():
            m.dirs[k] = DirEntry(**v)
        for k, v in (raw.get("files") or {}).items():
            m.files[k] = FileEntry(**v)
        return m

    def save_tree(self, index_dir: Path, key: str, chapters: list[Chapter],
                  raw_lines: list[str] | None = None) -> None:
        body: dict[str, Any] = {"chapters": [c.to_dict() for c in chapters]}
        if raw_lines is not None:
            body["lines"] = raw_lines
        _atomic_write(Path(index_dir) / "trees" / f"{key}.json", body)

    def load_tree(self, index_dir: Path, key: str) -> tuple[list[Chapter], list[str]]:
        p = Path(index_dir) / "trees" / f"{key}.json"
        if not p.is_file():
            return [], []
        body = json.loads(p.read_text(encoding="utf-8"))
        return ([Chapter.from_dict(c) for c in body.get("chapters") or []],
                body.get("lines") or [])

    # ---- convenience ----------------------------------------------------
    def children_of(self, rel_path: Optional[str]) -> tuple[list[DirEntry], list[FileEntry]]:
        """Direct sub-dirs and files of a directory (None = root)."""
        dirs = [d for d in self.dirs.values() if d.parent == rel_path]
        files = [f for f in self.files.values() if f.parent == rel_path]
        dirs.sort(key=lambda d: d.name)
        files.sort(key=lambda f: f.name)
        return dirs, files

    def get_dir(self, rel_path: str) -> Optional[DirEntry]:
        return self.dirs.get(rel_path)

    def get_file(self, rel_path: str) -> Optional[FileEntry]:
        return self.files.get(rel_path)


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
