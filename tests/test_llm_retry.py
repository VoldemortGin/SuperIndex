#!/usr/bin/env python3
"""
Tests for the LLM retry/escalation logic in nav/llm.py.

The bug these cover: a reply that hits `max_tokens` comes back with
`finish_reason="length"` and empty content. Retrying the *identical* request
cannot help, so `chat()` must escalate the budget and drop reasoning effort.

No network: `litellm` is replaced with a scripted stub.

    python tests/test_llm_retry.py
    pytest tests/test_llm_retry.py
"""
from __future__ import annotations

import contextlib
import contextvars
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from superindex.nav import llm  # noqa: E402
from superindex.nav.route import _ints  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


# ── stub litellm ─────────────────────────────────────────────────────────
class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content, finish):
        self.message = _Msg(content)
        self.finish_reason = finish


class _Resp:
    def __init__(self, content, finish):
        self.choices = [_Choice(content, finish)]


def truncated() -> _Resp:
    """What a provider returns when the output budget runs out."""
    return _Resp("", "length")


def reply(text: str) -> _Resp:
    return _Resp(text, "stop")


class Stub:
    """Returns scripted responses in order, recording every call's kwargs."""

    suppress_debug_info = True

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def completion(self, **kwargs):
        self.calls.append(dict(kwargs))
        item = self.script.pop(0) if self.script else _Resp("", "length")
        if isinstance(item, Exception):
            raise item
        return item


@contextlib.contextmanager
def stubbed(script):
    """Swap nav.llm's litellm for a scripted stub, restoring it afterwards."""
    stub = Stub(script)
    real = llm._litellm
    llm._litellm = lambda: stub          # type: ignore[assignment]
    try:
        yield stub
    finally:
        llm._litellm = real              # type: ignore[assignment]


# ── tests ────────────────────────────────────────────────────────────────
def test_escalates_on_length() -> None:
    print("\n[finish_reason=length → 扩大预算并关闭 reasoning]")
    with stubbed([truncated(), reply("恢复后的内容")]) as stub:
        out = llm.chat("p", effort="low", max_tokens=1000, retries=2)
        check("最终拿到了内容", out == "恢复后的内容", out)
        check("一共调用了 2 次", len(stub.calls) == 2, str(len(stub.calls)))
        c1, c2 = stub.calls
        check("第 1 次带 reasoning_effort", c1.get("reasoning_effort") == "low")
        check("第 1 次用原始预算", c1.get("max_tokens") == 1000,
              str(c1.get("max_tokens")))
        check("第 2 次预算翻倍", c2.get("max_tokens") == 2000,
              str(c2.get("max_tokens")))
        check("第 2 次不再传 reasoning_effort",
              "reasoning_effort" not in c2, str(c2.get("reasoning_effort")))


def test_escalation_capped() -> None:
    print("\n[预算升级有上限，且单调不减]")
    with stubbed([truncated(), truncated(), reply("ok")]) as stub:
        out = llm.chat("p", effort="low", max_tokens=8000, retries=2)
        check("拿到内容", out == "ok", out)
        budgets = [c["max_tokens"] for c in stub.calls]
        check("预算不超过 ceiling",
              all(b <= llm.MAX_TOKEN_CEILING for b in budgets), str(budgets))
        check("预算单调不减", budgets == sorted(budgets), str(budgets))


def test_no_reasoning_escalates_budget_only() -> None:
    print("\n[本来就没开 reasoning → 只扩预算]")
    with stubbed([truncated(), reply("ok")]) as stub:
        out = llm.chat("p", effort="", max_tokens=1000, retries=2)
        check("拿到内容", out == "ok", out)
        check("第 1 次无 reasoning_effort", "reasoning_effort" not in stub.calls[0])
        check("第 2 次仍无 reasoning_effort",
              "reasoning_effort" not in stub.calls[1])
        check("预算仍然翻倍", stub.calls[1]["max_tokens"] == 2000,
              str(stub.calls[1]["max_tokens"]))


def test_exhausted_raises_with_detail() -> None:
    print("\n[全部截断 → 报错且信息可诊断]")
    with stubbed([truncated(), truncated(), truncated()]) as stub:
        try:
            llm.chat("p", effort="low", max_tokens=1000, retries=2)
            check("应抛 RuntimeError", False, "未抛")
        except RuntimeError as exc:
            msg = str(exc)
            check("抛 RuntimeError", True)
            check("说明是 length 截断", "finish_reason=length" in msg, msg[:100])
            check("带上了当时的 max_tokens", "max_tokens=" in msg, msg[:100])
            check("说明 reasoning 状态", "reasoning=" in msg, msg[:100])
        check("确实试满 3 次", len(stub.calls) == 3, str(len(stub.calls)))


