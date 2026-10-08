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
import contextvars
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from superindex.nav import llm
from superindex.nav.debuglog import error as log_error
from superindex.nav.policy import (
    POLICY_CANDIDATES, POLICY_ENV, PolicyError, RoutingPolicy,
)
from superindex.nav.store import Chapter, DirEntry, FileEntry, Manifest

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


# Output budgets. These are caps, not charges — a reply that fits costs the same
# either way, so the only thing a tight cap buys is a truncation. Reasoning
# models count their thinking against the cap, which is why the old 400-600
# values were fragile: one long reasoning chain and the call returned
# finish_reason=length with no content. nav/llm.chat() now escalates on
# truncation, but starting with enough room avoids paying for the retry.
TOKENS_ROUTE = int(os.getenv("NAV_TOKENS_ROUTE", "1500"))
TOKENS_SECTION = int(os.getenv("NAV_TOKENS_SECTION", "1200"))

def year_hints(question: str) -> list[str]:
    """Years mentioned in the question — the strongest routing signal for reports.

    This is the *built-in* period extractor. A `RoutingPolicy` extends it with
    business-specific period syntax (`FY24`, `2024H1`, `Q3`) and is what the
    navigator actually calls; with an empty policy the two are identical.
    """
    return re.findall(r"(?:19|20)\d{2}", question)


def _bind_policy(policy: Optional[RoutingPolicy], corpus: str = "") -> RoutingPolicy:
    """Resolve the policy a navigator will use, never raising.

    A broken policy file must not stop a query — a typo in YAML should degrade
    to the built-in defaults and leave a trace in the error log, not take the
    server down. That is the whole reason `PolicyError` exists as a distinct
    type: it is catchable here and nowhere else.
    """
    if policy is None:
        try:
            policy = RoutingPolicy.load()
        except PolicyError as exc:
            log_error(exc, where="policy.load")
            print(f"  ! 路由策略加载失败，退回默认行为: {exc}", flush=True)
            policy = RoutingPolicy()
    return policy.flatten_for(corpus) if corpus else policy


def _question_terms(question: str) -> list[tuple[str, int]]:
    """(term, weight) pairs to match a question against a candidate.

    Chinese has no spaces, so `[\u4e00-\u9fff]{2,}` returns whole runs — for
    "友邦保险 2024 年的每股股息是多少" that is "年的每股股息是多少", which will
    never appear verbatim in a summary. Matching therefore also uses **bigrams**
    of each run, which is what makes the fallback work for Chinese at all.

    The whole run is kept too, at a higher weight, so an exact phrase still
    outranks an accidental two-character overlap.
    """
    out: list[tuple[str, int]] = []
    for run in re.findall(r"[\u4e00-\u9fff]+", question):
        if len(run) <= 3:
            out.append((run, 2))
        else:
            out.append((run, 2))
            out.extend((run[i:i + 2], 1) for i in range(len(run) - 1))
    out.extend((w, 2) for w in re.findall(r"[A-Za-z]{3,}", question))
    return out


def _score_candidate(question: str, path: str, summary: str,
                     years: list[str], topic: str = "", *,
                     weight: int = 0,
                     extra_terms: tuple[tuple[str, int], ...] = ()) -> int:
    """Score one candidate against the question.

    Weighting follows trustworthiness, not convenience:

    * **path** (a folder name someone chose for the org chart) — 1 per term
    * **topic / summary** (derived from the documents themselves) — the term's
      weight, so a whole-phrase hit counts double a bigram overlap
    * **year** anywhere — 3, because a period match is the strongest signal
      available and is what cross-period questions get wrong
    * **weight** — the `RoutingPolicy` bonus, added last and separately

    The fallback exists precisely for the case where folder names are
    unhelpful, so it must not lean on them. The policy bonus is the one
    sanctioned exception, and it comes from a human who *knows* which folder
    names are unhelpful — which is why it is a flat bonus rather than a
    multiplier on the path term.

    `extra_terms` carries alias expansions (see `RoutingPolicy.alias_terms`).
    """
    low_path = path.lower()
    low_text = f"{topic or ''} {summary or ''}".lower()
    score = weight
    for year in years:
        if year in path or year in low_text:
            score += 3
    for term, term_weight in list(_question_terms(question)) + list(extra_terms):
        t = term.lower()
        if t in low_path:
            score += 1
        if t in low_text:
            score += term_weight
    return score


