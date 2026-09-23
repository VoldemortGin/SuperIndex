#!/usr/bin/env python3
"""
Build the two-level navigation index.

    # 1. structure only — free, no LLM, seconds even for thousands of files
    python -m nav.build /path/to/reports --out index/

    # 2. summaries for routing (file + directory level)   [LLM]
    python -m nav.build /path/to/reports --out index/ --summarize-files

    # 3. summaries for chapter selection                   [LLM]
    python -m nav.build /path/to/reports --out index/ --summarize-chapters

Steps 2 and 3 are incremental: a file is skipped when its size and mtime are
unchanged and it already carries the summary being asked for. Re-running is
therefore cheap and safe.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nav import llm  # noqa: E402
from nav.store import (Chapter, DirEntry, FileEntry, Manifest, file_key)  # noqa: E402

TEXT_EXT = {".md", ".markdown", ".txt"}
PDF_EXT = {".pdf"}
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
BOLD_ONLY_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")


# ---------------------------------------------------------------- chapters
def markdown_chapters(lines: list[str]) -> list[Chapter]:
    """Headings -> a Chapter tree with 1-based inclusive line ranges.

    Handles ATX headings and the common `**Bold line**` convention. A bold line
    is kept at its own level rather than promoted to level 1, so mixed documents
    do not collapse into a flat tree.
    """
    found: list[tuple[int, int, str]] = []
    in_code = False
    for i, raw in enumerate(lines, start=1):
        s = raw.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not s:
            continue
        m = HEADING_RE.match(s)
        if m:
            found.append((i, len(m.group(1)), m.group(2).strip()))
            continue
        b = BOLD_ONLY_RE.match(s)
        if b and len(b.group(1)) <= 80:
            found.append((i, 9, b.group(1).strip()))   # 9 = "bold" marker

    if not found:
        return []

    # A bold line takes the level of the nearest preceding real heading + 1,
    # clamped; that keeps it nested instead of becoming a top-level sibling.
    nodes: list[Chapter] = []
    stack: list[Chapter] = []
    last_real = 0
    for idx, (line, lvl, title) in enumerate(found):
        if lvl == 9:
            level = min(last_real + 1, 6) if last_real else 1
        else:
            level = lvl
            last_real = lvl
        end = len(lines)
        for nxt_line, nxt_lvl, _ in found[idx + 1:]:
            nxt_eff = min(last_real + 1, 6) if nxt_lvl == 9 else nxt_lvl
            if nxt_eff <= level:
                end = nxt_line - 1
                break
        node = Chapter(title=title, level=level, start=line, end=end)
        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            nodes.append(node)
        stack.append(node)
    return nodes


def pdf_chapters(path: Path) -> list[Chapter]:
    """Chapter tree from a PDF's embedded bookmarks, if it has any."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return []
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:  # noqa: BLE001
        return []
    try:
        try:
            outline = doc.get_toc()
        except Exception:  # noqa: BLE001 - API varies across pypdfium2 versions
            return []
        items = []
        for entry in outline or []:
            try:
                level, title, page = int(entry[0]), str(entry[1]).strip(), int(entry[2])
            except (TypeError, ValueError, IndexError):
                continue
            if title:
                items.append((level, title, page))
        if not items:
            return []
        total = len(doc)
        nodes: list[Chapter] = []
        stack: list[Chapter] = []
        for idx, (level, title, page) in enumerate(items):
            end = total
            for nxt_lvl, _, nxt_page in items[idx + 1:]:
                if nxt_lvl <= level:
                    end = max(page, nxt_page - 1)
                    break
            node = Chapter(title=title, level=level, start=page, end=end)
            while stack and stack[-1].level >= level:
                stack.pop()
            (stack[-1].children if stack else nodes).append(node)
            stack.append(node)
        return nodes
    finally:
        doc.close()


def read_document(path: Path) -> tuple[list[Chapter], list[str]]:
    """Return (chapter tree, source lines) for one file."""
    if path.suffix.lower() in PDF_EXT:
        return pdf_chapters(path), []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []
    lines = text.split("\n")
    return markdown_chapters(lines), lines