def test_transport_error_retries_unchanged() -> None:
    print("\n[传输错误按原样重试，不改预算]")
    with stubbed([ConnectionError("boom"), reply("重试成功")]) as stub:
        out = llm.chat("p", effort="low", max_tokens=1000, retries=2)
        check("重试后成功", out == "重试成功", out)
        check("传输错误不改变预算",
              [c["max_tokens"] for c in stub.calls] == [1000, 1000],
              str([c["max_tokens"] for c in stub.calls]))
        check("传输错误不丢 reasoning_effort",
              stub.calls[1].get("reasoning_effort") == "low")


def test_stream_fallback_gets_more_room() -> None:
    print("\n[流式返回空 → 回退时预算翻倍]")
    # The stub's completion() is not iterable, so chat_stream raises and
    # chat_stream_text falls through to the non-streaming path. The first
    # scripted item is consumed by the stream attempt.
    with stubbed([reply("流式这一发不可迭代"), reply("非流式回退的内容")]) as stub:
        out = "".join(llm.chat_stream_text("p", effort="low", max_tokens=1000))
        check("回退拿到了内容", out == "非流式回退的内容", out)
        check("回退预算翻倍", stub.calls[1]["max_tokens"] == 2000,
              str([c["max_tokens"] for c in stub.calls]))


# ── streaming events: reasoning vs content ───────────────────────────────
class _Delta:
    """A streamed delta. Reasoning models put thinking in a separate field."""

    def __init__(self, content=None, reasoning=None, alias=None):
        self.content = content
        self.reasoning_content = reasoning
        if alias is not None:
            self.reasoning = alias


class _StreamChunk:
    def __init__(self, delta):
        self.choices = [type("_C", (), {"delta": delta})()]


class StreamStub:
    """completion() returns an iterable of chunks when `stream` is set.

    `chunks` is a list of (content, reasoning) pairs — either may be None, which
    is what the provider does: most chunks carry only thinking.
    """

    suppress_debug_info = True

    def __init__(self, chunks, fallback=None):
        self.chunks = list(chunks)
        self.fallback = fallback
        self.calls: list[dict] = []

    def completion(self, **kwargs):
        self.calls.append(dict(kwargs))
        if kwargs.get("stream"):
            return [_StreamChunk(_Delta(c, r)) for c, r in self.chunks]
        return self.fallback if self.fallback is not None else _Resp("", "length")


@contextlib.contextmanager
def stream_stubbed(chunks, fallback=None):
    stub = StreamStub(chunks, fallback)
    real = llm._litellm
    llm._litellm = lambda: stub              # type: ignore[assignment]
    try:
        yield stub
    finally:
        llm._litellm = real                  # type: ignore[assignment]


def test_stream_events_split_reasoning_from_content() -> None:
    print("\n[流式事件把「思考」和「正文」分开，且顺序不乱]")
    # 真实形态：多数 chunk 只有思考，正文零星夹在中间。
    with stream_stubbed([(None, "想1"), ("答1", "想2"), ("答2", None)]) as stub:
        events = list(llm.chat_stream_events("p", effort="low", max_tokens=1000))
        check("事件顺序正确",
              events == [("reasoning", "想1"), ("reasoning", "想2"),
                         ("content", "答1"), ("content", "答2")],
              str(events))
        check("只调用一次（没有多余回退）", len(stub.calls) == 1, str(len(stub.calls)))


def test_stream_events_reasoning_alias() -> None:
    print("\n[部分厂商把思考放在 reasoning 字段 → 同样转发]")
    # StreamStub builds chunks from (content, reasoning) pairs; override the
    # completion so the `reasoning` alias field can be exercised directly.
    stub = StreamStub([])
    stub.completion = lambda **kw: [          # type: ignore[assignment]
        _StreamChunk(_Delta(alias="别名字段的思考")),
        _StreamChunk(_Delta(content="正文")),
    ]
    real = llm._litellm
    llm._litellm = lambda: stub              # type: ignore[assignment]
    try:
        events = list(llm.chat_stream_events("p", max_tokens=100))
        check("reasoning 别名被识别",
              ("reasoning", "别名字段的思考") in events, str(events))
        check("正文照常输出", ("content", "正文") in events, str(events))
    finally:
        llm._litellm = real                  # type: ignore[assignment]


def test_stream_events_falls_back_when_only_reasoning() -> None:
    print("\n[只有思考没有正文 → 视为没到，回退且预算翻倍]")
    # A turn that thinks and never answers must not stream a long silence and
    # then stop empty. Reasoning alone is not arrival.
    with stream_stubbed([(None, "想了很久"), (None, "还在想")],
                        fallback=reply("回退后的答案")) as stub:
        events = list(llm.chat_stream_events("p", effort="low", max_tokens=1000))
        kinds = [k for k, _ in events]
        check("思考被转发", ("reasoning", "想了很久") in events, str(events[:2]))
        check("回退的正文也带上了", ("content", "回退后的答案") in events, str(events))
        check("回退时预算翻倍",
              stub.calls[-1]["max_tokens"] == 2000,
              str([c.get("max_tokens") for c in stub.calls]))
        check("回退调用是非流式的", "stream" not in stub.calls[-1],
              str(stub.calls[-1].keys()))


