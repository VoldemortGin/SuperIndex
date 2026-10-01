"""Structured debug logging — what to read when an answer is wrong or a call blows up.

Two append-only JSONL streams under `results/logs/`, so they can be tailed,
grepped, or loaded straight into pandas:

* **`queries.jsonl`** — one record per question: the scope it ran against, every
  routing decision, the sources it read, the answer, and the timings. This is
  the file to open when the *answer* is wrong: it shows whether the model picked
  the wrong directory, the wrong file, or the wrong section — and whether a
  fallback fired.
* **`errors.jsonl`** — one record per exception, with the full traceback and
  whatever context was in flight. Every record carries the `id` of the query it
  belongs to, so an error can be joined back to its query record.

Both files are gitignored build output. `SUPERINDEX_DEBUG_LOG=0` turns logging
off; `SUPERINDEX_LOG_DIR` moves it.

**Logging must never break the app.** Every write is wrapped: a failure to log
prints one line to stderr and is otherwise swallowed.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from superindex.runtime import app_dir

# Next to the other run outputs (`superindex batch` writes results/batch/):
# the working directory from source, the executable's folder when frozen.
LOG_DIR = Path(os.getenv("SUPERINDEX_LOG_DIR") or (app_dir() / "results" / "logs"))
QUERY_LOG = LOG_DIR / "queries.jsonl"
ERROR_LOG = LOG_DIR / "errors.jsonl"

# Rotate at 16 MB so a long-running server cannot fill the disk.
MAX_BYTES = int(os.getenv("SUPERINDEX_LOG_MAX_BYTES", str(16 * 1024 * 1024)))
# Long answers are the bulk of a record; keep enough to eyeball, not all of it.
ANSWER_KEEP = int(os.getenv("SUPERINDEX_LOG_ANSWER_CHARS", "4000"))

_lock = threading.Lock()
_warned = False


def enabled() -> bool:
    return (os.getenv("SUPERINDEX_DEBUG_LOG") or "1").strip().lower() not in {
        "0", "false", "no", "off"}


def new_id(prefix: str = "q") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _append(path: Path, record: dict) -> None:
    """Append one JSON line. Never raises."""
    global _warned
    if not enabled():
        return
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.is_file() and path.stat().st_size > MAX_BYTES:
                    path.replace(path.with_suffix(path.suffix + ".1"))
            except OSError:
                pass
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 - logging must not break the caller
        if not _warned:
            _warned = True
            print(f"[debuglog] disabled after write failure: {exc}", file=sys.stderr)


# ── errors ───────────────────────────────────────────────────────────────
def error(exc: BaseException, *, where: str = "", **context: Any) -> str:
    """Record an exception with its traceback. Returns the record id.

    Pass `query_id=` to correlate with a `queries.jsonl` record.
    """
    rec = {
        "ts": _now(),
        "id": new_id("e"),
        "where": where,
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)).strip(),
        "context": context or {},
    }
    _append(ERROR_LOG, rec)
    return rec["id"]


# ── query trace ──────────────────────────────────────────────────────────
class QueryTrace:
    """Accumulates one question's story and writes it as a single record.

    Usage:
        trace = QueryTrace(question, scope_names)
        trace.route_step(...)
        trace.sources([...])
        trace.finish(answer_text)
        # or, on failure:
        trace.fail(exc, stage="answer")
    """

    def __init__(self, question: str, scope: Optional[list[str]] = None,
                 *, model: str = ""):
        self.id = new_id("q")
        self.record: dict[str, Any] = {
            "ts": _now(),
            "id": self.id,
            "question": question,
            "scope": list(scope or []),
            "model": model,
            "steps": [],
            "files": [],
            "sections": [],
            "answer": "",
            "ok": False,
            "error": None,
            "stages": {},
            # Which business routing policy was in effect. Recorded per query
            # because "I added a directory weight and nothing changed" is
            # otherwise indistinguishable from "the weight never matched".
            "policy": {},
        }
        self._t0 = time.time()
        self._marks: dict[str, float] = {}

    # ── progress ─────────────────────────────────────────────────────────
    def mark(self, stage: str) -> None:
        """Record a timing checkpoint; also stored as `stages[<stage>_ms]`."""
        self._marks[stage] = time.time()

    def elapsed(self, stage: str) -> int:
        t = self._marks.get(stage)
        return int((time.time() - t) * 1000) if t else 0

    def route_step(self, *, level: str, where: str, detail: str = "",
                   picked: Optional[list[str]] = None, note: str = "") -> None:
        self.record["steps"].append({
            "level": level, "where": where, "detail": detail,
            "picked": list(picked or []), "note": note,
        })

    def files(self, rel_paths: list[str]) -> None:
        self.record["files"] = list(rel_paths)

    def sources(self, items: list[dict]) -> None:
        self.record["sections"] = list(items)

    def stage_ms(self, name: str, ms: int) -> None:
        self.record["stages"][name] = ms

    def policy(self, info: dict) -> None:
        """Note the routing policy this query ran under.

        Cheap, and the only way to answer "did my config actually load?"
        after the fact — an empty `source` means no policy file was found.
        """
        self.record["policy"] = dict(info or {})

    # ── terminal states ──────────────────────────────────────────────────
    def finish(self, answer: str = "", **extra: Any) -> dict:
        self.record["ok"] = True
        self.record["answer"] = (answer or "")[:ANSWER_KEEP]
        self.record["answer_chars"] = len(answer or "")
        self.record["ms"] = int((time.time() - self._t0) * 1000)
        self.record.update(extra)
        _append(QUERY_LOG, self.record)
        return self.record

    def abort(self, reason: str, **extra: Any) -> dict:
        """Ran to completion but found nothing usable (no files / no sections).

        Distinct from `fail()`: nothing raised, so this belongs in
        queries.jsonl only — an empty result is a quality signal, not a bug.
        """
        self.record["ok"] = False
        self.record["error"] = reason
        self.record["ms"] = int((time.time() - self._t0) * 1000)
        self.record.update(extra)
        _append(QUERY_LOG, self.record)
        return self.record

    def fail(self, exc: BaseException, *, stage: str = "", **extra: Any) -> dict:
        """Record the failure both here and in errors.jsonl, sharing this id."""
        self.record["ok"] = False
        self.record["error"] = f"{type(exc).__name__}: {exc}"
        self.record["failed_at"] = stage
        self.record["ms"] = int((time.time() - self._t0) * 1000)
        self.record.update(extra)
        _append(QUERY_LOG, self.record)
        error(exc, where=f"query.{stage}" if stage else "query",
              query_id=self.id, question=self.record["question"],
              scope=self.record["scope"])
        return self.record


# ── reading back ─────────────────────────────────────────────────────────
def read(kind: str = "queries", limit: int = 50,
         *, only_failed: bool = False, query_id: str = "") -> list[dict]:
    """Read recent records, newest first. Used by the API and the CLI."""
    path = {"queries": QUERY_LOG, "errors": ERROR_LOG}.get(kind)
    if path is None or not path.is_file():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if only_failed and kind == "queries" and rec.get("ok"):
                    continue
                if query_id and rec.get("id") != query_id \
                        and rec.get("context", {}).get("query_id") != query_id:
                    continue
                out.append(rec)
    except OSError:
        return []
    return list(reversed(out))[:limit]


def stats() -> dict:
    """Cheap summary for the UI: counts and the most recent failure."""
    def count(p: Path) -> int:
        if not p.is_file():
            return 0
        try:
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError:
            return 0

    recent = read("errors", limit=1)
    return {
        "enabled": enabled(),
        "dir": str(LOG_DIR),
        "queries": count(QUERY_LOG),
        "errors": count(ERROR_LOG),
        "last_error": recent[0] if recent else None,
    }


__all__ = ["QueryTrace", "enabled", "error", "new_id", "read", "stats",
           "LOG_DIR", "QUERY_LOG", "ERROR_LOG"]
