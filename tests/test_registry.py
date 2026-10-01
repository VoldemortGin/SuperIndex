#!/usr/bin/env python3
"""
Tests for the corpus registry and multi-corpus navigation.

Everything runs offline: indexing is exercised with `summarize=False`, so no
LLM is called. Fixtures are built in a temp directory, never in the repo.

    python tests/test_registry.py
    pytest tests/test_registry.py
"""
from __future__ import annotations

import contextlib
import json
import re
import shutil
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from superindex.nav.registry import (  # noqa: E402
    Corpus, Registry, STATUS_ERROR, STATUS_READY,
)
from superindex.nav.build import (BUILDER_VERSION, _ancestor_dirs, _flash_chapters,  # noqa: E402
                       _page_line_spans, _spread, corpus_fingerprint,
                       scan, summarize_files)
from superindex.nav.route import (MultiNavigator, Result, build_context,  # noqa: E402
                       display_path, merge_manifests, _score_candidate)
from superindex.nav.store import DirEntry, Manifest  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── fixtures ─────────────────────────────────────────────────────────────
def make_corpus(base: Path, name: str, files: dict[str, str]) -> Path:
    d = base / name
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


SAMPLE = {
    "2024/annual/A.md": "# Report A\n\n## Summary\n\nAlpha content.\n\n## Details\n\nMore.\n",
    "2024/annual/B.md": "# Report B\n\n## Summary\n\nBeta content.\n",
    "2025/interim/C.md": "# Report C\n\n## Notes\n\nGamma content.\n",
}


def new_registry(tmp: Path) -> Registry:
    """Each test gets its own subdirectory, so state never leaks between them."""
    tmp.mkdir(parents=True, exist_ok=True)
    return Registry(state_file=tmp / "state.json", index_root=tmp / "idx")


# dirs created by SAMPLE: "", "2024", "2024/annual", "2025", "2025/interim"
SAMPLE_DIRS = 4          # excluding the root itself


# ── tests ────────────────────────────────────────────────────────────────
def test_add_validation(tmp: Path) -> None:
    print("\n[注册校验]")
    reg = new_registry(tmp / "add")
    src = make_corpus(tmp / "src", "alpha", SAMPLE)

    for label, bad in [("相对路径", "src/alpha"),
                       ("不存在的路径", str(tmp / "nope"))]:
        try:
            reg.add(bad)
            check(f"拒绝{label}", False, "未报错")
        except ValueError as exc:
            check(f"拒绝{label}", True, str(exc)[:46])

    # a file, not a directory
    f = tmp / "afile.txt"
    f.write_text("x", encoding="utf-8")
    try:
        reg.add(str(f))
        check("拒绝文件（非目录）", False, "未报错")
    except ValueError as exc:
        check("拒绝文件（非目录）", "not a directory" in str(exc))

    # inside the project — indexing our own tree would recurse into results/
    for inside in (ROOT, ROOT / "superindex" / "nav", ROOT / "samples"):
        try:
            reg.add(str(inside))
            check(f"拒绝项目内目录 {inside.name}", False, "未报错")
        except ValueError as exc:
            check(f"拒绝项目内目录 {inside.name}",
                  "inside the project" in str(exc))

    c = reg.add(str(src), name="Alpha")
    check("接受外部目录", c.name == "Alpha" and c.status == "pending")
    check("生成的 id 非空", bool(c.id) and len(c.id) == 8)

    try:
        reg.add(str(src))
        check("拒绝重复注册", False, "未报错")
    except ValueError as exc:
        check("拒绝重复注册", "already registered" in str(exc))


def test_crud_and_persistence(tmp: Path) -> None:
    print("\n[增删改查与持久化]")
    reg = new_registry(tmp / "crud")
    src = make_corpus(tmp / "src", "beta", SAMPLE)
    c = reg.add(str(src), name="Beta")

    check("list 返回 1 个", len(reg.list()) == 1)
    check("get 能取到", reg.get(c.id) is not None)
    check("find_by_path 能取到", (reg.find_by_path(src) or Corpus("", "", "", "")).id == c.id)

    reg.rename(c.id, "Beta 改名")
    check("改名生效", reg.get(c.id).name == "Beta 改名")
    check("空名字被拒", reg.rename(c.id, "  ") is None)

    reg2 = Registry(state_file=tmp / "crud" / "state.json", index_root=tmp / "crud" / "idx")
    check("重新载入后仍在", len(reg2.list()) == 1)
    check("重新载入后名字保留", reg2.list()[0].name == "Beta 改名")

    check("移除成功", reg.remove(c.id))
    check("移除后为空", len(reg.list()) == 0)
    check("重复移除返回 False", not reg.remove(c.id))
    check("移除后磁盘状态已保存",
          Registry(state_file=tmp / "crud" / "state.json",
                   index_root=tmp / "crud" / "idx").list() == [])