# ------------------------------------------------------------------- scan
def scan(root: Path, includes: set[str], excludes: set[str],
         max_files: Optional[int] = None
         ) -> tuple[Manifest, dict[str, tuple[list[Chapter], list[str]]]]:
    root = root.resolve()
    m = Manifest(root=str(root), built_at=__import__("time").time())
    trees: dict[str, tuple[list[Chapter], list[str]]] = {}

    def rel(p: Path) -> str:
        r = p.relative_to(root).as_posix()
        return "" if r == "." else r        # the root itself is ""

    def skip(p: Path) -> bool:
        parts = set(p.relative_to(root).parts)
        return bool(parts & excludes)

    m.dirs[""] = DirEntry(rel_path="", name=root.name or "/", parent=None)

    count = 0
    for dirpath, dirnames, filenames in __import__("os").walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not skip(here / d))
        if skip(here):
            continue
        rp = rel(here) if here != root else ""
        entry = m.dirs.setdefault(rp, DirEntry(rel_path=rp, name=here.name,
                                               parent=None if rp == "" else rel(here.parent)))
        entry.parent = None if rp == "" else rel(here.parent)
        for d in dirnames:
            crp = rel(here / d)
            m.dirs.setdefault(crp, DirEntry(rel_path=crp, name=d, parent=rp))
            entry.child_dirs.append(crp)
        for fn in sorted(filenames):
            fpath = here / fn
            if skip(fpath) or fpath.suffix.lower() not in includes:
                continue
            if max_files is not None and count >= max_files:
                continue
            frp = rel(fpath)
            try:
                st = fpath.stat()
            except OSError:
                continue
            chapters, lines = read_document(fpath)
            flat = [c for ch in chapters for c in ch.flatten()]
            fe = FileEntry(
                rel_path=frp, name=fn, parent=rp, ext=fpath.suffix.lower(),
                size=st.st_size, mtime=st.st_mtime,
                n_chapters=len(flat),
                max_depth=max((c.level for c in flat), default=0),
                tree_key=file_key(frp),
            )
            m.files[frp] = fe
            entry.files.append(frp)
            trees[frp] = (chapters, lines)
            count += 1

    # recursive counts, deepest first
    for rp in sorted(m.dirs, key=lambda x: -x.count("/")):
        d = m.dirs[rp]
        d.n_files = len(d.files) + sum(m.dirs[c].n_files for c in d.child_dirs)
        d.n_dirs = len(d.child_dirs) + sum(m.dirs[c].n_dirs for c in d.child_dirs)
    return m, trees


# -------------------------------------------------------------- summaries
def _chapter_outline(chapters: list[Chapter], limit: int = 40) -> str:
    out = []
    for c, depth in (n for ch in chapters for n in ch.walk()):
        out.append(f"{'  ' * (depth - 1)}- {c.title}")
        if len(out) >= limit:
            out.append("  ...")
            break
    return "\n".join(out)


def summarize_files(m: Manifest, index_dir: Path, model: str, workers: int,
                    force: bool = False) -> int:
    """One line per file, then one per directory (bottom-up)."""
    todo = [f for f in m.files.values() if force or not f.summary]
    print(f"  文件摘要: {len(todo)} 待生成 / {len(m.files)} 总数")

    def one(fe: FileEntry) -> tuple[str, str]:
        chapters, lines = m.load_tree(index_dir, fe.tree_key)
        head = ""
        if lines:
            head = " ".join(" ".join(lines[:60]).split())[:1200]
        prompt = (
            "用一句中文概括这份文件讲什么，并给出可检索的关键信息"
            "（报告期、主体、核心指标名称）。只输出这句话，不要解释。\n\n"
            f"文件名: {fe.name}\n"
            f"章节结构:\n{_chapter_outline(chapters)}\n\n"
            f"开头内容:\n{head}"
        )
        try:
            return fe.rel_path, llm.chat(prompt, model=model, max_tokens=200).strip()
        except Exception as exc:  # noqa: BLE001
            print(f"    ! {fe.rel_path}: {exc}")
            return fe.rel_path, ""

    if todo:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(one, fe): fe for fe in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                rp, s = fut.result()
                if s:
                    m.files[rp].summary = s
                if i % 25 == 0 or i == len(todo):
                    print(f"    {i}/{len(todo)}")

    # directories: deepest first so children are ready
    dirs_todo = [d for d in m.dirs.values()
                 if d.rel_path != "" and (force or not d.summary)]
    dirs_todo.sort(key=lambda d: -d.rel_path.count("/"))
    print(f"  目录摘要: {len(dirs_todo)} 待生成")
    for i, d in enumerate(dirs_todo, 1):
        kids = [m.dirs[c] for c in d.child_dirs] + [m.files[f] for f in d.files]
        listing = "\n".join(f"- {k.name}: {k.summary or '(无摘要)'}" for k in kids[:60])
        prompt = (
            "用一句中文概括这个目录里都有什么内容，便于检索时判断是否相关。"
            "只输出这句话。\n\n"
            f"目录名: {d.name}\n共 {d.n_files} 个文件、{d.n_dirs} 个子目录\n"
            f"直接内容:\n{listing}"
        )
        try:
            d.summary = llm.chat(prompt, model=model, max_tokens=200).strip()
        except Exception as exc:  # noqa: BLE001
            print(f"    ! {d.rel_path}: {exc}")
        if i % 25 == 0 or i == len(dirs_todo):
            print(f"    {i}/{len(dirs_todo)}")
    return len(todo)