class Navigator:
    def __init__(self, index_dir: str | Path, model: str = llm.DEFAULT_MODEL,
                 effort: str = llm.DEFAULT_EFFORT, verbose: bool = True,
                 policy: Optional[RoutingPolicy] = None, corpus: str = ""):
        self.index_dir = Path(index_dir)
        self.m = Manifest.load(self.index_dir)
        self.model = model
        self.effort = effort
        self.verbose = verbose
        # corpus_id -> display name. Single-corpus navigators leave it empty and
        # paths pass through unchanged; MultiNavigator fills it in.
        self.names: dict[str, str] = {}
        # Business routing knowledge (directory weights, periods, aliases…).
        # `None` means "read config/routing_policy.yaml"; an empty policy is a
        # no-op, so this cannot change behaviour by existing.
        self.policy = _bind_policy(policy, corpus)
        if not corpus and len(self.policy.corpora) == 1:
            # A single-corpus index has no id prefix on its paths, so an overlay
            # written for "the one corpus in the config" has nothing to match
            # against. Binding it here keeps single- and multi-corpus setups on
            # the same code path instead of special-casing lookups later.
            # (Only safe because there is exactly one corpus: with several, an
            # overlay must stay scoped to its own corpus.)
            self.policy = self.policy.flatten_for(self.policy.corpus_keys[0])

    def _say(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _periods(self, question: str) -> list[str]:
        """Reporting periods in the question, policy-extended."""
        return self.policy.periods_in(question)

    def _visible_dirs(self, dirs: list) -> list:
        """Drop directories the policy says must never be searched."""
        if not self.policy.exclude:
            return dirs
        return [d for d in dirs if not self.policy.is_excluded(d.rel_path)]

    def _visible_files(self, files: list) -> list:
        if not self.policy.exclude:
            return files
        return [f for f in files if not self.policy.is_excluded(f.rel_path)]

    def display(self, rel_path: str) -> str:
        """Rel path with the corpus id replaced by its name, for anything the
        model or a human reads. Use `fe.rel_path` only for internal lookups."""
        return display_path(rel_path, self.names)

    def _load_tree(self, fe: FileEntry) -> tuple[list[Chapter], list[str]]:
        """Fetch a document's chapter tree and source lines.

        Overridden by MultiNavigator, which has to route each lookup to the
        index directory that owns the document.
        """
        return self.m.load_tree(self.index_dir, fe.tree_key)

    # ------------------------------------------------------- level 1: files
    def find_files(self, question: str, top_n: int = 5
                   ) -> tuple[list[FileEntry], list[Step]]:
        dirs = self._visible_dirs([d for d in self.m.dirs.values() if d.rel_path])
        trace: list[Step] = []
        if len(dirs) <= DIR_TREE_BUDGET:
            picked_dirs, st = self._pick_dirs_from_tree(question, dirs)
            trace += st
        else:
            picked_dirs, st = self._descend_dirs(question, top_n)
            trace += st

        pool = self._visible_files(self._files_under(picked_dirs))
        if not pool:
            pool = self._visible_files(list(self.m.files.values()))
            trace.append(Step(level="dir", where="(全部)", detail="fallback",
                              note="选中目录下无文件，回退到全量"))
        files, st = self._pick_files(question, pool, top_n)
        trace += st
        return files, trace

    def _pick_dirs_from_tree(self, question: str, dirs: list) -> tuple[list[str], list[Step]]:
        """Show the whole directory tree, let the model choose targets."""
        lines = []
        ids = []
        for d in sorted(dirs, key=lambda x: x.rel_path):
            depth = d.rel_path.count("/")
            pad = "  " * depth
            ids.append(d.rel_path)
            i = len(ids) - 1
            # Show the content-derived topic when we have one: the folder name
            # may say nothing about what is inside.
            shown = d.topic or d.name
            label = f"{pad}[D{i}] {shown}/  ({d.n_files} 文件, {d.n_dirs} 子目录)"
            if d.summary:
                label += f" — {d.summary}"
            # Business priority marker, right next to the candidate it applies to.
            label += self.policy.annotate(d.rel_path)
            lines.append(label)
        listing = "\n".join(lines)
        years = self._periods(question)
        hint = f"\n注意：问题提到的年份是 {', '.join(years)}，目录路径或摘要必须与之匹配。" if years else ""
        guidance = self.policy.prompt_block([d.rel_path for d in dirs])

        prompt = (
            "你在一个多级目录的知识库里定位文件所在的目录。只输出 JSON。\n\n"
            f"用户问题: {question}{hint}\n\n"
            f"目录树（{len(dirs)} 个目录，缩进表示层级）:\n{listing}\n"
            f"{guidance}\n"
            "任务：选出最可能包含答案的目录编号。\n"
            "规则：\n"
            "- 选最具体的目录：能直接定位到文件所在的那一层\n"
            "- 选 1-4 个，按相关性排序\n"
            "- **必须至少选 1 个**，从最相关的开始，不要留空\n"
            "- 年份、公司名、报告期必须与问题一致\n"
            "- 标注了 [优先+N] 的目录是业务上更常被问到的，同等相关时优先选它\n\n"
            '编号只写数字，不要带 D/F 前缀。输出: {"dirs": [0, 3]}'
        )
        try:
            ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                max_tokens=TOKENS_ROUTE, label="route.dirs")
            idxs = [i for i in _ints(ans.get("dirs")) if 0 <= i < len(ids)]
        except Exception as exc:  # noqa: BLE001
            # Log it as well as printing: the server runs with verbose=False,
            # so without this the only visible symptom is "模型未选中" and the
            # actual reason (bad JSON, rate limit, truncation) is lost.
            log_error(exc, where="route.dirs", question=question,
                      dirs=len(dirs), prompt_chars=len(prompt))
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
            dirs = self._visible_dirs(dirs)
            files = self._visible_files(files)
            if not dirs and not files:
                break
            lines, ids = [], []
            for d in dirs:
                ids.append(d.rel_path)
                lines.append(f"[D{len(ids)-1}] {d.topic or d.name}/  ({d.n_files} 文件)"
                             + (f" — {d.summary}" if d.summary else "")
                             + self.policy.annotate(d.rel_path))
            fids = []
            for f in files:
                fids.append(f.rel_path)
                lines.append(f"[F{len(fids)-1}] {f.name}"
                             + (f" — {f.summary}" if f.summary else "")
                             + self.policy.annotate(f.rel_path))
            where = " / ".join(p or "/" for p in frontier)
            guidance = self.policy.prompt_block(
                [d.rel_path for d in dirs] + [f.rel_path for f in files])
            prompt = (
                "你在一个多级目录的知识库里逐层定位文件。只输出 JSON。\n\n"
                f"用户问题: {question}\n当前目录: {where}\n\n"
                f"当前内容:\n" + "\n".join(lines) + "\n"
                f"{guidance}\n"
                "规则：\n"
                "- descend: 要展开的子目录编号，1-3 个（只写数字）\n"
                "- pick: 直接选中的文件编号，最多 5 个（只写数字）\n"
                "- 若本层有文件且相关，优先 pick；否则必须 descend，不要留空\n"
                "- 标注了 [优先+N] 的目录是业务上更常被问到的\n\n"
                '编号只写数字，不要带 D/F 前缀。输出: {"descend": [0], "pick": []}'
            )
            try:
                ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                    max_tokens=TOKENS_SECTION,
                                    label="route.descend")
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
        """Deterministic backstop: match the question against path *and* summary.

        Summaries matter more here than anywhere else — this path runs when the
        model did not pick, so it is the last chance to find a directory whose
        name says nothing about its contents.

        Two policy hooks land here, because this is the one place a *wrong*
        ranking is unrecoverable (the model is out of the loop): alias
        expansions widen what counts as a hit, and directory weights break ties
        in favour of the directories the business actually cares about.
        """
        years = self._periods(question)
        extra = tuple(self.policy.alias_terms(question))
        scored = []
        for d in dirs:
            if self.policy.is_excluded(d.rel_path):
                continue
            s = _score_candidate(question, d.rel_path, d.summary, years, d.topic,
                                 weight=self.policy.weight_for(d.rel_path),
                                 extra_terms=extra)
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
            # `rel_path` (internal, id-prefixed) is what the policy is matched
            # against; `display` is what the model reads. Both are needed, so
            # they are computed separately rather than one derived from the other.
            mark = self.policy.annotate(f.rel_path)
            if detail == "full" and f.summary:
                lines.append(f"[F{i}] {self.display(f.rel_path)} — {f.summary}{mark}")
            else:
                lines.append(f"[F{i}] {self.display(f.rel_path)}{mark}")
        years = self._periods(question)
        hint = (f"\n注意：问题提到的年份是 {', '.join(years)}，文件名或摘要必须与之匹配。"
                if years else "")
        guidance = self.policy.prompt_block([f.rel_path for f in pool])
        prompt = (
            "你在从候选文件里挑出最可能包含答案的。只输出 JSON。\n\n"
            f"用户问题: {question}{hint}\n\n"
            f"候选文件（{len(pool)} 个）:\n" + "\n".join(lines) + "\n"
            f"{guidance}\n"
            "任务：选出最相关的文件编号。\n"
            "规则：\n"
            f"- 选 1-{min(top_n, len(pool))} 个，按相关性排序\n"
            "- **必须至少选 1 个**，不要留空\n"
            "- 报告期（年度/中期）与年份必须和问题一致\n"
            "- 标注了 [优先+N] 的路径是业务上更常被问到的，同等相关时优先选它\n\n"
            '编号只写数字。输出: {"files": [0, 2]}'
        )
        try:
            ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                max_tokens=TOKENS_SECTION, label="route.files")
            idxs = [i for i in _ints(ans.get("files")) if 0 <= i < len(pool)]
        except Exception as exc:  # noqa: BLE001
            log_error(exc, where="route.files", question=question,
                      candidates=len(pool))
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
        """Deterministic backstop over files; path and summary both count."""
        years = self._periods(question)
        extra = tuple(self.policy.alias_terms(question))
        scored = []
        for f in pool:
            s = _score_candidate(question, f.rel_path, f.summary, years,
                                 weight=self.policy.weight_for(f.rel_path),
                                 extra_terms=extra)
            scored.append((s, f))
        scored.sort(key=lambda x: -x[0])
        top = [f for s, f in scored[:top_n]]
        return top if scored and scored[0][0] > 0 else [f for _, f in scored[:1]]

    # ---------------------------------------------------- level 2: chapters
    def find_sections(self, question: str, fe: FileEntry, top_n: int = 6
                      ) -> tuple[list[Chapter], list[Step]]:
        chapters, _ = self._load_tree(fe)
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
            f"用户问题: {question}\n文件: {self.display(fe.rel_path)}\n\n"
            f"章节列表（缩进表示层级，{len(flat)} 个节点）:\n"
            + "\n".join(lines) + "\n\n"
            "任务：选出最可能包含答案的章节编号。\n"
            "规则：\n"
            "- 返回**最具体**的章节，不要返回它的父章节\n"
            f"- 选 1-{top_n} 个，按相关性排序\n"
            "- **必须至少选 1 个**，不要留空\n"
            "- 涉及具体数值时，优先含表格或数字明细的章节\n\n"
            '编号只写数字。输出: {"sections": [3, 7]}'
        )
        try:
            ans = llm.chat_json(prompt, model=self.model, effort=self.effort,
                                max_tokens=TOKENS_SECTION,
                                label="route.sections",
                                tag=self.display(fe.rel_path))
            idxs = [i for i in _ints(ans.get("sections")) if 0 <= i < len(flat)]
        except Exception as exc:  # noqa: BLE001
            log_error(exc, where="route.sections", question=question,
                      file=fe.rel_path)
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
        _, lines = self._load_tree(fe)
        if lines:
            return "\n".join(lines[chapter.start - 1:chapter.end])[:max_chars]
        return f"(PDF: {fe.name} 第 {chapter.start}-{chapter.end} 页)"

    # ------------------------------------------------------------ pipeline
    def run(self, question: str, max_files: int = 5, max_sections: int = 6) -> Result:
        res = Result(question=question)
        self._say(f"\n问题: {question}")
        self._say(f"索引: {len(self.m.files)} 文件 / {len(self.m.dirs) - 1} 目录")
        if not self.policy.is_empty:
            self._say(f"策略: {self.policy.describe()}")
        res.files, t1 = self.find_files(question, top_n=max_files)
        res.trace += t1
        if not res.files:
            res.notes.append("未定位到任何文件")
            return res
        # One section call per file, and they are independent: each reads its
        # own tree file and asks the model a self-contained question. Serially
        # that is N round trips stacked end to end; in parallel it is one.
        # `map` (not `as_completed`) because the ORDER is load-bearing —
        # build_context truncates in relevance order, so a reshuffle would let a
        # marginal section crowd out the best one.
        #
        # Safe to thread: _load_tree() only reads a JSON file (store.py:148),
        # self.policy is frozen, and self.m is read-only after load.
        for fe, (secs, t2) in zip(res.files, self.sections_for_all(
                question, res.files, max_sections)):
            res.trace += t2
            res.sections += [(fe, s) for s in secs]
        return res

    def sections_for_all(self, question: str, files: list,
                         max_sections: int) -> list[tuple[list, list[Step]]]:
        """`find_sections` for every file, in order, concurrently.

        A single file skips the pool entirely — the overhead of a thread would
        exceed the work, and this is the common case on small corpora.
        """
        if len(files) == 1:
            return [self.find_sections(question, files[0], top_n=max_sections)]
        # `ThreadPoolExecutor` does NOT inherit contextvars, so the per-call
        # instrumentation bucket that `llm.record_calls()` sets would be
        # invisible to these workers — and these are precisely the calls that
        # bucket exists to account for. So copy the context per task and run the
        # task inside it.
        #
        # One copy per task, not one shared: `Context.run` refuses to be entered
        # twice at once, so a single copy handed to four workers would raise
        # instead of collecting. The copy must also be taken *here*, on the
        # calling thread — taking it inside the worker would capture that
        # worker's own empty context and find nothing.
        with ThreadPoolExecutor(max_workers=min(len(files), 4)) as pool:
            futs = [pool.submit(contextvars.copy_context().run,
                                self.find_sections, question, fe,
                                top_n=max_sections)
                    for fe in files]
            # Read back in submission order, not completion order — the order
            # is load-bearing (see the note in `run()`), and `f.result()` blocks
            # in exactly the sequence the futures were created.
            return [f.result() for f in futs]