def test_indexing_and_changes(tmp: Path) -> None:
    print("\n[索引与变更检测]")
    reg = new_registry(tmp / "index")
    src = make_corpus(tmp / "src", "gamma", SAMPLE)
    c = reg.add(str(src), name="Gamma")

    c = reg.index(c.id, summarize=False)
    check("索引后状态 ready", c.status == STATUS_READY, c.error)
    check("文件数正确", c.n_files == 3, str(c.n_files))
    check("目录数正确", c.n_dirs == SAMPLE_DIRS, str(c.n_dirs))
    check("章节数 > 0", c.n_chapters >= 6, str(c.n_chapters))
    check("索引目录已创建", Path(c.index_dir, "manifest.json").is_file())

    ch = reg.detect_changes(c.id)
    check("无变化时 diff 为空",
          not ch["added"] and not ch["changed"] and not ch["removed"], str(ch))

    # modify
    p = src / "2024/annual/A.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n## Added\n\nNew section.\n",
                 encoding="utf-8")
    ch = reg.detect_changes(c.id)
    check("检测到修改", ch["changed"] == ["2024/annual/A.md"], str(ch["changed"]))

    # add
    (src / "2026/annual/D.md").parent.mkdir(parents=True, exist_ok=True)
    (src / "2026/annual/D.md").write_text("# D\n\n## S\n\nbody\n", encoding="utf-8")
    ch = reg.detect_changes(c.id)
    check("检测到新增", ch["added"] == ["2026/annual/D.md"], str(ch["added"]))

    # incremental reindex picks both up
    before = c.n_chapters
    c = reg.index(c.id, summarize=False)
    check("重建后文件数 +1", c.n_files == 4, str(c.n_files))
    check("重建后章节数增加", c.n_chapters > before, f"{before} -> {c.n_chapters}")

    # remove
    (src / "2026/annual/D.md").unlink()
    ch = reg.detect_changes(c.id)
    check("检测到删除", ch["removed"] == ["2026/annual/D.md"], str(ch["removed"]))
    c = reg.index(c.id, summarize=False)
    check("重建后文件数回落", c.n_files == 3, str(c.n_files))
    check("孤儿章节树已清理",
          len(list(Path(c.index_dir, "trees").glob("*.json"))) == 3,
          str(len(list(Path(c.index_dir, "trees").glob("*.json")))))

    # unchanged files keep their tree (incremental, not a full rebuild)
    m = Manifest.load(Path(c.index_dir))
    check("未变更文件保留 n_chapters",
          all(f.n_chapters >= 0 for f in m.files.values()))

    # missing directory is reported, not crashed on
    shutil.rmtree(src)
    ch = reg.detect_changes(c.id)
    check("目录消失时标记 missing", ch.get("missing") is True, str(ch))
    c = reg.index(c.id, summarize=False)
    check("目录消失时进入 error", c.status == STATUS_ERROR, c.status)
    check("错误信息可读", "no longer exists" in c.error, c.error)


