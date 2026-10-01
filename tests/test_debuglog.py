#!/usr/bin/env python3
"""
Tests for nav/debuglog.py — structured query/error logging.

Everything writes to a temp directory; the real results/logs/ is untouched.

    python tests/test_debuglog.py
    pytest tests/test_debuglog.py
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from superindex.nav import debuglog  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  — {detail}" if detail else ""))


class Sandbox:
    """Point debuglog at a temp dir for the duration of a test."""

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="debuglog-test-"))
        self.saved = (debuglog.LOG_DIR, debuglog.QUERY_LOG, debuglog.ERROR_LOG)
        debuglog.LOG_DIR = self.tmp
        debuglog.QUERY_LOG = self.tmp / "queries.jsonl"
        debuglog.ERROR_LOG = self.tmp / "errors.jsonl"
        return self.tmp

    def __exit__(self, *exc):
        debuglog.LOG_DIR, debuglog.QUERY_LOG, debuglog.ERROR_LOG = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)


def lines(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip()]


# ── tests ────────────────────────────────────────────────────────────────
def test_query_record() -> None:
    print("\n[查询记录]")
    with Sandbox() as tmp:
        t = debuglog.QueryTrace("问题一", ["语料A", "语料B"], model="m")
        t.route_step(level="dir", where="全树 10 个", picked=["a/2024"])
        t.files(["a/2024/x.md"])
        t.sources([{"file": "a/2024/x.md", "title": "股息", "start": 1, "end": 5}])
        t.stage_ms("route", 1234)
        rec = t.finish("这是回答")

        got = lines(debuglog.QUERY_LOG)
        check("写入了 1 条", len(got) == 1, str(len(got)))
        r = got[0]
        check("id 一致", r["id"] == t.id == rec["id"])
        check("问题正确", r["question"] == "问题一")
        check("范围正确", r["scope"] == ["语料A", "语料B"])
        check("ok=True", r["ok"] is True)
        check("回答了内容", r["answer"] == "这是回答")
        check("answer_chars 正确", r["answer_chars"] == 4, str(r["answer_chars"]))
        check("记录了检索步骤", len(r["steps"]) == 1 and r["steps"][0]["level"] == "dir")
        check("记录了文件", r["files"] == ["a/2024/x.md"])
        check("记录了来源", r["sections"][0]["title"] == "股息")
        check("记录了分阶段耗时", r["stages"]["route"] == 1234, str(r["stages"]))
        check("有耗时", isinstance(r["ms"], int) and r["ms"] >= 0)
        check("有 ISO 时间戳", "T" in r["ts"])


def test_thinking_chars_recorded() -> None:
    """`thinking_chars` is what answers "why was this query slow?".

    A reasoning model spends most of its wall clock emitting tokens nobody
    sees. Without this field the only visible symptom is a slow query, and the
    cause is indistinguishable from expensive retrieval or a huge context.
    """
    print("\n[记录思考量 —— 慢查询的归因字段]")
    with Sandbox() as tmp:
        t = debuglog.QueryTrace("问题")
        t.finish("答案", context_chars=1234, thinking_chars=5678)
        r = lines(debuglog.QUERY_LOG)[0]
        check("thinking_chars 落库", r["thinking_chars"] == 5678,
              str(r.get("thinking_chars")))
        check("context_chars 落库", r["context_chars"] == 1234,
              str(r.get("context_chars")))
        check("answer_chars 仍是答案长度", r["answer_chars"] == 2,
              str(r["answer_chars"]))
        # 未传时不应凭空出现字段 —— 老的记录格式保持不变。
        debuglog.QueryTrace("问题2").finish("答")
        r2 = lines(debuglog.QUERY_LOG)[1]
        check("没传就不写该字段", "thinking_chars" not in r2, str(r2.keys()))


def test_abort_vs_fail() -> None:
    print("\n[abort（没找到）与 fail（抛异常）的区别]")
    with Sandbox():
        t1 = debuglog.QueryTrace("找不到的问题")
        t1.abort("no files located")
        r1 = lines(debuglog.QUERY_LOG)[-1]
        check("abort 记入 queries", r1["ok"] is False)
        check("abort 的原因记下了", r1["error"] == "no files located")
        check("abort 不写 errors.jsonl", len(lines(debuglog.ERROR_LOG)) == 0,
              str(len(lines(debuglog.ERROR_LOG))))

        t2 = debuglog.QueryTrace("会炸的问题")
        try:
            raise ValueError("boom")
        except ValueError as exc:
            t2.fail(exc, stage="answer")
        q = lines(debuglog.QUERY_LOG)[-1]
        e = lines(debuglog.ERROR_LOG)[-1]
        check("fail 记入 queries（ok=false）", q["ok"] is False)
        check("fail 记入 errors", e["type"] == "ValueError", e["type"])
        check("errors 里有 traceback", "ValueError: boom" in e["traceback"])
        check("errors 带 where", e["where"] == "query.answer", e["where"])
        check("errors 的 context 关联到 query_id",
              e["context"].get("query_id") == t2.id, str(e["context"]))
        check("失败阶段记下了", q["failed_at"] == "answer", q.get("failed_at"))


def test_read_filters() -> None:
    print("\n[读取与过滤]")
    with Sandbox():
        for i in range(3):
            debuglog.QueryTrace(f"问题{i}").finish(f"答{i}")
        debuglog.QueryTrace("失败的问题").abort("nothing")

        all_recs = debuglog.read("queries", limit=10)
        check("读到 4 条", len(all_recs) == 4, str(len(all_recs)))
        check("最新在前", all_recs[0]["question"] == "失败的问题",
              all_recs[0]["question"])

        failed = debuglog.read("queries", limit=10, only_failed=True)
        check("only_failed 只剩 1 条", len(failed) == 1, str(len(failed)))
        check("only_failed 内容正确", failed[0]["question"] == "失败的问题")

        check("limit 生效", len(debuglog.read("queries", limit=2)) == 2)
        check("未知 kind 返回空", debuglog.read("nope") == [])


def test_query_id_correlation() -> None:
    print("\n[按 query_id 关联查询与异常]")
    with Sandbox():
        t = debuglog.QueryTrace("会失败的问题")
        try:
            raise RuntimeError("kaboom")
        except RuntimeError as exc:
            t.fail(exc, stage="ask")
        debuglog.QueryTrace("无关的问题").finish("ok")

        by_id = debuglog.read("queries", limit=10, query_id=t.id)
        check("能按 id 取到查询", len(by_id) == 1 and by_id[0]["id"] == t.id,
              str([r["id"] for r in by_id]))
        errs = debuglog.read("errors", limit=10, query_id=t.id)
        check("能按 id 取到关联异常", len(errs) == 1, str(len(errs)))


def test_never_raises() -> None:
    print("\n[日志失败不能影响主流程]")
    with Sandbox() as tmp:
        # 把日志路径指向一个不可能写入的位置
        debuglog.QUERY_LOG = Path("/proc/nonexistent/queries.jsonl")
        debuglog.ERROR_LOG = Path("/proc/nonexistent/errors.jsonl")
        try:
            debuglog.QueryTrace("x").finish("y")
            debuglog.error(ValueError("z"), where="test")
            check("写入失败时静默降级", True)
        except Exception as exc:  # noqa: BLE001
            check("写入失败时静默降级", False, f"{type(exc).__name__}: {exc}")


def test_disabled() -> None:
    print("\n[SUPERINDEX_DEBUG_LOG=0 时关闭]")
    with Sandbox() as tmp:
        os.environ["SUPERINDEX_DEBUG_LOG"] = "0"
        try:
            debuglog.QueryTrace("不该被记录").finish("x")
            debuglog.error(ValueError("y"), where="test")
            check("关闭时不写文件", not (tmp / "queries.jsonl").exists()
                  and not (tmp / "errors.jsonl").exists())
            check("enabled() 返回 False", debuglog.enabled() is False)
        finally:
            os.environ.pop("SUPERINDEX_DEBUG_LOG", None)


def test_rotation() -> None:
    print("\n[超过上限时轮转]")
    with Sandbox() as tmp:
        saved = debuglog.MAX_BYTES
        debuglog.MAX_BYTES = 400          # 强制很快轮转
        try:
            for i in range(30):
                debuglog.QueryTrace(f"问题{i}" * 10).finish("答" * 50)
            rotated = tmp / "queries.jsonl.1"
            check("产生了轮转文件", rotated.is_file())
            check("当前文件仍在", (tmp / "queries.jsonl").is_file())
            check("轮转后当前文件变小",
                  (tmp / "queries.jsonl").stat().st_size < 2000,
                  str((tmp / "queries.jsonl").stat().st_size))
        finally:
            debuglog.MAX_BYTES = saved


def test_stats() -> None:
    print("\n[统计]")
    with Sandbox():
        debuglog.QueryTrace("a").finish("x")
        debuglog.QueryTrace("b").abort("nothing")
        try:
            raise ValueError("e")
        except ValueError as exc:
            debuglog.error(exc, where="test")
        st = debuglog.stats()
        check("查询计数正确", st["queries"] == 2, str(st["queries"]))
        check("异常计数正确", st["errors"] == 1, str(st["errors"]))
        check("带 last_error", (st["last_error"] or {}).get("type") == "ValueError",
              str(st["last_error"]))
        check("带目录", bool(st["dir"]))


def main() -> int:
    print("=" * 74)
    print("debuglog 测试（写入临时目录，不碰真实 results/logs）")
    print("=" * 74)
    test_query_record()
    test_thinking_chars_recorded()
    test_abort_vs_fail()
    test_read_filters()
    test_query_id_correlation()
    test_never_raises()
    test_disabled()
    test_rotation()
    test_stats()
    print()
    print("=" * 74)
    print(f"  通过 {len(PASS)}  失败 {len(FAIL)}")
    if FAIL:
        print("  失败项: " + ", ".join(FAIL))
    print("=" * 74)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
