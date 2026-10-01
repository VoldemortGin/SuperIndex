#!/usr/bin/env python3
"""
Tests for nav/suggest.py — the example questions shown above the input box.

The LLM is stubbed, so nothing here calls out. Two things matter and are
covered in this order:

1. **Defensive parsing.** The model's reply is the only untyped input in the
   feature, and a malformed question becomes a broken chip. Every shape it has
   actually produced is pinned below, including the ones that must be *dropped*
   rather than repaired.
2. **The empty answer is a valid answer.** Any failure yields `[]` and the UI
   hides the chips. There is no fallback that invents a question, because a
   question the index cannot answer makes the product look broken.

    python tests/test_suggest.py
    pytest tests/test_suggest.py
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from superindex.nav.registry import (  # noqa: E402
    Corpus, Registry, STATUS_READY, _suggestions_fingerprint,
)
from superindex.nav.store import Chapter, DirEntry, FileEntry, Manifest  # noqa: E402
from superindex.nav.suggest import (  # noqa: E402
    MAX_QUESTION_CHARS, _clean_question, _evidence, corpus_suggestions,
    parse_questions,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class StubLLM:
    """Replaces nav.llm.chat_json and records the prompts it was given."""

    def __init__(self, reply=None, *, boom=False):
        self.reply = reply if reply is not None else {
            "questions": ["友邦保险 2024 年的 VONB 是多少？",
                          "中国平安 2023 年全年股息是多少？",
                          "中国太保 2024 年的 OPAT 是多少？"]}
        self.boom = boom
        self.prompts: list[str] = []

    def __enter__(self):
        from superindex.nav import llm
        self._llm = llm
        self._saved = llm.chat_json

        def chat_json(prompt, **_kw):
            self.prompts.append(prompt)
            if self.boom:
                raise RuntimeError("stubbed transport failure")
            return self.reply

        llm.chat_json = chat_json
        return self

    def __exit__(self, *exc):
        self._llm.chat_json = self._saved


def build_manifest(root: Path) -> tuple[Manifest, Path]:
    """A two-period corpus with real section titles."""
    idx = root / "idx"
    m = Manifest(root="demo")
    for name, topic, summary in (("2024", "2024 年度", "2024 年全年报告"),
                                 ("2023", "2023 年度", "2023 年全年报告")):
        m.dirs[name] = DirEntry(rel_path=name, name=name, parent="",
                                topic=topic, summary=summary)
        m.dirs[name].n_files = 1
    m.dirs[""] = DirEntry(rel_path="", name="", parent=None)
    m.dirs[""].child_dirs = ["2023", "2024"]
    for name in ("2023", "2024"):
        rp = f"{name}/annual/report.md"
        m.files[rp] = FileEntry(rel_path=rp, name="report.md", parent=f"{name}/annual",
                                ext=".md", summary=f"{name} 年全年报告，含股息与 VONB",
                                tree_key=name)
        m.save_tree(idx, name, [
            Chapter("财务摘要", 1, 1, 4, "核心指标"),
            Chapter("股息", 1, 5, 9, "全年股息"),
        ], ["# 财务摘要", "x", "y", "z", "# 股息"])
    m.save(idx)
    return m, idx


# ══════════════════════════════════════════════════════════════════════════
# 1. Parsing the model's reply
# ══════════════════════════════════════════════════════════════════════════
def test_reply_shapes() -> None:
    print("\n[回复形状]")
    three = ["A 是多少？", "B 是多少？", "C 是多少？"]
    check("标准 {\"questions\": [...]}",
          parse_questions({"questions": three}) == three)
    check("裸数组", parse_questions(three) == three)
    check("单数键 question", parse_questions({"question": three}) == three)
    check("items 键", parse_questions({"items": three}) == three)
    check("裸字符串按行/分号拆",
          parse_questions("A 是多少？\nB 是多少？；C 是多少？") == three)
    check("字符串里只有一条", parse_questions("A 是多少？") == ["A 是多少？"])

    # Shapes that must NOT be guessed at.
    for junk, label in ((None, "None"), ({}, "空对象"), ({"nope": 1}, "未知键"),
                        (42, "数字"), ([1, 2], "纯数字列表"),
                        ({"questions": "?"}, "只有标点")):
        check(f"拒绝: {label}", parse_questions(junk) == [])


def test_cleaning() -> None:
    print("\n[清洗：该留的留，该丢的丢]")
    c = _clean_question
    # ★ The leading year is the most valuable part of a finance question.
    check("保留开头的年份", c("2024 年 VONB 是多少？") == "2024 年 VONB 是多少？")
    check("保留开头的数字指标", c("150 港仙是多少钱？") == "150 港仙是多少钱？")
    check("补问号", c("友邦 2024 年全年股息是多少") == "友邦 2024 年全年股息是多少？")
    check("句号换成问号", c("友邦 2024 年的 VONB 是多少。") == "友邦 2024 年的 VONB 是多少？")
    check("英文问号保留", c("What is VONB?") == "What is VONB?")
    check("压掉换行与多空格", c("友邦\n  2024 年\nVONB？") == "友邦 2024 年 VONB？")
    check("去引号", c('"友邦的 VONB？"') == "友邦的 VONB？")

    # Bullets: stripped, but only real list markers.
    check("去 - 项目符号", c("- 友邦的 VONB？") == "友邦的 VONB？")
    check("去 1. 项目符号", c("1. 友邦的 VONB？") == "友邦的 VONB？")
    check("去 2) 项目符号", c("2) 友邦的 VONB？") == "友邦的 VONB？")
    check("去 3、项目符号", c("3、友邦的 VONB？") == "友邦的 VONB？")

    # Dropped, not repaired. Each of these became a chip at some point during
    # development, which is why they are pinned individually.
    check("超长丢弃", c("x" * (MAX_QUESTION_CHARS + 1)) == "")
    check("空串丢弃", c("   ") == "" and c("") == "")
    check("两个问题丢弃", c("友邦的 VONB？平安呢？") == "")
    check("纯标点丢弃", c("?") == "" and c("？") == "")
    check("纯数字丢弃", c("2024？") == "" and c("2024 年") == "")
    check("语气词/寒暄丢弃（无提问意图）", c("好的") == "" and c("以下是三个问题") == "")
    check("断句片段丢弃（以逗号结尾）", c("好的，") == "" and c("友邦的 VONB，") == "")

    # …but a real question that merely lacks the trailing mark is still kept.
    check("缺问号但有疑问词 → 补上",
          c("友邦 2024 年全年股息是多少") == "友邦 2024 年全年股息是多少？")
    check("缺问号但有英文疑问词 → 补上",
          c("what is the VONB for 2024") == "what is the VONB for 2024？")

    # Dedupe and cap.
    check("去重", parse_questions(["A？", "A？", "B？"]) == ["A？", "B？"])
    check("按 limit 截断",
          parse_questions(["A？", "B？", "C？", "D？"], limit=2) == ["A？", "B？"])
    check("limit 默认 3", len(parse_questions(["A？", "B？", "C？", "D？"])) == 3)
    check("混入非字符串时跳过",
          parse_questions(["A？", None, 7, {"x": 1}, "B？"]) == ["A？", "B？"])


# ══════════════════════════════════════════════════════════════════════════
# 2. Evidence gathering
# ══════════════════════════════════════════════════════════════════════════
def test_evidence() -> None:
    print("\n[取材：章节标题最关键]")
    tmp = Path(tempfile.mkdtemp(prefix="suggest-ev-"))
    try:
        m, idx = build_manifest(tmp)
        ev = _evidence(m, idx)
        check("含章节标题", "章节标题" in ev and "股息" in ev and "财务摘要" in ev)
        check("含文件名与摘要", "2024/annual/report.md" in ev and "全年报告" in ev)
        check("含目录与主题", "2024/" in ev and "2024 年度" in ev)
        check("章节标题排在文件之前（截断时先丢最不具体的）",
              ev.index("章节标题") < ev.index("文件与摘要") < ev.index("目录:"))

        empty = Manifest(root="e")
        check("空语料返回空串", _evidence(empty, idx) == "")

        # A corpus with files but no trees must still yield evidence, not blow up.
        m2 = Manifest(root="m2")
        m2.dirs[""] = DirEntry(rel_path="", name="", parent=None)
        m2.files["a.md"] = FileEntry(rel_path="a.md", name="a.md", parent="",
                                     ext=".md", summary="只有摘要", tree_key="missing")
        ev2 = _evidence(m2, idx)
        check("树文件缺失不影响取材", "只有摘要" in ev2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# 3. The call itself
# ══════════════════════════════════════════════════════════════════════════
def test_corpus_suggestions() -> None:
    print("\n[生成：提示词要带上语料信息]")
    tmp = Path(tempfile.mkdtemp(prefix="suggest-gen-"))
    try:
        m, idx = build_manifest(tmp)
        with StubLLM() as stub:
            got = corpus_suggestions(m, idx, "model", name="友邦保险",
                                     summary="收录 2023-2024 年报")
            p = stub.prompts[-1]
            check("返回 3 条", len(got) == 3, str(got))
            check("提示词含库名", "友邦保险" in p)
            check("提示词含库说明", "收录 2023-2024 年报" in p)
            check("提示词含章节标题", "股息" in p)
            check("提示词要求 JSON", "questions" in p)
            check("提示词要求可被回答", "确实能回答" in p)
            check("提示词要求区分度", "区分度" in p)
            check("提示词禁止搬目录名", "目录名" in p)

        # limit is honoured and passed through to the prompt.
        with StubLLM() as stub:
            got = corpus_suggestions(m, idx, "model", limit=2)
            check("limit=2 生效", len(got) == 2, str(got))
            check("limit 写进提示词", "2 个" in stub.prompts[-1])

        # A malformed reply degrades to fewer questions, never to junk.
        with StubLLM({"questions": ["好的，", "", "友邦 2024 年的 VONB 是多少？"]}):
            got = corpus_suggestions(m, idx, "model")
            check("坏条目被丢掉，好条目保留",
                  got == ["友邦 2024 年的 VONB 是多少？"], str(got))

        with StubLLM({"nope": True}):
            check("无法解析 → 空列表", corpus_suggestions(m, idx, "model") == [])

        # ★ Transport failure must be survivable: this runs mid-index.
        with StubLLM(boom=True):
            check("调用异常 → 空列表", corpus_suggestions(m, idx, "model") == [])

        # No evidence → do not even call the model.
        with StubLLM() as stub:
            empty = Manifest(root="e")
            check("空语料不发请求", corpus_suggestions(empty, idx, "model") == [])
            check("确实没发请求", stub.prompts == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# 4. Registry caching
# ══════════════════════════════════════════════════════════════════════════
def new_registry(tmp: Path) -> Registry:
    tmp.mkdir(parents=True, exist_ok=True)
    return Registry(state_file=tmp / "state.json", index_root=tmp / "idx")


def seed(reg: Registry, tmp: Path, *, name="友邦保险",
         summarize_files=True) -> Corpus:
    """Register a corpus and give it an index, bypassing the LLM indexer."""
    src = tmp / "src" / name
    (src / "2024" / "annual").mkdir(parents=True, exist_ok=True)
    (src / "2024" / "annual" / "A.md").write_text(
        "# 报告\n\n## 股息\n\n150.72 港仙\n", encoding="utf-8")
    c = reg.add(str(src), name=name)
    c.summarize_files = summarize_files
    c.status = STATUS_READY
    m, idx = build_manifest(tmp / f"build-{c.id}")
    c.index_dir = str(idx)
    c.summary = "收录 2024 年年报"
    reg.save()
    return c


def test_registry_cache() -> None:
    print("\n[缓存：按指纹失效，改名要重算]")
    tmp = Path(tempfile.mkdtemp(prefix="suggest-reg-"))
    try:
        reg = new_registry(tmp / "reg")
        c = seed(reg, tmp)
        check("初始没有示例问题", c.suggestions == [])

        with StubLLM():
            got = reg.refresh_suggestions(c.id)
            check("生成后有 3 条", len(got) == 3 and len(reg.get(c.id).suggestions) == 3)
            check("指纹已写入", bool(reg.get(c.id).suggestions_fingerprint))
            check("指纹含库名",
                  reg.get(c.id).suggestions_fingerprint.endswith("|友邦保险"))

        # Persisted, not just in memory.
        again = Registry(state_file=tmp / "reg" / "state.json",
                         index_root=tmp / "reg" / "idx")
        check("重启后仍在", len(again.get(c.id).suggestions) == 3)

        # ensure_suggestions must not re-spend on a corpus that already has them.
        check("已有则不再排队", reg.ensure_suggestions() == [])

        # ★ A rename must regenerate: the questions name the corpus.
        old = reg.get(c.id).suggestions_fingerprint
        check("改名后指纹不同",
              _suggestions_fingerprint(old.split("|")[0], "友邦保险控股")
              != old)

        # A corpus with descriptions off must be skipped, not silently billed.
        c2 = seed(reg, tmp, name="无描述库", summarize_files=False)
        check("关掉描述的语料不排队", c2.id not in reg.ensure_suggestions())
        check("关掉描述的语料也没有示例问题",
              reg.get(c2.id).suggestions == [])

        # A not-ready corpus must be skipped too.
        c3 = seed(reg, tmp, name="未就绪库")
        c3.status = "pending"
        reg.save()
        check("未就绪的语料不排队", c3.id not in reg.ensure_suggestions())

        # Fingerprint helper is total.
        check("空内容指纹返回空", _suggestions_fingerprint("", "任意") == "")

        # A failing generation leaves the corpus without suggestions, and the
        # failure must not propagate out of refresh_suggestions.
        c4 = seed(reg, tmp, name="失败库")
        with StubLLM(boom=True):
            check("生成失败返回空", reg.refresh_suggestions(c4.id) == [])
            check("生成失败不留脏数据", reg.get(c4.id).suggestions == [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ensure_runs_in_background() -> None:
    print("\n[回填：不阻塞请求]")
    tmp = Path(tempfile.mkdtemp(prefix="suggest-bg-"))
    try:
        reg = new_registry(tmp / "reg")
        c = seed(reg, tmp)
        with StubLLM():
            ids = reg.ensure_suggestions()
            check("返回被排队的 id", ids == [c.id], str(ids))
            check("立刻再次调用不重复排队（已标记 busy/done）",
                  reg.ensure_suggestions() == [])
            # The worker is a daemon thread; wait for it to land.
            import time
            for _ in range(100):
                if reg.get(c.id).suggestions:
                    break
                time.sleep(0.05)
            check("后台线程最终写入", len(reg.get(c.id).suggestions) == 3)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_api_shape() -> None:
    print("\n[接口：/api/state 里带上示例问题]")
    # corpus_json lives in the server; import it without starting a server.
    import importlib
    srv = importlib.import_module("superindex.webapp.nav_server")
    c = Corpus(id="x", path="/tmp", name="库", index_dir="/tmp/i")
    c.suggestions = ["A 是多少？", "B 是多少？", "C 是多少？"]
    j = srv.corpus_json(c)
    check("corpus_json 含 suggestions", j.get("suggestions") == c.suggestions)
    check("没有示例问题时是空列表", srv.corpus_json(
        Corpus(id="y", path="/tmp", name="库2", index_dir="/tmp/i2"))["suggestions"] == [])


def main() -> int:
    print("=" * 74)
    print("suggest 测试（打桩 LLM，临时目录，不联网）")
    print("=" * 74)
    test_reply_shapes()
    test_cleaning()
    test_evidence()
    test_corpus_suggestions()
    test_registry_cache()
    test_ensure_runs_in_background()
    test_api_shape()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