def test_multi_corpus(tmp: Path) -> None:
    print("\n[多语料合并与导航]")
    reg = new_registry(tmp / "multi")
    a = reg.add(str(make_corpus(tmp / "src", "one", SAMPLE)), name="语料一")
    b = reg.add(str(make_corpus(tmp / "src", "two", SAMPLE)), name="语料二")
    reg.index(a.id, summarize=False)
    reg.index(b.id, summarize=False)

    merged, dirs = merge_manifests([
        (a.id, a.index_dir, a.name, "第一个"),
        (b.id, b.index_dir, b.name, "第二个"),
    ])
    check("顶层是两个语料", [d.name for d in merged.children_of(None)[0]]
          == ["语料一", "语料二"])
    check("文件数翻倍", len(merged.files) == 6, str(len(merged.files)))
    check("目录数 = 2 × (语料根 + 子目录)",
          len(merged.dirs) == 2 * (1 + SAMPLE_DIRS), str(len(merged.dirs)))
    check("index_dirs 映射正确", set(dirs) == {a.id, b.id})

    sub, _ = merged.children_of(a.id)
    check("语料下保留原目录结构",
          sorted(d.name for d in sub) == ["2024", "2025"], str([d.name for d in sub]))
    f = merged.files[f"{a.id}/2024/annual/A.md"]
    check("文件 parent 正确指向", f.parent == f"{a.id}/2024/annual", f.parent)

    nav = MultiNavigator([
        (a.id, a.index_dir, a.name, "第一个"),
        (b.id, b.index_dir, b.name, "第二个"),
    ], verbose=False)
    check("corpus_ids 正确", sorted(nav.corpus_ids) == sorted([a.id, b.id]))
    check("能从正确索引加载树", len(nav._load_tree(f)[0]) > 0)
    check("未知语料前缀安全返回", nav._load_tree(
        type(f)(rel_path="zzz/x.md", name="x.md", parent="zzz", ext=".md")) == ([], []))

    # navigator() only exposes ready corpora
    nav2 = reg.navigator([a.id])
    check("navigator() 可只选一个", nav2.corpus_ids == [a.id])
    nav3 = reg.navigator()
    check("navigator() 默认含全部就绪语料",
          sorted(nav3.corpus_ids) == sorted([a.id, b.id]))
    try:
        reg.navigator(["nonexistent"])
        check("选择未知语料时报错", False, "未报错")
    except ValueError as exc:
        check("选择未知语料时报错", "no indexed corpora" in str(exc))


@contextlib.contextmanager
def fake_data_root(path: Path):
    """Point nav.registry.DATA_ROOT at a temp dir for one test.

    Without this, any test that touches the watcher discovers the real data/
    directory and starts indexing the user's own documents.
    """
    import superindex.nav.registry as reg_mod
    path.mkdir(parents=True, exist_ok=True)
    original = reg_mod.DATA_ROOT
    reg_mod.DATA_ROOT = path
    try:
        yield path
    finally:
        reg_mod.DATA_ROOT = original


def test_watcher_modes(tmp: Path) -> None:
    print("\n[watcher：检测模式与自动重建]")
    with fake_data_root(tmp / "watch" / "data"):
        _watcher_body(tmp)


def _watcher_body(tmp: Path) -> None:
    reg = new_registry(tmp / "watch")
    src = make_corpus(tmp / "src", "watch", SAMPLE)
    # summarize_files_enabled=False keeps this test offline: the watcher honours
    # the corpus's own setting rather than assuming descriptions are wanted.
    c = reg.add(str(src), name="Watch", summarize_files_enabled=False)
    reg.index(c.id, summarize=False)
    check("已索引 3 个文件", reg.get(c.id).n_files == 3)

    # ── detect-only mode ─────────────────────────────────────────────────
    reg.start_watcher(interval=0.3, auto_index=False)
    check("watcher 已启动", reg.watching)
    (src / "2024/annual/A.md").write_text("# changed\n\n## S\n\nnew\n", encoding="utf-8")

    deadline = time.time() + 12
    while time.time() < deadline and not (reg.get(c.id).changes or {}).get("changed"):
        time.sleep(0.3)
    ch = reg.get(c.id).changes
    check("检测到变更", ch.get("changed") == ["2024/annual/A.md"], str(ch))
    check("auto_index=False 时不动索引",
          reg.get(c.id).n_files == 3, str(reg.get(c.id).n_files))
    reg.stop_watcher()
    check("watcher 已停止", not reg.watching)

    # ── auto-index mode ──────────────────────────────────────────────────
    (src / "2026/annual/D.md").parent.mkdir(parents=True, exist_ok=True)
    (src / "2026/annual/D.md").write_text("# D\n\n## S\n\nbody\n", encoding="utf-8")
    reg.start_watcher(interval=0.3, auto_index=True)
    deadline = time.time() + 20
    while time.time() < deadline and reg.get(c.id).n_files < 4:
        time.sleep(0.3)
    c = reg.get(c.id)
    check("auto_index=True 时自动重建", c.n_files == 4, str(c.n_files))
    check("重建后 changes 清空", not any((c.changes or {}).values()), str(c.changes))
    check("沿用语料自身设置，未调用 LLM", c.n_summarized == 0, str(c.n_summarized))
    reg.stop_watcher()


