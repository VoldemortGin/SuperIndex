"""Shared LLM helper: one call, JSON out, with repair and retry."""
from __future__ import annotations

import contextvars
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Optional

from superindex.runtime import getenv, load_env, set_offline_defaults

load_env()

# litellm fetches its model price table over the network the first time it is
# imported, and only falls back to the bundled copy once that request fails. On
# a proxied or slow network that is a multi-second stall, and because the import
# is lazy it is charged to whichever request happens to trigger it — in the
# server, the first question after a restart (measured: 9.6s in a cold process,
# ~4s when a background call got there first and held the import lock).
# Nothing in nav/ needs live pricing, so keep the lookup local. `setdefault`,
# so an explicit value in the environment or .env still wins.
set_offline_defaults()

from superindex.nav.debuglog import error as log_error  # noqa: E402

DEFAULT_MODEL = os.getenv("NAV_MODEL", getenv("SUPERINDEX_CHAT_MODEL",
                                              "deepseek/deepseek-flash"))
# Reasoning is the single biggest latency lever: on routing prompts the model
# can spiral on open-ended tasks and burn the whole output budget without
# emitting content. "none" disables it outright; "low" is the safe middle.
DEFAULT_EFFORT = os.getenv("NAV_REASONING_EFFORT", "none")

# Upper bound for the token-budget escalation below.
MAX_TOKEN_CEILING = int(os.getenv("NAV_MAX_TOKEN_CEILING", "16384"))


# ── per-call instrumentation ─────────────────────────────────────────────
# A query that takes 12s because one routing call silently retried twice looks
# *identical* in the logs to one that is merely slow: the trace records stage
# totals (`stages.route_ms`), and a retry happens inside that total. Recording
# the attempt sequence turns "why was this slow?" from a correlation exercise
# into a read.
#
# A ContextVar rather than a module global, because the background indexer
# calls `chat()` on its own thread while a query is in flight. With a global,
# an indexing run would be recorded as part of the user's query.
_calls: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar(
    "nav_llm_calls", default=None)
# Worker threads share one bucket (`sections_for_all` hands the context to each
# task), so appends are serialized rather than leaning on list.append being
# atomic under the GIL.
_calls_lock = threading.Lock()


@contextmanager
def record_calls():
    """Collect one record per `chat()` call made inside this block.

    Yields the bucket — a plain list of dicts, appended as each call finishes,
    so it stays readable while the block is still running.

    Nothing is recorded when no block is active, which is what keeps the
    indexer's calls out of a query's bucket. Note that **threads spawned inside
    the block do not inherit it by themselves**: `ThreadPoolExecutor` does not
    copy contextvars, so `sections_for_all` copies the context per task by hand
    (see nav/route.py).
    """
    bucket: list = []
    token = _calls.set(bucket)
    try:
        yield bucket
    finally:
        _calls.reset(token)


def _note_call(rec: dict) -> None:
    """File one call's stats with the active bucket, if any. Never raises.

    Instrumentation must not be able to break the call it is measuring.
    """
    bucket = _calls.get()
    if bucket is None:
        return
    try:
        with _calls_lock:
            bucket.append(rec)
    except Exception:  # noqa: BLE001 - see docstring
        pass


def call_stats(calls: Optional[list]) -> dict:
    """Roll a bucket up into the fields the query record carries.

    `llm_n_retried` is the number to read first: a non-zero value means the
    model returned nothing usable at least once, so part of this query's
    latency went into recovering rather than working. `llm_ms` is the wall clock
    spent in `chat()` — compare it against `stages` to see how much of the query
    was retrieval round trips.
    """
    items = [c for c in (calls or []) if isinstance(c, dict)]
    return {
        "llm_calls": items,
        "llm_n_calls": len(items),
        "llm_n_retried": sum(1 for c in items if c.get("retried")),
        "llm_ms": sum(int(c.get("ms") or 0) for c in items),
    }


def _litellm():
    import litellm
    litellm.suppress_debug_info = True
    return litellm


def warmup() -> float:
    """Import litellm now, so the first question does not pay for it.

    The import costs seconds (it pulls in the provider SDKs and builds the model
    table) and it is deliberately lazy, which means it lands inside whichever
    call to `chat()` happens to be first. In the server that is the user's first
    question after a restart, where it is indistinguishable from the model being
    slow: measured at 9.6s in a cold process, and ~4s when a background call had
    already started the import and held the lock.

    Call this once at startup. Returns the seconds spent so the caller can show
    it rather than silently eating it — an unexplained stall at boot is still an
    unexplained stall.
    """
    t0 = time.time()
    _litellm()
    return time.time() - t0