def test_stream_text_still_yields_plain_strings() -> None:
    print("\n[chat_stream_text 语义不变 —— 仍产出纯字符串]")
    with stream_stubbed([(None, "想"), ("答", None)]):
        out = list(llm.chat_stream_text("p", max_tokens=100))
        check("全是字符串，不含思考", all(isinstance(x, str) for x in out), str(out))
        check("内容只有正文", "".join(out) == "答", str(out))


def test_json_bare_identifiers() -> None:
    print("\n[模型回显标签前缀 → JSON 修复]")
    from superindex.nav.llm import extract_json

    cases = [
        ('{"dirs": [D8, D9, D5, D6]}', ["D8", "D9", "D5", "D6"], "真实失败样本"),
        ('{"dirs": ["D8", "D9"]}',     ["D8", "D9"],             "已是字符串"),
        ('{"dirs": [8, 9]}',           [8, 9],                   "正常数字"),
        ('{"files": [F0, F2]}',        ["F0", "F2"],             "F 前缀"),
        ('```json\n{"sections": [S1]}\n```', ["S1"],           "围栏 + S 前缀"),
        ('{"dirs": [D8, D9,]}',        ["D8", "D9"],             "尾随逗号"),
    ]
    for raw, want, label in cases:
        got = extract_json(raw)
        key = next(k for k in ("dirs", "files", "sections") if k in (got or {}))
        check(f"解析 {label}", (got or {}).get(key) == want, str(got))

    check("true/false/null 不被加引号",
          extract_json('{"a": [true, false], "b": null}') == {"a": [True, False], "b": None})
    check("对象键不被误改",
          extract_json('{"dirs": [D1]}') == {"dirs": ["D1"]})
    check("空数组仍可解析", extract_json('{"dirs": []}') == {"dirs": []})


def test_ints_tolerates_prefixes() -> None:
    print("\n[_ints 容忍 D/F/S 前缀与字符串列表]")
    check("裸字符串", _ints("D8") == [8], str(_ints("D8")))
    check("字符串列表（原来会全丢）", _ints(["D8", "D9"]) == [8, 9],
          str(_ints(["D8", "D9"])))
    check("混合类型", _ints([8, "D9", "10"]) == [8, 9, 10], str(_ints([8, "D9", "10"])))
    check("正常数字", _ints([0, 3]) == [0, 3])
    check("None → []", _ints(None) == [])
    check("bool 被忽略", _ints([True, 2]) == [2], str(_ints([True, 2])))
    check("不可解析的项跳过", _ints(["x", 1, None]) == [1], str(_ints(["x", 1, None])))


# ── per-call instrumentation ─────────────────────────────────────────────
# A stage total cannot tell "the model was called three times" apart from "one
# call had to retry twice and was slow". These cover the record that can.
def test_calls_are_recorded_on_success() -> None:
    print("\n[成功也要记一笔 —— 重试过的成功正是要暴露的静默变慢]")
    with stubbed([reply("一次就好")]):
        with llm.record_calls() as calls:
            out = llm.chat("p", label="route.dirs", tag="t1")
        check("照常返回内容", out == "一次就好", out)
        check("记录 1 条", len(calls) == 1, str(calls))
        rec = calls[0]
        check("label 落库", rec.get("label") == "route.dirs", str(rec.get("label")))
        check("tag 落库", rec.get("tag") == "t1", str(rec.get("tag")))
        check("attempts=1", rec.get("attempts") == 1, str(rec.get("attempts")))
        check("未标记重试", rec.get("retried") is False)
        check("有耗时且为整数",
              isinstance(rec.get("ms"), int) and rec["ms"] >= 0, str(rec.get("ms")))
        check("每次尝试的耗时都在", rec.get("attempt_ms") == [rec["ms"]],
              str(rec.get("attempt_ms")))


def test_retry_is_visible_in_the_record() -> None:
    print("\n[重试在记录里可见 —— 这是本次埋点的全部意义]")
    with stubbed([truncated(), reply("重试后拿到")]) as stub:
        with llm.record_calls() as calls:
            out = llm.chat("p", effort="low", max_tokens=1000, retries=2,
                           label="route.files")
        check("最终拿到内容", out == "重试后拿到", out)
        check("底层确实调了 2 次", len(stub.calls) == 2, str(len(stub.calls)))
        rec = calls[0]
        check("记录里 attempts=2", rec.get("attempts") == 2,
              str(rec.get("attempts")))
        check("记录里标了已重试", rec.get("retried") is True)
        check("两次尝试各有耗时", len(rec.get("attempt_ms") or []) == 2,
              str(rec.get("attempt_ms")))
        check("总耗时 = 各次之和", rec.get("ms") == sum(rec["attempt_ms"]),
              str(rec))