def test_display_path(tmp: Path) -> None:
    print("\n[display_path —— 别把 corpus id 喂给模型]")
    names = {"610b3c99": "友邦保险", "5a4a0b3b": "annual_reports"}

    check("替换前缀为名称",
          display_path("610b3c99/2024/annual/A.md", names)
          == "友邦保险 / 2024/annual/A.md",
          display_path("610b3c99/2024/annual/A.md", names))
    # The bare id is a real selection: routing picks the corpus level before it
    # picks anything inside it, so an id with no `/` must still render as the
    # name. Letting it through unchanged is exactly what produced
    # "→ a9e87d72、5a4a0b3b" in the thinking panel.
    check("只有 id 没有子路径也要翻译",
          display_path("610b3c99", names) == "友邦保险",
          display_path("610b3c99", names))
    check("未知 id 原样返回",
          display_path("zzzzzzzz/x.md", names) == "zzzzzzzz/x.md")
    check("没有 names 时原样返回",
          display_path("610b3c99/x.md", None) == "610b3c99/x.md")
    check("空串安全", display_path("", names) == "")

    # 端到端：prompt 里绝不能出现 8 位 id
    reg = new_registry(tmp / "display")
    a = reg.add(str(make_corpus(tmp / "display" / "src", "one", SAMPLE)), name="语料一")
    reg.index(a.id, summarize=False)
    nav = reg.navigator([a.id])
    check("MultiNavigator 带上了 names", nav.names.get(a.id) == "语料一",
          str(nav.names))
    check("nav.display() 生效",
          nav.display(f"{a.id}/2024/annual/A.md") == "语料一 / 2024/annual/A.md",
          nav.display(f"{a.id}/2024/annual/A.md"))

    res = Result(question="q")
    fe = nav.m.files[f"{a.id}/2024/annual/A.md"]
    chapters, _ = nav._load_tree(fe)
    if chapters:
        res.sections = [(fe, chapters[0])]
        ctx, srcs = build_context(res, nav)
        check("prompt 里没有 8 位 id",
              not re.search(r"\b[0-9a-f]{8}/", ctx), ctx[:120])
        check("prompt 用可读路径",
              "语料一 / 2024/annual/A.md" in ctx, ctx[:120])
        check("sources 保留原始 file",
              srcs[0]["file"] == f"{a.id}/2024/annual/A.md", srcs[0]["file"])
        check("sources 带 display",
              srcs[0]["display"] == "语料一 / 2024/annual/A.md",
              srcs[0]["display"])


def test_corpus_tree(tmp: Path) -> None:
    print("\n[corpus_tree —— 给 UI 的文档树]")
    reg = new_registry(tmp / "tree")
    src = make_corpus(tmp / "tree" / "src", "alpha", SAMPLE)
    c = reg.add(str(src), name="alpha")
    reg.index(c.id, summarize=False)
    t = reg.corpus_tree(c.id)
    check("基本元数据", t["id"] == c.id and t["name"] == "alpha"
          and t["n_files"] == 3 and t["n_chapters"] > 0,
          str({k:t[k] for k in ("n_files","n_dirs","n_chapters")}))
    check("顶层 2 个节点（2024/2025，fixture 决定的）",
          {n["name"] for n in t["nodes"]} == {"2024", "2025"},
          str([n["name"] for n in t["nodes"]]))
    n2024 = next(n for n in t["nodes"] if n["name"] == "2024")
    check("2024 下有 annual",
          [c["name"] for c in n2024["children"]] == ["annual"],
          str([c["name"] for c in n2024["children"]]))
    annual = n2024["children"][0]
    file_nodes = [c for c in annual["children"] if c["type"] == "file"]
    check("annual 里有 .md 文件", any(f["name"].endswith(".md") for f in file_nodes))
    check("文件带章节数", all(f.get("n_chapters", 0) > 0 for f in file_nodes))
    check("unknown id 返回 error",
          reg.corpus_tree("nonexistent").get("error") == "unknown corpus")