def summarize_chapters(m: Manifest, index_dir: Path, model: str, workers: int,
                       force: bool = False) -> int:
    """One line per chapter node, leaves first."""
    targets = []
    for fe in m.files.values():
        if fe.n_chapters == 0:
            continue
        chapters, lines = m.load_tree(index_dir, fe.tree_key)
        if not chapters:
            continue
        todo = [c for c, _ in (n for ch in chapters for n in ch.walk())
                if force or not c.summary]
        if todo:
            targets.append((fe, chapters, lines, todo))

    total = sum(len(t) for _, _, _, t in targets)
    print(f"  章节摘要: {total} 待生成 / {len(m.files)} 个文件")
    if not total:
        return 0

    done = 0

    def one(args):
        fe, chapters, lines, ch = args
        if lines:
            body = "\n".join(lines[ch.start - 1:min(ch.end, ch.start + 119)])
        else:
            body = f"(PDF 第 {ch.start}-{ch.end} 页)"
        prompt = (
            "用一句中文概括这一节讲了什么，保留可检索的关键信息"
            "（指标名称、数字、期间）。只输出这句话。\n\n"
            f"文件: {fe.name}\n章节: {ch.title}\n\n内容:\n{body[:2400]}"
        )
        try:
            return ch, llm.chat(prompt, model=model, max_tokens=180).strip()
        except Exception as exc:  # noqa: BLE001
            return ch, ""

    jobs = [(fe, chs, ln, c) for fe, chs, ln, todo in targets for c in todo]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, j) for j in jobs]
        for fut in as_completed(futs):
            ch, s = fut.result()
            if s:
                ch.summary = s
            done += 1
            if done % 50 == 0 or done == len(jobs):
                print(f"    {done}/{len(jobs)}")

    for fe, chapters, lines, _ in targets:
        m.save_tree(index_dir, fe.tree_key, chapters, lines if fe.ext in TEXT_EXT else None)
    return total


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="corpus root directory")
    ap.add_argument("--out", default="nav_index", help="index output directory")
    ap.add_argument("--include", default=".md,.markdown,.txt,.pdf")
    ap.add_argument("--exclude", default=".git,node_modules,__pycache__,.venv,index",
                    help="directory names to skip")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--summarize-files", action="store_true")
    ap.add_argument("--summarize-chapters", action="store_true")
    ap.add_argument("--force", action="store_true", help="recompute existing summaries")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    out = Path(args.out)
    includes = {e.strip().lower() for e in args.include.split(",") if e.strip()}
    excludes = {e.strip() for e in args.exclude.split(",") if e.strip()}

    # reuse existing manifest when only summarizing
    existing = out / "manifest.json"
    if existing.is_file() and (args.summarize_files or args.summarize_chapters):
        print(f"载入已有索引 {existing}")
        m = Manifest.load(out)
        fresh, trees = scan(root, includes, excludes, args.max_files)
        added, removed = [], []
        for rp, fe in fresh.files.items():
            if rp in m.files:
                old = m.files[rp]
                fe.summary = old.summary
                fe.meta = old.meta
                changed = (fe.size != old.size or fe.mtime != old.mtime)
                if changed:
                    # content changed: rebuild this tree (its chapter
                    # summaries are stale, so they are dropped with it)
                    chs, lines = trees[rp]
                    m.save_tree(out, fe.tree_key, chs,
                                lines if fe.ext in TEXT_EXT else None)
                    added.append(rp + " (changed)")
            else:
                chs, lines = trees[rp]
                m.save_tree(out, fe.tree_key, chs,
                            lines if fe.ext in TEXT_EXT else None)
                added.append(rp)
            m.files[rp] = fe
        for rp, d in fresh.dirs.items():
            if rp in m.dirs and m.dirs[rp].summary:
                d.summary = m.dirs[rp].summary
            m.dirs[rp] = d
        for rp in list(m.files):
            if rp not in fresh.files:
                removed.append(rp)
                del m.files[rp]
        for rp in added:
            print(f"  + {rp}")
        for rp in removed:
            print(f"  - {rp}")
    else:
        print(f"扫描 {root} ...")
        m, trees = scan(root, includes, excludes, args.max_files)
        for rp, (chapters, lines) in trees.items():
            fe = m.files.get(rp)
            if fe:
                m.save_tree(out, fe.tree_key, chapters,
                            lines if fe.ext in TEXT_EXT else None)
        m.save(out)
        print(f"  目录 {len(m.dirs)}, 文件 {len(m.files)}, "
              f"章节节点 {sum(f.n_chapters for f in m.files.values())}")

    if args.summarize_chapters:
        print("生成章节摘要 ...")
        summarize_chapters(m, out, args.model, args.workers, args.force)
        m.save(out)
    if args.summarize_files:
        print("生成文件与目录摘要 ...")
        summarize_files(m, out, args.model, args.workers, args.force)
        m.save(out)

    m.save(out)
    print(f"\n索引已写入 {out}")
    print(f"  目录 {len(m.dirs)}  文件 {len(m.files)}  "
          f"有摘要文件 {sum(1 for f in m.files.values() if f.summary)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