def test_no_collector_is_a_noop() -> None:
    print("\n[没开采集时埋点完全不存在]")
    with stubbed([reply("ok")]):
        out = llm.chat("p", label="route.dirs")
        check("照常返回", out == "ok", out)
    check("默认没有 bucket", llm._calls.get() is None, str(llm._calls.get()))


def test_bucket_is_isolated_across_threads() -> None:
    print("\n[线程隔离 —— 后台建索引不能污染查询的调用记录]")
    from concurrent.futures import ThreadPoolExecutor

    def worker(_):
        llm.chat("p", label="index.summary")
        return llm._calls.get()

    with stubbed([reply("x")] * 8) as stub:
        with llm.record_calls() as calls:
            llm.chat("p", label="route.dirs")
            # Plain submit: ThreadPoolExecutor does not copy contextvars, so a
            # thread that is not part of this query cannot see the bucket.
            with ThreadPoolExecutor(max_workers=2) as pool:
                blind = list(pool.map(worker, [1, 2]))
            # Explicit copy, the way sections_for_all does it: same bucket.
            with ThreadPoolExecutor(max_workers=2) as pool:
                futs = [pool.submit(contextvars.copy_context().run, worker, 0)
                        for _ in (1, 2)]
                [f.result() for f in futs]
        check("桶里共 3 条（主线程 1 + 拷贝上下文的 2）", len(calls) == 3,
              str([c["label"] for c in calls]))
        check("未拷贝上下文的线程看不到 bucket", blind == [None, None], str(blind))
        check("拷贝了上下文的线程写进了同一个桶",
              sorted(c["label"] for c in calls)
              == ["index.summary", "index.summary", "route.dirs"],
              str([c["label"] for c in calls]))
        check("底层一共调了 5 次（主线程 1 + 两个池各 2）", len(stub.calls) == 5,
              str(len(stub.calls)))


def test_call_stats_rollup() -> None:
    print("\n[call_stats 汇总 —— 重试次数是第一个该看的数]")
    s = llm.call_stats([
        {"label": "route.dirs", "ms": 800, "retried": False},
        {"label": "route.files", "ms": 1200, "retried": True},
    ])
    check("调用次数", s["llm_n_calls"] == 2, str(s["llm_n_calls"]))
    check("重试次数", s["llm_n_retried"] == 1, str(s["llm_n_retried"]))
    check("总耗时", s["llm_ms"] == 2000, str(s["llm_ms"]))
    check("原始记录保留", s["llm_calls"][1]["label"] == "route.files", str(s))
    check("None 不炸", llm.call_stats(None)["llm_n_calls"] == 0)
    check("脏数据被跳过", llm.call_stats([None, "x", {"ms": 5}])["llm_n_calls"] == 1,
          str(llm.call_stats([None, "x", {"ms": 5}])))


def test_chat_json_forwards_label() -> None:
    print("\n[chat_json 把 label/tag 透传下去]")
    with stubbed([reply('{"dirs": [0]}')]):
        with llm.record_calls() as calls:
            ans = llm.chat_json("p", label="route.dirs", tag="t1")
        check("仍解析出 JSON", ans == {"dirs": [0]}, str(ans))
        check("记录带上了 label",
              bool(calls) and calls[0]["label"] == "route.dirs", str(calls))
        check("记录带上了 tag", bool(calls) and calls[0]["tag"] == "t1",
              str(calls))


def main() -> int:
    print("=" * 74)
    print("LLM 重试与预算升级测试（stub 掉 litellm，不联网）")
    print("=" * 74)
    test_escalates_on_length()
    test_escalation_capped()
    test_no_reasoning_escalates_budget_only()
    test_exhausted_raises_with_detail()
    test_transport_error_retries_unchanged()
    test_stream_fallback_gets_more_room()
    test_stream_events_split_reasoning_from_content()
    test_stream_events_reasoning_alias()
    test_stream_events_falls_back_when_only_reasoning()
    test_stream_text_still_yields_plain_strings()
    test_json_bare_identifiers()
    test_ints_tolerates_prefixes()
    test_calls_are_recorded_on_success()
    test_retry_is_visible_in_the_record()
    test_no_collector_is_a_noop()
    test_bucket_is_isolated_across_threads()
    test_call_stats_rollup()
    test_chat_json_forwards_label()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