def merge_manifests(corpora: list[tuple[str, "str | Path", str, str]]
                    ) -> tuple[Manifest, dict[str, Path]]:
    """Merge N corpus indexes into one routing view.

    Each corpus becomes a top-level pseudo-directory named by its id, so the
    model sees a single tree and can compare branches across corpora in one
    call — rather than routing each corpus separately and then guessing which
    result is best.

    `corpora` is a list of `(corpus_id, index_dir, display_name, summary)`.
    Returns the merged manifest plus a `corpus_id -> index_dir` map, which is
    what lets `MultiNavigator` find a document's tree file.
    """
    import time as _time

    merged = Manifest(root="(merged)", built_at=_time.time())
    index_dirs: dict[str, Path] = {}

    for cid, index_dir, name, summary in corpora:
        idir = Path(index_dir)
        index_dirs[cid] = idir
        src = Manifest.load(idir)

        merged.dirs[cid] = DirEntry(rel_path=cid, name=name, parent=None,
                                    summary=summary)

        for rp, d in src.dirs.items():
            if rp == "":
                continue                      # the corpus root itself
            nrp = f"{cid}/{rp}"
            parent = f"{cid}/{d.parent}" if d.parent else cid
            merged.dirs[nrp] = DirEntry(rel_path=nrp, name=d.name,
                                        parent=parent, summary=d.summary,
                                        topic=d.topic)
            merged.dirs[parent].child_dirs.append(nrp)

        for rp, f in src.files.items():
            nrp = f"{cid}/{rp}"
            parent = f"{cid}/{f.parent}" if f.parent else cid
            merged.files[nrp] = FileEntry(
                rel_path=nrp, name=f.name, parent=parent, ext=f.ext,
                size=f.size, mtime=f.mtime, summary=f.summary, meta=f.meta,
                n_chapters=f.n_chapters, max_depth=f.max_depth,
                tree_key=f.tree_key)
            merged.dirs[parent].files.append(nrp)

    for rp in sorted(merged.dirs, key=lambda x: -x.count("/")):
        d = merged.dirs[rp]
        d.n_files = len(d.files) + sum(merged.dirs[c].n_files
                                       for c in d.child_dirs)
        d.n_dirs = len(d.child_dirs) + sum(merged.dirs[c].n_dirs
                                           for c in d.child_dirs)
    return merged, index_dirs


