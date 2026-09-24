"""Shared LLM helper: one call, JSON out, with repair and retry."""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Optional

from superindex.runtime import getenv, load_env

load_env()

DEFAULT_MODEL = os.getenv("NAV_MODEL", getenv("SUPERINDEX_CHAT_MODEL",
                                              "deepseek/deepseek-flash"))
# Reasoning is the single biggest latency lever: on routing prompts the model
# can spiral on open-ended tasks and burn the whole output budget without
# emitting content. "none" disables it outright; "low" is the safe middle.
DEFAULT_EFFORT = os.getenv("NAV_REASONING_EFFORT", "none")


def _litellm():
    import litellm
    litellm.suppress_debug_info = True
    return litellm


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON value out of a model reply.

    Models wrap JSON in prose or fences; some emit trailing commas. Try the
    text as-is, then a fence-stripped slice, then a comma-repaired variant.
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
    for candidate in (body,
                      " ".join(body.split()),
                      re.sub(r",(\s*[\]}])", r"\1", body)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def chat(prompt: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
         max_tokens: int = 2048, retries: int = 2) -> str:
    """One completion. Retries on empty content or transport errors."""
    litellm = _litellm()
    kwargs: dict[str, Any] = {"max_tokens": max_tokens}
    if effort:
        kwargs["reasoning_effort"] = effort
    last = ""
    for attempt in range(retries + 1):
        try:
            resp = litellm.completion(model=model,
                                      messages=[{"role": "user", "content": prompt}],
                                      **kwargs)
            content = (resp.choices[0].message.content or "").strip()
            if content:
                return content
            last = f"(empty content, finish_reason={resp.choices[0].finish_reason})"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"LLM call failed after {retries + 1} attempts: {last}")


def chat_json(prompt: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT,
              max_tokens: int = 2048, retries: int = 2) -> Any:
    """Completion whose reply must contain JSON. Raises if none parses."""
    raw = chat(prompt, model=model, effort=effort, max_tokens=max_tokens,
               retries=retries)
    parsed = extract_json(raw)
    if parsed is None:
        raise RuntimeError(f"reply contained no JSON: {raw[:200]!r}")
    return parsed