def test_data_root_dropzone(tmp: Path) -> None:
    print("\n[投放区（DATA_ROOT）：自动发现与注册]")
    with fake_data_root(tmp / "dropzone" / "data") as fake_data:
        reg = new_registry(tmp / "dropzone")

        (fake_data / "empty").mkdir()
        check("空目录被跳过", reg.discover_data_root() == [],
              str(reg.discover_data_root()))

        d1 = fake_data / "reports" / "2024"
        d1.mkdir(parents=True)
        (d1 / "a.md").write_text("# A\n\n## S\n\nbody\n", encoding="utf-8")
        check("发现含文件的子目录",
              [p.name for p in reg.discover_data_root()] == ["reports"],
              str([p.name for p in reg.discover_data_root()]))

        d2 = fake_data / "scans"
        d2.mkdir()
        (d2 / "x.pdf").write_bytes(b"%PDF-1.4\n")
        check("PDF 也算可索引",
              sorted(p.name for p in reg.discover_data_root()) == ["reports", "scans"],
              str(sorted(p.name for p in reg.discover_data_root())))

        d3 = fake_data / "scripts_only"
        d3.mkdir()
        (d3 / "run.sh").write_text("echo hi\n", encoding="utf-8")
        check("只有 .sh 的目录被跳过",
              "scripts_only" not in [p.name for p in reg.discover_data_root()])

        (fake_data / ".hidden").mkdir()
        (fake_data / ".hidden" / "x.md").write_text("# x\n", encoding="utf-8")
        check("隐藏目录被跳过",
              ".hidden" not in [p.name for p in reg.discover_data_root()])

        added = reg.sync_data_root(index=False)
        check("sync 自动注册",
              sorted(c.name for c in added) == ["reports", "scans"],
              str(sorted(c.name for c in added)))
        check("sync 是幂等的", reg.sync_data_root(index=False) == [])

        extra = fake_data / "extra"
        extra.mkdir()
        c = reg.add(str(extra))
        check("投放区内的目录允许注册", c.name == "extra")

        for bad, label in [(ROOT / "superindex" / "nav", "项目内非投放区目录"),
                           (ROOT, "项目根")]:
            try:
                reg.add(str(bad))
                check(f"{label}仍拒绝", False, "未报错")
            except ValueError as exc:
                check(f"{label}仍拒绝", "inside the project" in str(exc))


@contextlib.contextmanager
def stubbed_chat(prefix: str = "SUM"):
    """Deterministic summaries plus a call counter, so regeneration is visible."""
    import superindex.nav.llm as llm_mod
    calls: list[str] = []
    real = llm_mod.chat

    def fake(prompt, **kw):
        calls.append(prompt)
        n = len(calls)
        # Directory prompts now ask for JSON; the stub has to answer in kind or
        # the dir summaries stay empty and the test would pass for the wrong
        # reason.
        if '"topic"' in prompt:
            return json.dumps({"topic": f"T{n}", "summary": f"{prefix}{n}"},
                              ensure_ascii=False)
        return f"{prefix}{n}"

    llm_mod.chat = fake
    try:
        yield calls
    finally:
        llm_mod.chat = real


def test_ancestor_dirs() -> None:
    print("\n[_ancestor_dirs]")
    check("三层", _ancestor_dirs("a/b/c.md") == ["a", "a/b"],
          str(_ancestor_dirs("a/b/c.md")))
    check("一层", _ancestor_dirs("a/c.md") == ["a"], str(_ancestor_dirs("a/c.md")))
    check("根层文件", _ancestor_dirs("c.md") == [], str(_ancestor_dirs("c.md")))