class MultiNavigator(Navigator):
    """Navigator over several corpus indexes at once.

    Only overrides how a document's tree file is located: rel paths are
    namespaced `<corpus_id>/...`, so the owning index directory is recoverable
    from the path itself.
    """

    def __init__(self, corpora: list[tuple[str, "str | Path", str, str]],
                 model: str = llm.DEFAULT_MODEL, effort: str = llm.DEFAULT_EFFORT,
                 verbose: bool = True, policy: Optional[RoutingPolicy] = None):
        self._corpora = corpora
        self.model = model
        self.effort = effort
        self.verbose = verbose
        self.index_dir = Path(".")            # unused; kept for the base class
        self.m, self._index_dirs = merge_manifests(corpora)
        self.names = {cid: name for cid, _d, name, _s in corpora}
        # Corpus overlays in the config are keyed by display name, because that
        # is what a human editing the file sees. Paths here are keyed by id, so
        # bind the two once, now, rather than translating on every lookup.
        self.policy = _bind_policy(policy).bind_corpora(self.names)

    def _load_tree(self, fe: FileEntry) -> tuple[list[Chapter], list[str]]:
        cid = fe.rel_path.split("/", 1)[0]
        idir = self._index_dirs.get(cid)
        if idir is None:
            return [], []
        return self.m.load_tree(idir, fe.tree_key)

    @property
    def corpus_ids(self) -> list[str]:
        return [c[0] for c in self._corpora]


