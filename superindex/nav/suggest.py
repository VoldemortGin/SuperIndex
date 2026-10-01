#!/usr/bin/env python3
"""
Example questions a corpus can actually answer — the chips above the input box.

Why these are generated rather than written by hand
---------------------------------------------------
A hand-written demo question ("2024 年全年股息是多少？") is only useful for the
corpus it was written for. Ship it against a different corpus and it teaches the
user to ask something the index cannot answer — which reads as a *retrieval*
failure when it is really a *suggestion* failure. That is the worst kind of
first impression: the product looks broken on the one question it proposed.

So the questions are derived from the corpus itself, using the same evidence
routing uses: the corpus description, the directory topics, file summaries, and
section titles. Section titles are the strongest signal — "股息", "新业务价值",
"分市场表现" are literally the names of things that can be asked about.

Cost and failure
----------------
One call per corpus, cached in the registry and invalidated by the same content
fingerprint that invalidates the corpus summary. On any failure this returns an
empty list: the UI hides the chips. **No question is better than a wrong
question**, because a wrong one wastes the user's first click and makes the
index look worse than it is.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from superindex.nav import llm
from superindex.nav.debuglog import error as log_error
from superindex.nav.store import Manifest

DEFAULT_LIMIT = 3
TOKENS_SUGGEST = 800

# A "question" longer than this is a paragraph, and a chip that wraps to three
# lines is worse than no chip. Chinese questions run ~20-40 characters.
MAX_QUESTION_CHARS = 80

# Must contain a Latin letter or a CJK character. Rejects "?" and "2024？" —
# punctuation and bare numbers are not questions, and a chip reading "?" is
# worse than no chip.
_HAS_LETTER = re.compile(r"[A-Za-z\u4e00-\u9fff]")

# Trailing connector means the text trailed off: "好的，" is a fragment of a
# preamble the model forgot to finish, not a question.
_FRAGMENT_END = re.compile(r"[，,、;；:：]$")

# Chinese and English question intent. A reply that has neither this nor a
# terminal "？" is prose — most often a preamble like "好的" or "以下是".
_QUESTION_INTENT = re.compile(
    r"多少|几|什么|哪|如何|怎么|为什么|是否|有没有|谁|何时|对比|差异|区别|分别|同比|"
    r"\b(?:what|which|how|when|why|where|who|whom|whose|"
    r"is|are|was|were|does|do|did|can|could|should|compare)\b",
    re.I,
)

# How much of the corpus to show the model. Bounded so the prompt stays cheap
# on a thousand-file corpus — the point is to convey what *kind* of material
# this is, not to enumerate it.
MAX_DIRS = 10
MAX_FILES = 8
MAX_TITLES_PER_FILE = 12
MAX_TITLES = 30


def _clean_question(raw: str) -> str:
    """Normalise one model-authored question, or return "" if unusable.

    Dropping is deliberate. A question that is not actually a question is the
    one failure the user sees directly — it is the first thing on the screen
    and the thing they will click. An empty slot costs nothing; the UI hides
    the chips and the other two still appear.
    """
    q = " ".join((raw or "").split()).strip()
    # Strip a list marker if there is one: "- ", "* ", "1. ", "2) ", "3、".
    # The digit form *requires* the punctuation, because a leading year is the
    # most valuable part of a question — "2024 年 VONB 是多少？" must not lose
    # its "2024" to a bullet-stripper that treats any digit as a marker.
    q = re.sub(r"^(?:[-*·•]|\(?\d{1,2}[.)、])\s*", "", q).strip()
    q = q.strip('"“”\'‘’')

    if not q or len(q) > MAX_QUESTION_CHARS:
        return ""
    if not _HAS_LETTER.search(q):
        return ""
    # A question that contains another question is two questions.
    if "？" in q[:-1] or "?" in q[:-1]:
        return ""
    if _FRAGMENT_END.search(q):
        return ""
    # Needs question intent: either it already ends in a question mark, or it
    # uses a question word. Prose like "好的" / "以下是" has neither.
    if not q.endswith(("？", "?")) and not _QUESTION_INTENT.search(q):
        return ""

    q = q.rstrip("。.")
    if not q.endswith(("？", "?")):
        q += "？"
    return q


def parse_questions(raw: Any, limit: int = DEFAULT_LIMIT) -> list[str]:
    """Pull a usable question list out of whatever the model replied.

    Tolerates the three shapes we actually see: the documented
    `{"questions": [...]}`, a bare JSON array, and a single string. Anything
    else yields an empty list rather than a guess.
    """
    items: Any = raw
    if isinstance(raw, dict):
        for key in ("questions", "question", "items", "list"):
            if key in raw:
                items = raw[key]
                break
        else:
            items = []
    if isinstance(items, str):
        items = re.split(r"[\n;；]|(?<=？)\s*|(?<=\?)\s*", items)
    if not isinstance(items, (list, tuple)):
        return []

    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        q = _clean_question(item)
        if q and q not in out:
            out.append(q)
        if len(out) >= limit:
            break
    return out


def _evidence(m: Manifest, index_dir: Path) -> str:
    """What the corpus contains, in the form the model needs to propose questions.

    Ordered most- to least-concrete so that a truncation would drop the least
    useful part: section titles name askable things, directory names do not.
    """
    root = m.dirs.get("")
    if root is None or (not m.files and not root.child_dirs):
        return ""

    sections: list[str] = []

    titles: list[str] = []
    for rp in sorted(m.files)[:3]:
        fe = m.files[rp]
        try:
            chapters, _lines = m.load_tree(Path(index_dir), fe.tree_key)
        except Exception:  # noqa: BLE001 - a missing tree is not fatal here
            continue
        for ch in chapters:
            for node, _depth in ch.walk():
                title = (node.title or "").strip()
                if title and title not in titles:
                    titles.append(title)
        titles = titles[:MAX_TITLES]
    if titles:
        sections.append("章节标题（最具体，说明可以问什么）:\n  "
                        + "、".join(titles[:MAX_TITLES]))

    if m.files:
        lines = []
        for rp in sorted(m.files)[:MAX_FILES]:
            fe = m.files[rp]
            summary = (fe.summary or "").strip().replace("\n", " ")[:160]
            lines.append(f"  {rp}" + (f" — {summary}" if summary else ""))
        sections.append(f"文件与摘要:\n" + "\n".join(lines))

    if root.child_dirs:
        lines = []
        for rp in sorted(root.child_dirs)[:MAX_DIRS]:
            d = m.dirs[rp]
            label = (d.topic or d.name or rp).strip()
            summary = (d.summary or "").strip().replace("\n", " ")[:100]
            lines.append(f"  {rp}/ ({label})" + (f" — {summary}" if summary else ""))
        sections.append("目录:\n" + "\n".join(lines))

    return "\n\n".join(sections)


def corpus_suggestions(m: Manifest, index_dir: "str | Path", model: str, *,
                       name: str = "", summary: str = "",
                       limit: int = DEFAULT_LIMIT) -> list[str]:
    """Up to `limit` questions this corpus can answer. Never raises."""
    evidence = _evidence(m, Path(index_dir))
    if not evidence:
        return []

    head = f"库名: {name}\n" if name else ""
    if summary:
        head += f"库说明: {summary}\n"

    prompt = (
        "下面是一个文档库的索引信息。请写出 "
        f"{limit} 个**这个库里的文档确实能回答**的问题，"
        "用于放在搜索框里作为示例，让用户知道可以问什么。只输出 JSON。\n\n"
        f"{head}\n{evidence}\n\n"
        "要求：\n"
        f"- 输出形如 {{\"questions\": [\"...\", \"...\", \"...\"]}}，共 {limit} 个\n"
        "- 每个问题都要能被上面的内容回答：主体、报告期、指标都必须出现过\n"
        "- 三个问题要有区分度：不要都是同一指标的同一年度\n"
        "- 一句话，不超过 40 字，用中文提问\n"
        "- 不要把目录名或文件名直接搬进问题，不要出现「根据文档」这类废话\n"
    )
    try:
        ans = llm.chat_json(prompt, model=model, max_tokens=TOKENS_SUGGEST)
    except Exception as exc:  # noqa: BLE001
        # Logged, not raised: the caller is mid-index and the UI already
        # handles "no suggestions" by hiding the chips.
        log_error(exc, where="suggest.corpus", corpus=name)
        print(f"    ! 示例问题生成失败: {exc}")
        return []
    return parse_questions(ans, limit)


__all__ = ["DEFAULT_LIMIT", "MAX_QUESTION_CHARS", "corpus_suggestions",
           "parse_questions"]