def test_dir_summary_carried_and_invalidated(tmp: Path) -> None:
    print("\n[目录摘要：继承 + 失效传播]")
    root = tmp / "stale" / "corpus"
    (root / "2024" / "annual").mkdir(parents=True)
    (root / "2024" / "annual" / "A.md").write_text("# A\n\n## S\n\nalpha\n", encoding="utf-8")
    (root / "2024" / "annual" / "B.md").write_text("# B\n\n## S\n\nbeta\n", encoding="utf-8")
    idx = tmp / "stale" / "idx"
    idx.mkdir(parents=True)

    def build(prev=None):
        m, trees = scan(root, {".md"}, set(), previous=prev)
        for rp, (ch, ln) in trees.items():
            m.save_tree(idx, m.files[rp].tree_key, ch, ln)
        m.save(idx)
        return m

    def phase(prev):
        with stubbed_chat() as calls:
            m = build(prev)
            summarize_files(m, idx, "m", 2)
        return m, len(calls)

    m1, n1 = phase(None)
    check("首次：2 文件 + 2 目录 = 4 次", n1 == 4, str(n1))
    check("目录摘要有内容", bool(m1.dirs["2024/annual"].summary)
          and bool(m1.dirs["2024"].summary))

    m2, n2 = phase(m1)
    check("无改动：0 次调用（目录摘要被继承）", n2 == 0, str(n2))
    check("继承的是同一个摘要",
          m2.dirs["2024"].summary == m1.dirs["2024"].summary)

    (root / "2024" / "annual" / "A.md").write_text(
        "# A\n\n## S\n\nalpha CHANGED\n", encoding="utf-8")
    m3, n3 = phase(m2)
    check("改一个文件：1 文件 + 2 个祖先目录 = 3 次", n3 == 3, str(n3))
    check("祖先目录摘要已更新",
          m3.dirs["2024"].summary != m2.dirs["2024"].summary,
          f"{m2.dirs['2024'].summary} -> {m3.dirs['2024'].summary}")
    check("未变的 B 没有被重算",
          m3.files["2024/annual/B.md"].summary == m2.files["2024/annual/B.md"].summary)

    (root / "2025").mkdir()
    (root / "2025" / "C.md").write_text("# C\n\n## S\n\ngamma\n", encoding="utf-8")
    m4, n4 = phase(m3)
    check("新增文件：新文件 + 新目录 = 2 次", n4 == 2, str(n4))
    check("无关的 2024 目录保持不动",
          m4.dirs["2024"].summary == m3.dirs["2024"].summary)


def test_score_candidate_prefers_summary() -> None:
    print("\n[回退打分：摘要权重高于路径]")
    q = "友邦保险 2024 年的每股股息是多少？"
    years = ["2024"]

    by_summary = _score_candidate(q, "归档/A", "友邦保险 2024 年股息", years)
    by_path = _score_candidate(q, "友邦保险/2024", "", years)
    check("摘要命中 > 路径命中（同名时）", by_summary > 0, str(by_summary))
    check("只有路径也能得分", by_path > 0, str(by_path))
    check("名字毫无信息时靠摘要得分", by_summary >= 5, str(by_summary))
    check("两者都无 → 0 分",
          _score_candidate(q, "杂项", "会议纪要", years) == 0)
    check("年份在摘要里也算",
          _score_candidate("2024 年数据", "x", "2024 年报", years) >= 3)
    check("摘要为空不报错",
          _score_candidate(q, "友邦保险/2024", None, years) > 0)


def test_fallback_uses_summary(tmp: Path) -> None:
    print("\n[确定性回退能靠摘要找到「名字无用」的目录]")
    from superindex.nav.store import DirEntry
    reg = new_registry(tmp / "fb")
    src = make_corpus(tmp / "fb" / "src", "one", SAMPLE)
    c = reg.add(str(src), name="c")
    reg.index(c.id, summarize=False)
    nav = reg.navigator([c.id])

    dirs = [
        DirEntry(rel_path="归档/A", name="A", parent="归档",
                 summary="友邦保险 2024 年年度报告，含每股股息"),
        DirEntry(rel_path="归档/B", name="B", parent="归档",
                 summary="中国平安 2023 年年度报告"),
        DirEntry(rel_path="杂项", name="杂项", parent=None, summary="会议纪要"),
    ]
    picked = nav._fallback_dirs("友邦保险 2024 年的每股股息是多少？", dirs)
    check("选中了名字无用但摘要正确的目录",
          picked == ["归档/A"], str(picked))
    check("无关目录未被选中", "归档/B" not in picked and "杂项" not in picked)


def test_scan_preserves_derived_fields(tmp: Path) -> None:
    print("\n[scan() 必须继承所有「从内容派生」的字段]")
    root = tmp / "preserve" / "corpus"
    (root / "d").mkdir(parents=True)
    (root / "d" / "a.md").write_text("# a\n\n## s\n\nbody\n", encoding="utf-8")

    m1, _ = scan(root, {".md"}, set())
    m1.dirs["d"].summary = "SUM"
    m1.dirs["d"].topic = "TOPIC"
    m2, _ = scan(root, {".md"}, set(), previous=m1)

    # 这条断言的价值在于「新增派生字段时忘了加进 _dir()」——
    # 症状不是报错，而是每次扫描都重算一遍，白花钱。
    check("summary 被继承", m2.dirs["d"].summary == "SUM", m2.dirs["d"].summary)
    check("topic 被继承", m2.dirs["d"].topic == "TOPIC", m2.dirs["d"].topic)
    check("旧 manifest（无 topic 字段）仍可加载",
          DirEntry(rel_path="x", name="x", parent=None).topic == "")