def _quote_bare_tokens(body: str) -> str:
    """`{"dirs": [D8, D9]}` -> `{"dirs": ["D8", "D9"]}`.

    Routing prompts label candidates `[D0]`, `[D1]`, `[F3]`… and models routinely
    echo the prefix, emitting a bare identifier where JSON wants a string. That
    is a perfectly good answer in the wrong syntax — discarding it and falling
    back to keyword matching loses real quality, so quote it instead.

    Only tokens in *value* position are touched: the match starts at `[` or `,`,
    so object keys (which follow `{` or `:`) are left alone. `true` / `false` /
    `null` are left alone too.
    """
    keep = {"true", "false", "null"}

    def repl(m: "re.Match[str]") -> str:
        token = m.group(2)
        if token.lower() in keep:
            return m.group(0)
        return f'{m.group(1)}"{token}"'

    return re.sub(r'([\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)', repl, body)


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON value out of a model reply.

    Models wrap JSON in prose or fences, leave trailing commas, and emit bare
    identifiers where a string is expected. Each repair is tried in turn; the
    first that parses wins.
    """
    if not text or not text.strip():
        return None
    t = text.strip()
    if "```" in t:
        t = re.sub(r"^.*?```(?:json)?\s*", "", t, flags=re.S).split("```")[0]
    starts = [i for i in (t.find("["), t.find("{")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end = max(t.rfind("]"), t.rfind("}"))
    if end <= start:
        return None
    body = t[start:end + 1]
    tight = " ".join(body.split())
    no_trailing = re.sub(r",(\s*[\]}])", r"\1", body)
    for candidate in (body,
                      tight,
                      no_trailing,
                      _quote_bare_tokens(body),
                      _quote_bare_tokens(tight),
                      _quote_bare_tokens(no_trailing)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def chat(prompt: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
         max_tokens: int = 2048, retries: int = 2, label: str = "",
         tag: str = "") -> str:
    """One completion. Retries on empty content or transport errors.

    A `finish_reason == "length"` reply gets special handling: the model ran out
    of budget, so **retrying the identical request cannot help**. Each retry
    doubles `max_tokens` and drops `reasoning_effort`, because reasoning tokens
    are usually what consumed the budget — which is why a "low effort" call can
    return no content at all while still reporting a normal finish.

    `label` and `tag` name the call site and never reach the model: `label`
    groups calls by stage ("route.dirs"), `tag` names the individual one (which
    file, say). Both land in the record collected by `record_calls()`.
    """
    litellm = _litellm()
    budget = max_tokens
    reasoning = effort
    attempts: list[str] = []
    timings: list[int] = []          # ms per attempt, same order as `attempts`
    for attempt in range(retries + 1):
        kwargs: dict[str, Any] = {"max_tokens": budget}
        if reasoning:
            kwargs["reasoning_effort"] = reasoning
        note = ""
        content = ""
        t0 = time.time()
        try:
            resp = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                **kwargs)
            choice = resp.choices[0]
            content = (choice.message.content or "").strip()
            if not content:
                finish = choice.finish_reason
                note = (f"empty content, finish_reason={finish}, "
                        f"max_tokens={budget}, reasoning={reasoning or 'off'}")
                if finish == "length" and attempt < retries:
                    budget = min(budget * 2, MAX_TOKEN_CEILING)
                    if reasoning:
                        reasoning = ""      # free the budget for actual content
                        note += f" -> retry with max_tokens={budget}, reasoning off"
                    else:
                        note += f" -> retry with max_tokens={budget}"
        except Exception as exc:  # noqa: BLE001
            note = f"{type(exc).__name__}: {exc} (max_tokens={budget})"
        timings.append(int((time.time() - t0) * 1000))
        if content:
            # Recorded on the *success* path too, and that is the whole point:
            # a call that retried once and then worked is exactly the silent
            # slowdown this exists to expose. Only the failure path used to
            # leave a trace, and it leaves one in errors.jsonl, where nobody
            # looks when the answer eventually arrived.
            _note_call({
                "label": label or "llm",
                "tag": tag,
                "model": model,
                "ms": sum(timings),
                "attempts": len(timings),
                "attempt_ms": list(timings),
                "retried": len(timings) > 1,
                "prompt_chars": len(prompt),
            })
            return content
        attempts.append(f"#{attempt + 1}: {note}")
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    detail = " | ".join(attempts)
    log_error(RuntimeError(detail), where="llm.chat", model=model,
              prompt_chars=len(prompt), attempts=attempts, attempt_ms=timings,
              label=label)
    raise RuntimeError(
        f"LLM call failed after {retries + 1} attempts: {detail}")


def chat_json(prompt: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
              max_tokens: int = 2048, retries: int = 2, label: str = "",
              tag: str = "") -> Any:
    """Completion whose reply must contain JSON. Raises if none parses."""
    raw = chat(prompt, model=model, effort=effort, max_tokens=max_tokens,
               retries=retries, label=label, tag=tag)
    parsed = extract_json(raw)
    if parsed is None:
        raise RuntimeError(f"reply contained no JSON: {raw[:200]!r}")
    return parsed


def chat_stream(prompt: str, model: str = DEFAULT_MODEL,
                effort: str = DEFAULT_EFFORT, max_tokens: int = 2048):
    """Yield answer deltas as they arrive.

    Used by the web UI, where waiting for the whole completion makes a
    multi-second answer feel broken. Falls back to a single chunk if the
    provider does not support streaming, so callers can always treat this as
    an iterator of strings.
    """
    litellm = _litellm()
    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "stream": True}
    if effort:
        kwargs["reasoning_effort"] = effort
    resp = litellm.completion(model=model,
                              messages=[{"role": "user", "content": prompt}],
                              **kwargs)
    for chunk in resp:
        try:
            delta = chunk.choices[0].delta.content
        except (AttributeError, IndexError):
            delta = None
        if delta:
            yield delta


def chat_stream_text(prompt: str, model: str = DEFAULT_MODEL,
                     effort: str = DEFAULT_EFFORT, max_tokens: int = 2048):
    """chat_stream with a non-streaming fallback, always yielding at least once.

    Some providers return an **empty stream** rather than an error when the
    output budget is exhausted. Falling back with the same budget would fail the
    same way, so the fallback gets twice the room (capped), and `chat()`'s own
    escalation handles it from there.
    """
    got = False
    try:
        for delta in chat_stream(prompt, model=model, effort=effort,
                                 max_tokens=max_tokens):
            got = True
            yield delta
    except Exception:  # noqa: BLE001 - provider may not stream; fall through
        if got:
            raise
    if not got:
        yield chat(prompt, model=model, effort=effort,
                   max_tokens=min(max_tokens * 2, MAX_TOKEN_CEILING))


def chat_stream_events(prompt: str, model: str = DEFAULT_MODEL,
                       effort: str = DEFAULT_EFFORT, max_tokens: int = 2048):
    """Stream **both** the model's thinking and its answer, tagged by kind.

    Yields `("reasoning", text)` and `("content", text)` pairs. The reasoning is
    the model's private scratchpad — a reasoning model emits it in a separate
    `reasoning_content` field, and on our own probe of `deepseek-flash` it was
    **240 of 269 chunks, against 27 chunks of answer**. Those tokens are
    generated whether or not anyone reads them, so discarding them buys nothing
    and costs the one thing a slow answer cannot spare: evidence that something
    is happening. Forwarding them moves the first visible sign of life from the
    first *answer* token to the first *thinking* token — 0.4s instead of ~2s on
    a small prompt, and far wider apart on a real 20K-character one.

    Callers that only want the answer keep using `chat_stream_text()`; this
    function exists so the UI can show the difference.

    The fallback mirrors `chat_stream_text`: if no **content** ever arrives (an
    exhausted budget can return an empty stream rather than an error), retry
    non-streaming with twice the room. Reasoning alone does not count as
    arrival — a turn that only thinks and never answers would otherwise stream
    a long silence and then stop with an empty reply.
    """
    litellm = _litellm()
    kwargs: dict[str, Any] = {"max_tokens": max_tokens, "stream": True}
    if effort:
        kwargs["reasoning_effort"] = effort
    got_content = False
    try:
        resp = litellm.completion(model=model,
                                  messages=[{"role": "user", "content": prompt}],
                                  **kwargs)
        for chunk in resp:
            try:
                delta = chunk.choices[0].delta
            except (AttributeError, IndexError):
                continue
            # `reasoning_content` is the field DeepSeek-family models use; some
            # providers put it under `reasoning`. Read both, prefer the former.
            thinking = (getattr(delta, "reasoning_content", None)
                        or getattr(delta, "reasoning", None))
            if thinking:
                yield ("reasoning", thinking)
            content = getattr(delta, "content", None)
            if content:
                got_content = True
                yield ("content", content)
    except Exception:  # noqa: BLE001 - provider may not stream; fall through
        if got_content:
            raise
    if not got_content:
        yield ("content", chat(prompt, model=model, effort=effort,
                               max_tokens=min(max_tokens * 2,
                                              MAX_TOKEN_CEILING)))
