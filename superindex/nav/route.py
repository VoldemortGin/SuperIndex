#!/usr/bin/env python3
"""
Two-level navigation over the index built by nav.build.

    python -m superindex.nav.route index/ "2024 年港湾人寿的 VONB 和 OPAT 分别是多少？"
    python -m superindex.nav.route index/ "..." --show-content
    python -m superindex.nav.route index/ "..." --json

Level 1 walks the corpus to pick candidate files; level 2 walks each candidate's
chapter tree to pick sections; then the section text is returned.

Design notes that matter for robustness at scale:

* The **directory tree is far smaller than the file count**, so level 1 shows the
  whole directory tree at once (bounded by directory count) and lets the model
  choose target directories. Only then are files listed. That is two LLM calls
  instead of one-per-level, and it does not degrade when a folder holds hundreds
  of files.
* If the directory count itself exceeds the budget, level 1 falls back to
  descending one level at a time.
* Both prompts state that a selection is **required** when candidates exist. An
  earlier version let the model return an empty list "when nothing is relevant"
  and it did so unpredictably, aborting navigation on questions that were
  plainly answerable. There is also a deterministic fallback if it still
  returns nothing.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from superindex.nav import llm
from superindex.nav.store import Chapter, FileEntry, Manifest

DIR_TREE_BUDGET = 240        # dirs shown at once in level 1
FILE_BUDGET = 80             # files shown in one listing
CHAPTER_BUDGET = 90          # chapter nodes shown in one listing
DEFAULT_CONTENT_CHARS = 6000
TEXT_EXT = {".md", ".markdown", ".txt"}


@dataclass
class Step:
    level: str
    where: str
    detail: str
    picked: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Result:
    question: str
    files: list[FileEntry] = field(default_factory=list)
    sections: list[tuple[FileEntry, Chapter]] = field(default_factory=list)
    trace: list[Step] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def year_hints(question: str) -> list[str]:
    """Years mentioned in the question — the strongest routing signal for reports."""
    return re.findall(r"(?:19|20)\d{2}", question)


class Navigator:
    def __init__(self, index_dir: str | Path, model: str = llm.DEFAULT_MODEL,
                 effort: str = llm.DEFAULT_EFFORT, verbose: bool = True):
        self.index_dir = Path(index_dir)
        self.m = Manifest.load(self.index_dir)
        self.model = model
        self.effort = effort
        self.verbose = verbose

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # ------------------------------------------------------- level 1: files
    def find_files(self, question: str, top_n: int = 5
                   ) -> tuple[list[FileEntry], list[Step]]:
        dirs = [d for d in self.m.dirs.values() if d.rel_path]
        trace: list[Step] = []
        if len(dirs) <= DIR_TREE_BUDGET:
            picked_dirs, st = self._pick_dirs_from_tree(question, dirs)
            trace += st
        else:
            picked_dirs, st = self._descend_dirs(question, top_n)
            trace += st

        pool = self._files_under(picked_dirs)
        if not pool:
            pool = list(self.m.files.values())
            trace.append(Step(level="dir", where="(全部)", detail="fallback",
                              note="选中目录下无文件，回退到全量"))
        files, st = self._pick_files(question, pool, top_n)
        trace += st
        return files, trace

    def _pick_dirs_from_tree(self, question: str, dirs: list) -> tuple[list[str], list[Step]]:
        """Show the whole directory tree, let the model choose targets."""
        by_path = {d.rel_path: d for d in dirs}
        lines = []
        ids = []
        for d in sorted(dirs, key=lambda x: x.rel_path):
            depth = d.rel_path.count("/")
            pad = "  " * depth
            ids.append(d.rel_path)
            i = len(ids) - 1
            label = f"{pad}[D{i}] {d.name}/  ({d.n_files} 文件, {d.n_dirs} 子目录)"
            if d.summary:
                label += f" — {d.summary}"
            lines.append(label)
        listing = "\n".join(lines)
        years = year_hints(question)
        hint = f"\n注意：问题提到的年份是 {', '.join(years)}，目录路径或摘要必须与之匹配。" if years else ""

        prompt = (
            "你在一个多级目录的知识库里定位文件所在的目录。只输出 JSON。\n\n"
            f"用户问题: {question}{hint}\n\n"
            f"目录树（{len(dirs)} 个目录，缩进表示层级）:\n{listing}\n\n"
            "任务：选出最可能包含答案的目录编号（D 开头）。\n"
            "规则：\n"
            "- 选最具体的目录：能直接定位到文件所在的那一层\n"
            "- 选 1-4 个，按相关性排序\n"
            "- **必须至少选 1 个**，从最相关的开始，不要留空\n"
            "- 年份、公司名、报告期必须与问题一致\n\n"
            '输出: {"dirs": [0, 3]}'
        )
        try:
            ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                max_tokens=600)
            idxs = [i for i in _ints(ans.get("dirs")) if 0 <= i < len(ids)]
        except Exception as exc:  # noqa: BLE001
            self._say(f"  ! 目录选择失败: {exc}")
            idxs = []
        picked = [ids[i] for i in idxs]
        if not picked:
            picked = self._fallback_dirs(question, dirs)
            note = "模型未选中，使用关键词回退"
        else:
            note = ""
        st = Step(level="dir", where=f"全树 {len(dirs)} 个目录", detail="tree",
                  picked=[p for p in picked], note=note)
        self._say(f"  [目录] 全树 {len(dirs)} 个 → 选 {len(picked)} 个"
                  + (f"  ({note})" if note else ""))
        for p in picked:
            self._say(f"      {p}/")
        return picked, [st]

    def _descend_dirs(self, question: str, top_n: int) -> tuple[list[str], list[Step]]:
        """Fallback for very large trees: descend one level at a time."""
        frontier, picked, trace, seen = [""], [], [], set()
        for step in range(8):
            dirs, files = [], []
            for dp in frontier:
                cd, cf = self.m.children_of(dp)
                dirs += [d for d in cd if d.rel_path not in seen]
                files += cf
            if not dirs and not files:
                break
            lines, ids = [], []
            for d in dirs:
                ids.append(d.rel_path)
                lines.append(f"[D{len(ids)-1}] {d.name}/  ({d.n_files} 文件)"
                             + (f" — {d.summary}" if d.summary else ""))
            fids = []
            for f in files:
                fids.append(f.rel_path)
                lines.append(f"[F{len(fids)-1}] {f.name}"
                             + (f" — {f.summary}" if f.summary else ""))
            where = " / ".join(p or "/" for p in frontier)
            prompt = (
                "你在一个多级目录的知识库里逐层定位文件。只输出 JSON。\n\n"
                f"用户问题: {question}\n当前目录: {where}\n\n"
                f"当前内容:\n" + "\n".join(lines) + "\n\n"
                "规则：\n"
                "- descend: 要展开的子目录编号（D 开头），1-3 个\n"
                "- pick: 直接选中的文件编号（F 开头），最多 5 个\n"
                "- 若本层有文件且相关，优先 pick；否则必须 descend，不要留空\n\n"
                '输出: {"descend": [0], "pick": []}'
            )
            try:
                ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                    max_tokens=500)
            except Exception as exc:  # noqa: BLE001
                self._say(f"  ! 逐层下钻失败: {exc}")
                break
            desc = [ids[i] for i in _ints(ans.get("descend")) if 0 <= i < len(ids)]
            take = [fids[i] for i in _ints(ans.get("pick")) if 0 <= i < len(fids)]
            note = ""
            if not desc and not take:
                # model abstained. Prefer files sitting right here — falling
                # back to the whole corpus is a much blunter instrument and
                # gets worse as the corpus grows.
                if files:
                    take = [f.rel_path for f in
                            self._fallback_files(question, files, 3)]
                    note = "关键词回退(本层文件)"
                elif dirs:
                    fb = self._fallback_dirs(question, dirs)
                    if not fb and len(dirs) <= 3:
                        fb = [d.rel_path for d in dirs]
                    desc = fb
                    note = "关键词回退(子目录)" if fb else "无候选"
            picked += [p for p in take if p not in picked]
            trace.append(Step(level="dir", where=where, detail="level",
                              picked=desc + take, note=note))
            self._say(f"  [目录 {step+1}] {where} → 进 {len(desc)}, 选 {len(take)}"
                      + (f"  ({note})" if note else ""))
            if picked or not desc:
                break
            seen.update(desc)
            frontier = desc
        return picked, trace

    def _fallback_dirs(self, question: str, dirs: list) -> list[str]:
        """Deterministic backstop: match year / name tokens against the question."""
        years = year_hints(question)
        scored = []
        for d in dirs:
            s = 0
            for y in years:
                if y in d.rel_path:
                    s += 3
            for tok in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", question):
                if tok.lower() in d.rel_path.lower():
                    s += 1
            if s:
                scored.append((s, d.rel_path))
        scored.sort(reverse=True)
        if scored:
            best = scored[0][0]
            return [p for s, p in scored if s == best][:4]
        return []

    def _files_under(self, dir_paths: list[str]) -> list[FileEntry]:
        out, seen = [], set()
        for dp in dir_paths:
            for rp, fe in self.m.files.items():
                if rp in seen:
                    continue
                if rp.startswith(dp + "/"):
                    out.append(fe)
                    seen.add(rp)
        out.sort(key=lambda f: f.rel_path)
        return out

    def _pick_files(self, question: str, pool: list[FileEntry], top_n: int
                    ) -> tuple[list[FileEntry], list[Step]]:
        if not pool:
            return [], []
        detail = "full" if len(pool) <= FILE_BUDGET else "names"
        lines = []
        for i, f in enumerate(pool):
            if detail == "full" and f.summary:
                lines.append(f"[F{i}] {f.rel_path} — {f.summary}")
            else:
                lines.append(f"[F{i}] {f.rel_path}")
        years = year_hints(question)
        hint = (f"\n注意：问题提到的年份是 {', '.join(years)}，文件名或摘要必须与之匹配。"
                if years else "")
        prompt = (
            "你在从候选文件里挑出最可能包含答案的。只输出 JSON。\n\n"
            f"用户问题: {question}{hint}\n\n"
            f"候选文件（{len(pool)} 个）:\n" + "\n".join(lines) + "\n\n"
            "任务：选出最相关的文件编号（F 开头）。\n"
            "规则：\n"
            f"- 选 1-{min(top_n, len(pool))} 个，按相关性排序\n"
            "- **必须至少选 1 个**，不要留空\n"
            "- 报告期（年度/中期）与年份必须和问题一致\n\n"
            '输出: {"files": [0, 2]}'
        )
        try:
            ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                max_tokens=500)
            idxs = [i for i in _ints(ans.get("files")) if 0 <= i < len(pool)]
        except Exception as exc:  # noqa: BLE001
            self._say(f"  ! 文件选择失败: {exc}")
            idxs = []
        picked = [pool[i] for i in idxs[:top_n]]
        note = ""
        if not picked:
            picked = self._fallback_files(question, pool, top_n)
            note = "模型未选中，使用关键词回退"
        st = Step(level="dir", where=f"{len(pool)} 个候选文件", detail=detail,
                  picked=[f.rel_path for f in picked], note=note)
        self._say(f"  [文件] {len(pool)} 个候选 → 选 {len(picked)} 个"
                  + (f"  ({note})" if note else ""))
        for f in picked:
            self._say(f"      {f.rel_path}")
        return picked, [st]

    def _fallback_files(self, question: str, pool: list[FileEntry],
                        top_n: int) -> list[FileEntry]:
        years = year_hints(question)
        scored = []
        for f in pool:
            s = sum(3 for y in years if y in f.rel_path)
            for tok in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}", question):
                if tok.lower() in f.rel_path.lower():
                    s += 1
            scored.append((s, f))
        scored.sort(key=lambda x: -x[0])
        top = [f for s, f in scored[:top_n]]
        return top if scored and scored[0][0] > 0 else [f for _, f in scored[:1]]

    # ---------------------------------------------------- level 2: chapters
    def find_sections(self, question: str, fe: FileEntry, top_n: int = 6
                      ) -> tuple[list[Chapter], list[Step]]:
        chapters, _ = self.m.load_tree(self.index_dir, fe.tree_key)
        flat = [(c, d) for ch in chapters for c, d in ch.walk()]
        if not flat:
            return [], []

        detail = "full" if len(flat) <= CHAPTER_BUDGET else "names"
        lines = []
        for i, (c, depth) in enumerate(flat):
            span = (f"L{c.start}-{c.end}" if fe.ext in TEXT_EXT
                    else f"p{c.start}-{c.end}")
            pad = "  " * (depth - 1)
            if detail == "full" and c.summary:
                lines.append(f"[{i}] {pad}{c.title} ({span}) — {c.summary}")
            else:
                lines.append(f"[{i}] {pad}{c.title} ({span})")
        prompt = (
            "你在定位一份文档里的具体章节。只输出 JSON。\n\n"
            f"用户问题: {question}\n文件: {fe.rel_path}\n\n"
            f"章节列表（缩进表示层级，{len(flat)} 个节点）:\n"
            + "\n".join(lines) + "\n\n"
            "任务：选出最可能包含答案的章节编号。\n"
            "规则：\n"
            "- 返回**最具体**的章节，不要返回它的父章节\n"
            f"- 选 1-{top_n} 个，按相关性排序\n"
            "- **必须至少选 1 个**，不要留空\n"
            "- 涉及具体数值时，优先含表格或数字明细的章节\n\n"
            '输出: {"sections": [3, 7]}'
        )
        try:
            ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                max_tokens=400)
            idxs = [i for i in _ints(ans.get("sections")) if 0 <= i < len(flat)]
        except Exception as exc:  # noqa: BLE001
            self._say(f"  ! 章节定位失败 ({fe.name}): {exc}")
            idxs = []
        chosen = [flat[i][0] for i in idxs[:top_n]]
        note = ""
        if not chosen:
            chosen = self._fallback_sections(question, flat, top_n)
            note = "模型未选中，使用关键词回退"
        st = Step(level="chapter", where=fe.rel_path, detail=detail,
                  picked=[c.title for c in chosen], note=note)
        self._say(f"  [章节] {fe.name} ({len(flat)} 节点) → "
                  + (", ".join(c.title[:22] for c in chosen) or "无")
                  + (f"  ({note})" if note else ""))
        return chosen, [st]

    def _fallback_sections(self, question: str, flat, top_n: int) -> list[Chapter]:
        toks = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{3,}|\d{4}", question)
        scored = []
        for c, _ in flat:
            s = sum(1 for t in toks if t.lower() in c.title.lower())
            if c.summary:
                s += sum(0.5 for t in toks if t.lower() in c.summary.lower())
            scored.append((s, c))
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_n]]

    # ------------------------------------------------------------- content
    def get_content(self, fe: FileEntry, chapter: Chapter,
                    max_chars: int = DEFAULT_CONTENT_CHARS) -> str:
        _, lines = self.m.load_tree(self.index_dir, fe.tree_key)
        if lines:
            return "\n".join(lines[chapter.start - 1:chapter.end])[:max_chars]
        return f"(PDF: {fe.name} 第 {chapter.start}-{chapter.end} 页)"

    # ------------------------------------------------------------ pipeline
    def run(self, question: str, max_files: int = 5, max_sections: int = 6) -> Result:
        res = Result(question=question)
        self._say(f"\n问题: {question}")
        self._say(f"索引: {len(self.m.files)} 文件 / {len(self.m.dirs) - 1} 目录")
        res.files, t1 = self.find_files(question, top_n=max_files)
        res.trace += t1
        if not res.files:
            res.notes.append("未定位到任何文件")
            return res
        for fe in res.files:
            secs, t2 = self.find_sections(question, fe, top_n=max_sections)
            res.trace += t2
            res.sections += [(fe, s) for s in secs]
        return res


def _ints(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(m) for m in re.findall(r"\d+", value)]
    out = []
    for v in value:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("index_dir")
    ap.add_argument("question")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    ap.add_argument("--effort", default=llm.DEFAULT_EFFORT)
    ap.add_argument("--max-files", type=int, default=5)
    ap.add_argument("--max-sections", type=int, default=6)
    ap.add_argument("--show-content", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    nav = Navigator(args.index_dir, model=args.model, effort=args.effort)
    res = nav.run(args.question, max_files=args.max_files,
                  max_sections=args.max_sections)

    if args.json:
        print(json.dumps({
            "question": res.question,
            "files": [f.rel_path for f in res.files],
            "sections": [{"file": f.rel_path, "title": c.title,
                          "start": c.start, "end": c.end} for f, c in res.sections],
            "notes": res.notes,
        }, ensure_ascii=False, indent=2))
        return 0

    print("\n" + "=" * 78)
    print("定位结果")
    print("=" * 78)
    if not res.files:
        print("  未找到相关文件")
    for fe, ch in res.sections:
        print(f"\n  {fe.rel_path}")
        print(f"    § {ch.title}   ({ch.start}-{ch.end})")
        if ch.summary:
            print(f"      {ch.summary[:120]}")
        if args.show_content:
            print("      ---")
            for ln in nav.get_content(fe, ch).split("\n")[:14]:
                print(f"      {ln}")
    if res.files and not res.sections:
        for f in res.files:
            print(f"\n  {f.rel_path}  (未定位到具体章节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