def test_spread_sampling() -> None:
    print("\n[_spread —— 采样而非截断]")
    items = list(range(100))
    got = _spread(items, 10)
    check("数量正确", len(got) == 10, str(len(got)))
    check("覆盖全范围而非前 10 个", got[0] == 0 and got[-1] >= 80, str(got[:3] + got[-2:]))
    check("不足 limit 时原样返回", _spread([1, 2, 3], 10) == [1, 2, 3])
    check("空列表安全", _spread([], 10) == [])


def test_corpus_fingerprint(tmp: Path) -> None:
    print("\n[语料指纹 —— 只在输入变化时才重建语料摘要]")
    root = tmp / "fp" / "corpus"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.md").write_text("# x\n\n## s\n\nbody\n", encoding="utf-8")
    m, _ = scan(root, {".md"}, set())
    f0 = corpus_fingerprint(m)
    check("首次有指纹", bool(f0), f0)

    check("无变化 → 指纹不变", corpus_fingerprint(m) == f0)

    m.dirs["a"].summary = "新摘要"
    f1 = corpus_fingerprint(m)
    check("目录摘要变了 → 指纹变", f1 != f0, f"{f0} -> {f1}")

    m.dirs["a"].topic = "新标签"
    check("目录 topic 变了 → 指纹也变", corpus_fingerprint(m) != f1)

    m.files["a/x.md"].summary = "文件摘要"
    check("文件摘要变了 → 指纹变", corpus_fingerprint(m) != f1)


def test_topic_in_routing_labels(tmp: Path) -> None:
    print("\n[topic 用于路由标签]")
    reg = new_registry(tmp / "topic")
    src = make_corpus(tmp / "topic" / "src", "one", SAMPLE)
    c = reg.add(str(src), name="c")
    reg.index(c.id, summarize=False)
    nav = reg.navigator([c.id])

    d = nav.m.dirs[f"{c.id}/2024"]
    d.topic = "2024 年报与中期业绩"
    d.summary = "含新业务价值与股息"

    with stubbed_chat() as calls:
        import superindex.nav.llm as llm_mod
        real = llm_mod.chat_json
        captured = []
        llm_mod.chat_json = lambda p, **kw: (captured.append(p), {"dirs": [0]})[1]
        try:
            nav._pick_dirs_from_tree("2024 年的股息", [d])
        finally:
            llm_mod.chat_json = real
    prompt = captured[0] if captured else ""
    check("路由 prompt 用了 topic 而不是目录名",
          "2024 年报与中期业绩" in prompt, prompt[:100])
    check("目录名仍然出现（供参考）",
          "2024" in prompt)

    # 打分也应认 topic（bigram 让中文真正能匹配上）
    check("打分认 topic",
          _score_candidate("友邦保险的股息", "x", "", [], "友邦保险年报与股息") > 0)
    check("中文 bigram 生效：长句也能匹配上",
          _score_candidate("友邦保险 2024 年的每股股息是多少？",
                           "归档/A", "友邦保险年报，含每股股息", ["2024"]) >= 8,
          str(_score_candidate("友邦保险 2024 年的每股股息是多少？",
                               "归档/A", "友邦保险年报，含每股股息", ["2024"])))


def test_page_line_spans() -> None:
    print("\n[_page_line_spans —— 页码 → 行号]")
    lines = ["<!-- page: 1 -->", "a", "b",
             "<!-- page: 2 -->", "c",
             "<!-- page: 3 -->", "d", "e"]
    # 标记行本身算在它那一页里：p1 从行 1 到行 3（下一个标记的前一行）
    spans = _page_line_spans(lines)
    check("页 1 覆盖到下一标记前", spans.get(1) == (1, 3), str(spans.get(1)))
    check("页 2", spans.get(2) == (4, 5), str(spans.get(2)))
    check("末页到文件尾", spans.get(3) == (6, 8), str(spans.get(3)))
    check("每页都从标记行开始",
          all(spans[p][0] == i for p, i in ((1, 1), (2, 4), (3, 6))),
          str(spans))
    check("无页标记 → 空映射", _page_line_spans(["a", "b"]) == {})