def display_path(rel_path: str, names: Optional[dict[str, str]] = None) -> str:
    """`610b3c99/2024/annual/A.md` -> `友邦保险 / 2024/annual/A.md`.

    Rel paths are namespaced with the corpus id so several corpora can share one
    routing tree. That id is an internal address and must never reach the model:
    whatever path appears in the prompt is what the model cites, so it would
    answer with `610b3c99/2024/annual/...` instead of something a human can use.
    """
    if not rel_path or not names:
        return rel_path
    head, sep, tail = rel_path.partition("/")
    name = names.get(head)
    if not name:
        return rel_path
    # The bare corpus id is a valid value on its own — routing picks the corpus
    # level before it picks anything inside it, so `a9e87d72` must render as the
    # corpus name rather than leaking the id.
    return f"{name} / {tail}" if sep else name


def build_context(res: Result, nav: Navigator, *,
                  names: Optional[dict[str, str]] = None,
                  per_section_chars: int = 2600,
                  total_chars: int = 20000) -> tuple[str, list[dict]]:
    """Turn a Result into an answer prompt body plus a source list.

    `names` maps corpus id to display name; pass it so the prompt carries
    readable paths rather than internal ids.

    Truncation is deliberate and budgeted: sections are added in relevance
    order until the total would be exceeded, so a long tail of marginal
    sections cannot crowd out the best one.
    """
    names = names if names is not None else getattr(nav, "names", None)
    blocks: list[str] = []
    sources: list[dict] = []
    used = 0
    for fe, ch in res.sections:
        body = nav.get_content(fe, ch, max_chars=per_section_chars)
        shown = display_path(fe.rel_path, names)
        block = (f"--- source {len(sources) + 1} ---\n"
                 f"file: {shown}\n"
                 f"section: {ch.title}\n"
                 f"lines: {ch.start}-{ch.end}\n\n{body}")
        if used + len(block) > total_chars:
            break
        blocks.append(block)
        used += len(block)
        sources.append({
            "file": fe.rel_path,        # the real address, for follow-up lookups
            "display": shown,           # what to show a human
            "title": ch.title,
            "start": ch.start,
            "end": ch.end,
        })
    return "\n\n".join(blocks), sources


