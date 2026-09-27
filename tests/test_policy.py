#!/usr/bin/env python3
"""
Tests for nav/policy.py — the business routing policy.

The single most important property is asserted first: **an empty policy is a
no-op.** Everything else is a bonus on top of the built-in routing behaviour,
and if that ever stopped being true, shipping this feature would be a
behavioural change to every existing deployment.

Also covers the wiring in nav/route.py: periods, weights, exclusions, aliases
and prompt annotation, all with a stubbed LLM so nothing touches the network.

    python tests/test_policy.py
    pytest tests/test_policy.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nav.policy import (  # noqa: E402
    DirWeight, PolicyError, RoutingPolicy, Scope, _pattern_hits_segment,
)
from nav.route import (  # noqa: E402
    MultiNavigator, Navigator, _bind_policy, _score_candidate, year_hints,
)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


def write_policy(text: str, suffix: str = ".yaml") -> Path:
    """A throwaway policy file. Caller removes the parent dir."""
    tmp = Path(tempfile.mkdtemp(prefix="policy-test-"))
    p = tmp / f"routing_policy{suffix}"
    p.write_text(text, encoding="utf-8")
    return p


def _trace(question: str, scope: list[str]):
    """QueryTrace, imported lazily so a LogSandbox is already in place."""
    from nav.debuglog import QueryTrace
    return QueryTrace(question, scope)


class LogSandbox:
    """Point debuglog at a temp dir.

    Needed because `_bind_policy` deliberately logs a failed load — without
    this, a test that exercises the failure path would append real entries to
    results/logs/errors.jsonl and pollute the log a user actually reads.
    """

    def __enter__(self):
        from nav import debuglog
        self._dbg = debuglog
        self.tmp = Path(tempfile.mkdtemp(prefix="policy-log-"))
        self.saved = (debuglog.LOG_DIR, debuglog.QUERY_LOG, debuglog.ERROR_LOG)
        debuglog.LOG_DIR = self.tmp
        debuglog.QUERY_LOG = self.tmp / "queries.jsonl"
        debuglog.ERROR_LOG = self.tmp / "errors.jsonl"
        return self.tmp

    def __exit__(self, *exc):
        d = self._dbg
        d.LOG_DIR, d.QUERY_LOG, d.ERROR_LOG = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# 1. The load-bearing guarantee: empty means unchanged
# ══════════════════════════════════════════════════════════════════════════
def test_empty_is_a_noop() -> None:
    print("\n[空策略 = 内置默认行为]")
    p = RoutingPolicy()
    check("is_empty", p.is_empty)
    check("describe 说明是空策略", "空" in p.describe())

    # Periods: identical to the built-in extractor for every phrasing.
    for q in ("2024 年全年股息", "FY2024", "2019 与 2023 对比", "没有年份"):
        check(f"periods_in 与 year_hints 一致: {q!r}",
              p.periods_in(q) == year_hints(q),
              f"{p.periods_in(q)} vs {year_hints(q)}")

    # Nothing is excluded, nothing is weighted, nothing is annotated.
    check("不排除任何目录", not p.is_excluded("a/_drafts/b"))
    check("不排除根目录", not p.is_excluded("_drafts"))
    check("权重为 0", p.weight_for("a/annual") == 0)
    check("无标注", p.annotate("a/annual") == "")
    check("无提示词块", p.prompt_block(["a/annual"]) == "")
    check("无别名扩展", p.alias_terms("友邦的股息") == [])

    # And the scorer is unchanged: a weight of 0 must not alter the score.
    base = _score_candidate("2024 年股息", "a/annual", "全年股息", ["2024"])
    same = _score_candidate("2024 年股息", "a/annual", "全年股息", ["2024"],
                            weight=0, extra_terms=())
    check("weight=0 时打分不变", base == same, f"{base} vs {same}")


def test_missing_file_is_empty() -> None:
    print("\n[找不到文件 → 空策略]")
    tmp = Path(tempfile.mkdtemp(prefix="policy-missing-"))
    try:
        p = RoutingPolicy.load(tmp / "nope.yaml")
    except PolicyError:
        check("显式路径不存在时抛错", True)
    else:
        check("显式路径不存在时抛错", False, "应抛 PolicyError")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # An empty YAML document is a policy with nothing set, not an error.
    f = write_policy("# 全是注释\n")
    try:
        p = RoutingPolicy.load(f)
        check("空文档 → 空策略", p.is_empty and p.source == str(f))
    finally:
        shutil.rmtree(f.parent, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# 2. Parsing and validation
# ══════════════════════════════════════════════════════════════════════════
def test_parsing_forms() -> None:
    print("\n[解析：简写与完整写法]")
    p = RoutingPolicy.from_dict({
        "directories": {
            "weights": ["annual",
                        {"pattern": "interim", "weight": 2, "label": "中期报告"},
                        {"path": "group", "weight": 3}],
            "exclude": ["_drafts", "templates"],
            "scopes": [{"name": "财务", "dirs": ["annual"], "note": "看这里"}],
        },
        "periods": {"patterns": [r"FY\s?(\d{2,4})"]},
        "aliases": {"友邦": ["AIA"]},
        "instructions": "集团口径优先",
    })
    check("简写权重默认 1", p.weights[0] == DirWeight("annual", 1, ""))
    check("完整写法带 label", p.weights[1].label == "中期报告")
    check("path 是 pattern 的别名", p.weights[2].pattern == "group")
    check("exclude 解析", p.exclude == ("_drafts", "templates"))
    check("scope 解析", p.scopes == (Scope("财务", ("annual",), "看这里"),))
    check("periods 解析", p.periods == (r"FY\s?(\d{2,4})",))
    check("aliases 解析", p.aliases == (("友邦", ("AIA",)),))
    check("instructions 解析", p.instructions == "集团口径优先")

    # periods may also be a bare list, and aliases a list of groups.
    p2 = RoutingPolicy.from_dict({
        "periods": ["FY(\\d{2})"],
        "aliases": [["友邦", "AIA", "友邦保险"]],
    })
    check("periods 允许裸列表", p2.periods == ("FY(\\d{2})",))
    check("aliases 允许列表组", p2.aliases == (("友邦", ("AIA", "友邦保险")),))


def test_validation_errors() -> None:
    print("\n[校验：坏配置要报错，不能静默]")
    cases = [
        ({"directories": {"weights": "annual"}}, "weights 不是列表"),
        ({"directories": {"weights": [""]}}, "空目录模式"),
        ({"directories": {"weights": [{"weight": 3}]}}, "缺少 pattern"),
        ({"directories": {"weights": [{"pattern": "a", "weight": "高"}]}},
         "weight 不是数字"),
        ({"directories": {"exclude": [123]}}, "exclude 元素不是字符串"),
        ({"directories": {"scopes": [{"dirs": ["a"]}]}}, "scope 缺 name"),
        ({"periods": {"patterns": ["(unclosed"]}}, "正则语法错误"),
        ({"aliases": {"友邦": 1}}, "别名值类型错误"),
        ({"instructions": 42}, "instructions 不是字符串"),
        ({"corpora": {"x": "不是映射"}}, "语料覆盖不是映射"),
        (["不是映射"], "根节点不是映射"),
    ]
    for raw, label in cases:
        try:
            RoutingPolicy.from_dict(raw)
        except PolicyError as exc:
            check(f"拒绝: {label}", bool(str(exc)), str(exc)[:52])
        else:
            check(f"拒绝: {label}", False, "没有抛 PolicyError")

    # The good cases must still pass.
    for raw in ({}, None, {"version": 1}):
        try:
            RoutingPolicy.from_dict(raw)
            check(f"接受: {raw!r}", True)
        except PolicyError as exc:
            check(f"接受: {raw!r}", False, str(exc))


def test_broken_file_reports_the_path() -> None:
    print("\n[坏文件：报错要带路径]")
    f = write_policy("directories:\n  weights: {oops\n")
    try:
        RoutingPolicy.load(f)
    except PolicyError as exc:
        check("YAML 语法错误被捕获", "解析失败" in str(exc), str(exc)[:60])
    else:
        check("YAML 语法错误被捕获", False)
    finally:
        shutil.rmtree(f.parent, ignore_errors=True)


# ══════════════════════════════════════════════════════════════════════════
# 3. Injection point 1 — period detection
# ══════════════════════════════════════════════════════════════════════════
def test_periods() -> None:
    print("\n[注入点 1：期间识别]")
    p = RoutingPolicy.from_dict({
        "periods": {"patterns": [r"FY\s?(\d{2,4})", r"(\d{4})\s*年", r"\bQ([1-4])\b"]},
    })
    check("FY24 归一成 2024", p.periods_in("看 FY24 的数据")[:1] == ["2024"])
    check("FY2024 保留原文", "FY2024" in p.periods_in("FY2024 年报"))
    check("4 位年份不重复", p.periods_in("2024 年的数据") == ["2024"])
    check("裸年份仍然识别", p.periods_in("2019 对比") == ["2019"])
    check("多期间按出现顺序", p.periods_in("2023 与 2024 对比") == ["2023", "2024"])
    check("无期间返回空", p.periods_in("今年的股息是多少") == [])
    check("季度也能抓", p.periods_in("Q3 表现") == ["Q3"])

    # An empty policy must produce exactly the built-in answer, always.
    e = RoutingPolicy()
    for q in ("FY24", "2024 年", "Q3", "2023-2024", ""):
        check(f"空策略退化为内置: {q!r}", e.periods_in(q) == year_hints(q))


# ══════════════════════════════════════════════════════════════════════════
# 4. Injection point 2 — weights
# ══════════════════════════════════════════════════════════════════════════
def test_weights() -> None:
    print("\n[注入点 2：目录权重]")
    p = RoutingPolicy.from_dict({"directories": {"weights": [
        "annual",
        {"pattern": "group", "weight": 3, "label": "集团口径"},
        {"pattern": "*/audited/*", "weight": 2, "label": "已审计"},
    ]}})
    check("子串命中", p.weight_for("c/2024/annual/x.md") == 1)
    check("不命中为 0", p.weight_for("c/2024/other/x.md") == 0)
    check("权重可累加", p.weight_for("c/annual/group/x.md") == 4,
          str(p.weight_for("c/annual/group/x.md")))
    check("通配命中", p.weight_for("c/2024/audited/f.md") == 2)
    check("label 取最强的一条", p.label_for("c/annual/group/x.md") == "集团口径")
    check("无 label 时回落到 pattern", p.label_for("c/annual/x.md") == "annual")
    check("annotate 格式", p.annotate("c/annual/group/x.md") == "  [优先+4 集团口径]",
          repr(p.annotate("c/annual/group/x.md")))
    check("无权重不加标注", p.annotate("c/other/x.md") == "")

    # The bonus must actually move the score, and by exactly its own value.
    args = ("2024 年股息", "c/2024/annual/x.md", "全年股息", ["2024"])
    check("权重直接加到分数上",
          _score_candidate(*args, weight=5) - _score_candidate(*args) == 5)

    # …and it must be able to break a tie the question cannot.
    tie = RoutingPolicy.from_dict({"directories": {"weights": [
        {"pattern": "preferred", "weight": 3}]}})
    a = _score_candidate("股息", "c/a/plain/x.md", "", [])
    b = _score_candidate("股息", "c/a/preferred/x.md", "", [],
                         weight=tie.weight_for("c/a/preferred/x.md"))
    check("权重能打破平局", b > a, f"{a} vs {b}")


# ══════════════════════════════════════════════════════════════════════════
# 5. Injection point 3 — prompt
# ══════════════════════════════════════════════════════════════════════════
def test_prompt_block() -> None:
    print("\n[注入点 3：提示词]")
    p = RoutingPolicy.from_dict({
        "directories": {"scopes": [
            {"name": "财务数据", "dirs": ["annual", "interim"], "note": "先看这里"},
            {"name": "合规"},
        ]},
        "instructions": "集团合并口径优先于子公司口径",
    })
    block = p.prompt_block()
    check("带出处说明", "config/routing_policy.yaml" in block)
    check("含 instructions", "集团合并口径优先" in block)
    check("含业务域名", "财务数据" in block)
    check("含目录列表", "annual、interim" in block)
    check("含 note", "先看这里" in block)
    check("没有 dirs 的业务域也能渲染", "合规" in block)

    # The block must not invite the model to change the numbering contract.
    check("声明不改变编号规则", "不改变编号规则" in block)
    check("空策略不产生任何块", RoutingPolicy().prompt_block() == "")


# ══════════════════════════════════════════════════════════════════════════
# 6. Injection point 4 — exclude and alias
# ══════════════════════════════════════════════════════════════════════════
def test_exclude_matching() -> None:
    print("\n[注入点 4a：排除是整段匹配]")
    p = RoutingPolicy.from_dict({"directories": {"exclude": ["_drafts", "templates"]}})
    check("整段命中", p.is_excluded("c/2024/_drafts/x.md"))
    check("根层命中", p.is_excluded("templates"))
    check("子串不算命中（drafting）", not p.is_excluded("c/drafting/x.md"))
    check("子串不算命中（templates-old）", not p.is_excluded("c/templates-old/x.md"))
    check("不相关目录不命中", not p.is_excluded("c/2024/annual/x.md"))

    # A pattern with a slash is a path rule instead, for deliberate depth.
    deep = RoutingPolicy.from_dict({"directories": {"exclude": ["a/b/**"]}})
    check("带 / 的规则按路径匹配", deep.is_excluded("a/b/c/d.md"))
    check("带 / 的规则不误伤", not deep.is_excluded("x/a/b.md"))

    # The matcher itself, directly — the boundary between the two modes.
    check("整段：'*' 通配可用", _pattern_hits_segment("_*", "c/_drafts/x"))
    check("整段：大小写无关", _pattern_hits_segment("TEMPLATES", "c/templates/x"))
    check("空路径不命中", not _pattern_hits_segment("a", ""))
    check("空模式不命中", not _pattern_hits_segment("", "a/b"))


def test_aliases() -> None:
    print("\n[注入点 4b：别名扩展]")
    p = RoutingPolicy.from_dict({"aliases": {
        "友邦": ["AIA", "友邦保险"],
        "VONB": ["新业务价值"],
    }})
    got = dict(p.alias_terms("友邦 2024 年的 VONB 是多少"))
    check("命中键 → 补出其他写法", "AIA" in got and "友邦保险" in got)
    check("问题里已有的词不重复加", "友邦" not in got)
    check("第二个别名组也生效", "新业务价值" in got)
    check("扩展词权重为 2", got.get("AIA") == 2)
    check("不相关的组不扩展", "新业务价值" in got and "AIA" in got)
    check("无命中组返回空", p.alias_terms("今年的股息") == [])

    # Symmetric: naming the variant must pull in the key too.
    got2 = dict(p.alias_terms("AIA 的分红"))
    check("方向对称：命中变体补出键", got2.get("友邦") == 2)

    # Aliases widen the fallback, which is the whole point of the feature.
    q = "友邦的全年股息"
    check("别名让回退匹配到 AIA 目录",
          _score_candidate(q, "c/2024/AIA/x.md", "", []) == 0
          and _score_candidate(q, "c/2024/AIA/x.md", "", [],
                               extra_terms=tuple(p.alias_terms(q))) > 0)


# ══════════════════════════════════════════════════════════════════════════
# 7. Per-corpus overlays
# ══════════════════════════════════════════════════════════════════════════
def test_overlays() -> None:
    print("\n[单语料覆盖：合并而不是替换]")
    p = RoutingPolicy.from_dict({
        "directories": {
            "weights": [{"pattern": "annual", "weight": 1}],
            "exclude": ["_drafts"],
        },
        "instructions": "全局规则",
        "corpora": {
            "友邦保险": {
                "directories": {
                    "weights": [{"pattern": "thailand", "weight": 3,
                                 "label": "泰国市场"}],
                    "exclude": ["legacy"],
                    "scopes": [{"name": "海外", "dirs": ["thailand"]}],
                },
                "instructions": "该语料按市场拆章",
            },
        },
    })
    check("语料键被记录", p.corpus_keys == ("友邦保险",))
    check("全局权重对任意语料生效", p.weight_for("other/annual/x.md") == 1)
    check("覆盖权重只对该语料生效", p.weight_for("友邦保险/annual/thailand/x.md") == 4,
          str(p.weight_for("友邦保险/annual/thailand/x.md")))
    check("其他语料拿不到覆盖权重", p.weight_for("other/annual/thailand/x.md") == 1)
    check("覆盖排除生效", p.is_excluded("友邦保险/legacy/x.md"))
    check("覆盖排除不波及别的语料", not p.is_excluded("other/legacy/x.md"))
    check("全局排除仍然生效（合并语义）", p.is_excluded("友邦保险/_drafts/x.md"))

    block = p.prompt_block(["友邦保险/2024/annual", "other/2024/annual"])
    check("提示词含全局 instructions", "全局规则" in block)
    check("提示词含该语料 instructions", "该语料按市场拆章" in block)
    check("提示词含覆盖 scopes", "海外" in block)

    # Overlays not in scope must simply not render.
    only_other = p.prompt_block(["other/2024/annual"])
    check("不在范围内的语料覆盖不渲染", "该语料按市场拆章" not in only_other)

    # Binding rewrites name keys to ids, which is what merged paths carry.
    bound = p.bind_corpora({"a9e87d72": "友邦保险"})
    check("绑定后按 id 命中",
          bound.weight_for("a9e87d72/annual/thailand/x.md") == 4)
    check("绑定后旧名字不再命中",
          bound.weight_for("友邦保险/annual/thailand/x.md") == 1)
    check("未注册语料的覆盖被保留",
          RoutingPolicy.from_dict({"corpora": {"未来语料": {}}})
          .bind_corpora({"x": "别的"}).corpus_keys == ("未来语料",))
    check("无绑定信息时原样返回", p.bind_corpora({}) is p)

    # flatten_for is what a single-corpus navigator uses.
    flat = p.flatten_for("友邦保险")
    check("flatten 后覆盖变全局",
          flat.weight_for("any/annual/thailand/x.md") == 4)
    check("flatten 后不再有覆盖层", flat.corpus_keys == ())
    check("flatten 未知键只丢覆盖层",
          p.flatten_for("不存在").corpus_keys == ()
          and p.flatten_for("不存在").weight_for("x/annual/y") == 1)


def test_bind_policy_falls_back() -> None:
    print("\n[加载失败不致命]")
    check("_bind_policy(None) 不抛", isinstance(_bind_policy(None), RoutingPolicy))

    # Simulate the real failure: point the loader at a broken file via env.
    # This is the path a typo in the YAML actually takes in production.
    # `_bind_policy` prints one line to stdout when it degrades; capture it so
    # a deliberate failure does not look like test noise.
    import contextlib
    import io
    import os
    f = write_policy("directories:\n  weights: [\n")
    saved = os.environ.get("SUPERINDEX_ROUTING_POLICY")
    os.environ["SUPERINDEX_ROUTING_POLICY"] = str(f)
    try:
        noise = io.StringIO()
        with LogSandbox() as logdir, contextlib.redirect_stdout(noise):
            p = _bind_policy(None)
            # Asserted inside the sandbox: __exit__ removes the temp dir.
            logged = "policy.load" in (
                (logdir / "errors.jsonl").read_text(encoding="utf-8")
                if (logdir / "errors.jsonl").is_file() else "")
        check("坏文件 → 退回空策略", p.is_empty)
        check("退化时留下可读的一行说明", "策略加载失败" in noise.getvalue(),
              noise.getvalue().strip()[:70])
        check("失败被记进 errors 日志（where=policy.load）", logged)
    finally:
        if saved is None:
            os.environ.pop("SUPERINDEX_ROUTING_POLICY", None)
        else:
            os.environ["SUPERINDEX_ROUTING_POLICY"] = saved
        shutil.rmtree(f.parent, ignore_errors=True)

    # An explicit policy is never replaced.
    given = RoutingPolicy.from_dict({"directories": {"exclude": ["x"]}})
    check("显式传入的策略被尊重", _bind_policy(given) is given)
    check("指定语料时先 flatten",
          _bind_policy(RoutingPolicy.from_dict(
              {"corpora": {"c1": {"directories": {"exclude": ["y"]}}}}),
              "c1").exclude == ("y",))


# ══════════════════════════════════════════════════════════════════════════
# 8. Route integration, with the LLM stubbed out
# ══════════════════════════════════════════════════════════════════════════
class StubLLM:
    """Records prompts and returns a canned reply, so routing runs offline.

    `dirs`/`files` control what the model "picks". Passing empty lists is the
    important case: it is what forces the deterministic fallback, which is
    where three of the four policy injection points live.
    """

    def __init__(self, dirs=(0,), files=(0,), sections=(0,)):
        self.dirs, self.files, self.sections = list(dirs), list(files), list(sections)
        self.prompts: list[str] = []

    def __enter__(self):
        from nav import llm
        self._llm = llm
        self._saved = (llm.chat_json, llm.chat)

        def chat_json(prompt, **_kw):
            self.prompts.append(prompt)
            if "章节列表" in prompt:
                return {"sections": self.sections}
            return {"dirs": self.dirs, "files": self.files,
                    "descend": [], "pick": self.files}

        llm.chat_json = chat_json
        llm.chat = lambda prompt, **_kw: (self.prompts.append(prompt), "")[1]
        return self

    def __exit__(self, *exc):
        self._llm.chat_json, self._llm.chat = self._saved


def build_index(root: Path) -> None:
    """A tiny two-corpus manifest.

    Three directories per corpus, chosen so each policy hook has something to
    bite on: `aaa_other` sorts before `annual` (so a tie is visible),
    `annual` is the directory the policy will prefer, and `_drafts` is the one
    it must hide.
    """
    from nav.store import Chapter, DirEntry, FileEntry, Manifest
    for cid in ("c1", "c2"):
        m = Manifest(root=cid)
        for name, topic in (("aaa_other", "其他"), ("annual", "年报"),
                            ("_drafts", "草稿")):
            m.dirs[name] = DirEntry(rel_path=name, name=name, parent="",
                                    summary=f"{topic}目录")
            m.dirs[name].n_files = 1
        m.dirs[""] = DirEntry(rel_path="", name="", parent=None)
        m.dirs[""].child_dirs = ["aaa_other", "annual", "_drafts"]
        for name in ("aaa_other", "annual", "_drafts"):
            rp = f"{name}/report.md"
            m.files[rp] = FileEntry(rel_path=rp, name="report.md", parent=name,
                                    ext=".md", summary="2024 年全年股息", tree_key=name)
            m.save_tree(Path(root) / cid, name, [Chapter("股息", 1, 1, 3, "全年股息")],
                        ["# 股息", "150.72 港仙", "x"])
        m.save(Path(root) / cid)


def test_route_integration() -> None:
    print("\n[接线：route.py 真的用了策略]")
    tmp = Path(tempfile.mkdtemp(prefix="policy-route-"))
    try:
        build_index(tmp)
        corpora = [("c1", tmp / "c1", "语料一", "s1"),
                   ("c2", tmp / "c2", "语料二", "s2")]

        with StubLLM() as stub:
            # --- exclusions actually remove candidates from the prompt ---
            pol = RoutingPolicy.from_dict(
                {"directories": {"exclude": ["_drafts"]}})
            nav = MultiNavigator(corpora, verbose=False, policy=pol)
            nav.find_files("2024 年股息", top_n=3)
            dirs_prompt, files_prompt = stub.prompts[0], stub.prompts[-1]
            check("目录提示词里没有 _drafts", "_drafts" not in dirs_prompt,
                  dirs_prompt[:100].replace("\n", " | "))
            check("文件提示词里没有 _drafts", "_drafts" not in files_prompt)
            check("正常目录仍在提示词里", "annual" in dirs_prompt)

            # --- empty policy leaves the prompt as it always was ---
            stub.prompts.clear()
            plain = MultiNavigator(corpora, verbose=False, policy=RoutingPolicy())
            plain.find_files("2024 年股息", top_n=3)
            check("空策略下 _drafts 正常出现", "_drafts" in stub.prompts[0])

            # --- weights and scopes reach the prompt ---
            stub.prompts.clear()
            rich = MultiNavigator(corpora, verbose=False, policy=RoutingPolicy.from_dict({
                "directories": {
                    "weights": [{"pattern": "annual", "weight": 3, "label": "法定年报"}],
                    "scopes": [{"name": "财务", "dirs": ["annual"], "note": "先看这里"}],
                },
                "instructions": "集团口径优先",
            }))
            rich.find_files("2024 年股息", top_n=3)
            p = stub.prompts[-1]
            check("提示词带优先级标注", "[优先+3 法定年报]" in p, p[-160:].replace("\n", " | "))
            check("提示词带业务域", "业务域「财务」" in p)
            check("提示词带 instructions", "集团口径优先" in p)

        # --- aliases change the deterministic fallback's answer ---
        # The question says "AIA", the directory says "annual". With no alias
        # nothing in the question matches any path, so all three candidates tie
        # at 1 (from the summary) and insertion order decides — `aaa_other`
        # wins. The alias is what breaks that tie correctly.
        with StubLLM(dirs=(), files=()):        # model abstains → fallback runs
            plain_files, _ = MultiNavigator(
                corpora, verbose=False, policy=RoutingPolicy()
            ).find_files("AIA 的股息", top_n=1)
            alias_files, _ = MultiNavigator(
                corpora, verbose=False,
                policy=RoutingPolicy.from_dict({"aliases": {"AIA": ["annual"]}})
            ).find_files("AIA 的股息", top_n=1)
            check("无别名时回退落到第一个候选（aaa_other）",
                  plain_files and plain_files[0].rel_path.endswith("aaa_other/report.md"),
                  plain_files[0].rel_path if plain_files else "(空)")
            check("别名让回退选中 annual",
                  alias_files and alias_files[0].rel_path.endswith("annual/report.md"),
                  alias_files[0].rel_path if alias_files else "(空)")

        # --- a single-corpus Navigator binds the only overlay ---
        pol = RoutingPolicy.from_dict(
            {"corpora": {"语料一": {"directories": {"exclude": ["_drafts"]}}}})
        single = Navigator(tmp / "c1", verbose=False, policy=pol)
        check("单语料导航器套用了唯一覆盖", single.policy.is_excluded("_drafts"))
        check("单语料导航器只剩全局层", single.policy.corpus_keys == ())

        # --- the shipped default config parses and only excludes ---
        shipped = RoutingPolicy.load(ROOT / "config" / "routing_policy.yaml")
        check("随包默认配置可解析", not shipped.is_empty and shipped.source)
        check("随包默认只启用排除",
              bool(shipped.exclude) and not shipped.weights
              and not shipped.scopes and not shipped.periods
              and not shipped.aliases and not shipped.instructions.strip())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_trace_records_policy() -> None:
    print("\n[日志：记录当次生效的策略]")
    import json
    with LogSandbox() as tmp:
        t = _trace("问题", ["语料"])
        t.policy({"source": "/x/policy.yaml", "describe": "2 条排除", "empty": False})
        t.finish("答案")
        rec = [l for l in (tmp / "queries.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip()]
        body = json.loads(rec[-1])
        check("记录里有 policy 字段",
              body.get("policy", {}).get("source") == "/x/policy.yaml")
        check("默认是空 dict（没调用就不写）",
              _trace("q2", []).record["policy"] == {})


def test_sections_parallel_preserves_order() -> None:
    """Per-file section routing runs concurrently, but the ORDER must not move.

    `build_context` truncates in relevance order, so a reshuffle would let a
    marginal section crowd out the best one — the answer would get quietly
    worse while every individual call still looked correct.
    """
    print("\n[并行章节选择：真的并行，且顺序与串行一致]")
    import time as _time

    import nav.route as route_mod

    tmp = Path(tempfile.mkdtemp(prefix="policy-par-"))
    try:
        build_index(tmp)
        corpora = [("c1", tmp / "c1", "语料一", "s1"),
                   ("c2", tmp / "c2", "语料二", "s2")]

        # --- order: one section call per file, results aligned to files ---
        with StubLLM(dirs=(0,), files=(0, 1, 2), sections=(0,)) as stub:
            nav = MultiNavigator(corpora, verbose=False, policy=RoutingPolicy())
            res = nav.run("2024 年股息")
            check("选中了 3 个文件", len(res.files) == 3, str(len(res.files)))
            got = [fe.rel_path for fe, _ in res.sections]
            want = [f.rel_path for f in res.files]
            check("sections 顺序与 files 一致", got == want,
                  f"{got} != {want}")

        # --- single file must not pay for a thread pool ---
        real_pool = route_mod.ThreadPoolExecutor

        def no_pool(*_a, **_k):
            raise AssertionError("单文件不该启线程池")

        route_mod.ThreadPoolExecutor = no_pool        # type: ignore[assignment]
        try:
            with StubLLM(dirs=(0,), files=(0,), sections=(0,)):
                nav1 = MultiNavigator(corpora, verbose=False,
                                      policy=RoutingPolicy())
                one = nav1.sections_for_all("2024 年股息", [next(iter(
                    nav1.m.files.values()))], 6)
            check("单文件走原路径，不启线程池", len(one) == 1, str(len(one)))
        finally:
            route_mod.ThreadPoolExecutor = real_pool  # type: ignore[assignment]

        # --- concurrency: N slow calls must overlap, not stack ---
        # 4 files x 0.25s = 1.0s serial. Assert well under that; a 3x margin
        # keeps this from flapping on a busy machine while still failing loudly
        # if the pool ever degrades back to a serial loop.
        DELAY, N = 0.25, 4
        from nav import llm as _llm

        def slow_json(prompt, **_kw):
            _time.sleep(DELAY)
            if "章节列表" in prompt:
                return {"sections": [0]}
            return {"dirs": [0], "files": [0, 1, 2, 3], "descend": [],
                    "pick": [0, 1, 2, 3]}

        saved = (_llm.chat_json, _llm.chat)
        _llm.chat_json = slow_json
        _llm.chat = lambda prompt, **_kw: ""
        try:
            nav2 = MultiNavigator(corpora, verbose=False,
                                  policy=RoutingPolicy())
            files = list(nav2.m.files.values())[:N]
            t0 = _time.time()
            nav2.sections_for_all("2024 年股息", files, 6)
            took = _time.time() - t0
            check(f"{len(files)} 个文件并行（{DELAY}s/次）总耗时 < {DELAY*N*0.75:.2f}s",
                  took < DELAY * N * 0.75, f"{took:.2f}s")
        finally:
            _llm.chat_json, _llm.chat = saved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_sections_for_all_carries_the_call_bucket() -> None:
    """The per-call bucket must survive the hop into the worker threads.

    `ThreadPoolExecutor` does not inherit contextvars, so `sections_for_all`
    copies the context per task by hand. Without that copy the parallel section
    calls — the ones that dominate a many-file query — would be missing from the
    query's call log, and the instrumentation would silently under-report
    exactly the case it exists for. Silent under-reporting is worse than no
    instrumentation: it would read as "retrieval was cheap".
    """
    print("\n[并行章节调用也进得了调用记录（contextvars 要显式带过去）]")
    from nav import llm as _llm

    tmp = Path(tempfile.mkdtemp(prefix="policy-ctx-"))
    try:
        build_index(tmp)
        corpora = [("c1", tmp / "c1", "语料一", "s1"),
                   ("c2", tmp / "c2", "语料二", "s2")]

        def noting_json(prompt, label="", tag="", **_kw):
            # Stand in for chat()'s own `_note_call`, so this measures the
            # context hop rather than the litellm plumbing.
            _llm._note_call({"label": label, "tag": tag, "ms": 1, "attempts": 1,
                             "attempt_ms": [1], "retried": False})
            if "章节列表" in prompt:
                return {"sections": [0]}
            return {"dirs": [0], "files": [0, 1, 2, 3], "descend": [],
                    "pick": [0, 1, 2, 3]}

        saved = (_llm.chat_json, _llm.chat)
        _llm.chat_json = noting_json
        _llm.chat = lambda prompt, **_kw: ""
        try:
            nav = MultiNavigator(corpora, verbose=False, policy=RoutingPolicy())
            files = list(nav.m.files.values())[:4]
            with _llm.record_calls() as calls:
                nav.sections_for_all("2024 年股息", files, 6)
            check("4 个文件 → 4 次调用都进了记录", len(calls) == 4,
                  str(len(calls)))
            check("全部标为 route.sections",
                  all(c["label"] == "route.sections" for c in calls),
                  str([c["label"] for c in calls]))
            check("每次都带上了是哪份文档", all(c["tag"] for c in calls),
                  str([c["tag"] for c in calls]))
        finally:
            _llm.chat_json, _llm.chat = saved
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    print("=" * 74)
    print("policy 测试（临时目录 + 打桩 LLM，不联网）")
    print("=" * 74)
    test_empty_is_a_noop()
    test_missing_file_is_empty()
    test_parsing_forms()
    test_validation_errors()
    test_broken_file_reports_the_path()
    test_periods()
    test_weights()
    test_prompt_block()
    test_exclude_matching()
    test_aliases()
    test_overlays()
    test_bind_policy_falls_back()
    test_route_integration()
    test_sections_parallel_preserves_order()
    test_trace_records_policy()
    test_sections_for_all_carries_the_call_bucket()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