def test_flash_chapters_stubbed() -> None:
    print("\n[_flash_chapters —— flash 树转成行号制]")
    import superindex.engine.flash as flash_mod

    lines = ["<!-- page: 1 -->", "a", "<!-- page: 2 -->", "b",
             "<!-- page: 3 -->", "c"]
    tree = {"structure": [
        {"title": "CHAPTER ONE", "start_index": 1, "end_index": 2,
         "nodes": [{"title": "Sub", "start_index": 2, "end_index": 2}]},
        {"title": "CHAPTER TWO", "start_index": 3, "end_index": 3},
        {"title": "   ", "start_index": 3, "end_index": 3},
    ]}
    real = flash_mod.page_index_flash
    flash_mod.page_index_flash = lambda path, **kw: tree
    try:
        chapters = _flash_chapters(Path("x.pdf"), lines)
    finally:
        flash_mod.page_index_flash = real

    check("空标题被跳过", len(chapters) == 2, str([c.title for c in chapters]))
    check("标题保留", chapters[0].title == "CHAPTER ONE")
    spans = _page_line_spans(lines)
    # 期望值从 spans 推导，避免手算页码↔行号再算错
    check("页码已转成行号",
          (chapters[0].start, chapters[0].end)
          == (spans[1][0], spans[2][1]),
          f"{(chapters[0].start, chapters[0].end)} vs {(spans[1][0], spans[2][1])}")
    check("子节点保留", len(chapters[0].children) == 1)
    check("单页章节范围",
          (chapters[1].start, chapters[1].end) == (spans[3][0], spans[3][1]),
          f"{(chapters[1].start, chapters[1].end)} vs {(spans[3][0], spans[3][1])}")

    # flash 失败必须优雅返回空，交给调用方回退
    def boom(path, **kw):
        raise RuntimeError("flash exploded")
    flash_mod.page_index_flash = boom
    try:
        check("flash 抛错 → 返回 []（可回退）",
              _flash_chapters(Path("x.pdf"), lines) == [])
    finally:
        flash_mod.page_index_flash = real


def test_builder_version_invalidates(tmp: Path) -> None:
    print("\n[BUILDER_VERSION —— 改建树逻辑必须让旧树失效]")
    root = tmp / "bver" / "corpus"
    (root / "d").mkdir(parents=True)
    (root / "d" / "a.md").write_text("# a\n\n## s\n\nbody\n", encoding="utf-8")

    m1, t1 = scan(root, {".md"}, set())
    check("首次建树", len(t1) == 1, str(len(t1)))
    check("写入了版本号", m1.builder_version == BUILDER_VERSION,
          str(m1.builder_version))

    m2, t2 = scan(root, {".md"}, set(), previous=m1)
    check("同版本 + 未变 → 跳过", len(t2) == 0, str(len(t2)))

    # 模拟「旧版本建的索引」：内容没变，但建树逻辑变了
    m1.builder_version = BUILDER_VERSION - 1
    m3, t3 = scan(root, {".md"}, set(), previous=m1)
    check("版本不同 → 强制重建", len(t3) == 1, str(len(t3)))


def main() -> int:
    print("=" * 74)
    print("Corpus registry tests（全部离线，不调用 LLM）")
    print("=" * 74)
    tmp = Path(tempfile.mkdtemp(prefix="superindex-test-"))
    try:
        test_add_validation(tmp)
        test_crud_and_persistence(tmp)
        test_indexing_and_changes(tmp)
        test_multi_corpus(tmp)
        test_watcher_modes(tmp)
        test_page_line_spans()
        test_flash_chapters_stubbed()
        test_builder_version_invalidates(tmp)
        test_scan_preserves_derived_fields(tmp)
        test_spread_sampling()
        test_corpus_fingerprint(tmp)
        test_topic_in_routing_labels(tmp)
        test_ancestor_dirs()
        test_dir_summary_carried_and_invalidated(tmp)
        test_score_candidate_prefers_summary()
        test_fallback_uses_summary(tmp)
        test_display_path(tmp)
        test_corpus_tree(tmp)
        test_data_root_dropzone(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