def answer_prompt(question: str, context: str) -> str:
    return (
        "Answer the question using only the sources below.\n"
        "Cite the file and section you used, and state the reporting period any "
        "figure belongs to. If the sources do not contain the answer, say so "
        "plainly instead of guessing.\n\n"
        f"Question: {question}\n\n"
        f"Sources:\n{context or '(no sources were retrieved)'}\n\n"
        "Answer:"
    )


def _ints(value: Any) -> list[int]:
    """Coerce a model's index list into ints, tolerating the label prefixes.

    Prompts label candidates `[D0]`, `[F3]`, `[S1]`… and models write those
    prefixes back. Both a bare `"D8"` and a list `["D8", "D9"]` must yield 8 and
    9 — the single-string branch alone is not enough, and that gap is exactly
    how a perfectly good reply used to collapse into an empty selection.
    """
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        return [int(m) for m in re.findall(r"\d+", value)]

    out: list[int] = []
    for v in value:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out.append(v)
        elif isinstance(v, str):
            out.extend(int(m) for m in re.findall(r"\d+", v))
        else:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("index_dir")
    ap.add_argument("question")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL,
                    help="LiteLLM model (default: NAV_MODEL, else "
                         "SUPERINDEX_CHAT_MODEL, else deepseek/deepseek-flash)")
    ap.add_argument("--effort", default=llm.DEFAULT_EFFORT,
                    help="reasoning_effort (default: NAV_REASONING_EFFORT, else "
                         "'none'; empty sends none)")
    ap.add_argument("--max-files", type=int, default=5)
    ap.add_argument("--max-sections", type=int, default=6)
    ap.add_argument("--show-content", action="store_true")
    ap.add_argument("--json", action="store_true")
    # Business routing policy. Default: config/routing_policy.yaml if present.
    ap.add_argument("--policy", default=None,
                    help=f"路由策略文件（默认 {POLICY_CANDIDATES[0]}，"
                         f"也可用 ${POLICY_ENV}）")
    ap.add_argument("--corpus", default="",
                    help="语料名或 ID，用于套用策略文件里的单语料覆盖")
    ap.add_argument("--show-policy", action="store_true",
                    help="只打印生效的策略然后退出")
    args = ap.parse_args()

    try:
        policy = RoutingPolicy.load(args.policy)
    except PolicyError as exc:
        print(f"策略加载失败: {exc}", file=sys.stderr)
        return 2
    if args.show_policy:
        print(f"策略来源: {policy.source or '(未找到，使用内置默认)'}")
        print(f"策略内容: {policy.describe()}")
        for k, ov in policy.corpora:
            print(f"  语料覆盖 {k}: "
                  f"{len(ov.weights)} 权重 / {len(ov.exclude)} 排除 / "
                  f"{len(ov.scopes)} 业务域")
        return 0

    nav = Navigator(args.index_dir, model=args.model, effort=args.effort,
                    policy=policy, corpus=args.corpus)
    res = nav.run(args.question, max_files=args.max_files,
                  max_sections=args.max_sections)

    if args.json:
        print(json.dumps({
            "question": res.question,
            "policy": {"source": nav.policy.source,
                       "describe": nav.policy.describe()},
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
