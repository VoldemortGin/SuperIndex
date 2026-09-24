"""Own-model chat: document-QA agents over the local or cloud agent
tools, and the runs behind both ChatStream views, which also weave the
managed endpoint's chunk stream."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import queue
import threading
import time
import uuid
from typing import Any, Callable, Iterator, Mapping, Optional, Union

from .agent_tools import _base_instructions, targeting_block
from .chat_stream import ChatStream
from .errors import SuperIndexAPIError, _superindex_cause

CHAT_HEADER = (
    "You are SuperIndex, a document-focused assistant. "
    "Be concise, never use emojis, and do not expose tool names."
)


# ── shared: prompt, doc targeting, validation, sync bridges ──

def _managed_instructions(client, extra_system: list[str]) -> str:
    # Local: the built-in subset guidance. Own-model chat over cloud
    # documents: the live instructions the MCP server serves.
    base: str = _base_instructions(client)
    return "\n\n".join([CHAT_HEADER, base,
                        *[t for t in extra_system if t.strip()]])


def _system_text(content: Any) -> str:
    """Text of a system/developer message: a string, or text parts joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [part["text"] for part in content
                 if isinstance(part, dict) and isinstance(part.get("text"), str)]
        if texts and len(texts) == len(content):
            return "\n".join(texts)
    raise SuperIndexAPIError(
        "system message content must be a string or a list of text parts."
    )


def _split_chat_messages(messages) -> "tuple[list[str], list[dict]]":
    """Validate the chat_completions surface's messages: system/developer
    content joins the managed instructions; user/assistant history passes
    through. Tool-history round-trips belong to chat(protocol="responses")."""
    messages = list(messages)
    if not messages:
        raise SuperIndexAPIError("messages must be a non-empty list.")
    system_texts: list[str] = []
    history: list[dict] = []
    for message in messages:
        if not isinstance(message, dict) or "role" not in message:
            raise SuperIndexAPIError(
                "Each message must be a dict with 'role' and 'content'.")
        role = message["role"]
        if role in ("system", "developer"):
            system_texts.append(_system_text(message.get("content")))
        elif role in ("user", "assistant"):
            content = message.get("content")
            if not isinstance(content, str):
                raise SuperIndexAPIError(
                    "content must be a string on this lane; for "
                    "structured items use chat(protocol=\"responses\")."
                )
            history.append({"role": role, "content": content})
        else:
            raise SuperIndexAPIError(
                f"Unsupported role {role!r} on this lane. Tool history "
                "round-trips belong to chat(protocol=\"responses\")."
            )
    if not history:
        raise SuperIndexAPIError("messages must contain a user or assistant "
                                "message.")
    return system_texts, history


def _run_sync(coro):
    from .utils import run_off_loop
    return run_off_loop(asyncio.run, coro)


_SENTINEL = object()


def _stream_sync(agen_factory) -> Iterator[Any]:
    """Drive an async generator from a background thread; yield synchronously.

    Closing the iterator cancels the run between items: the pump stops, and
    the async generator's cleanup cancels the underlying agent task, so no
    further model turns or tool executions start. An in-flight backend
    request cannot be aborted mid-turn.
    """
    items: "queue.Queue[Any]" = queue.Queue(maxsize=32)
    cancelled = threading.Event()

    def deliver(item) -> bool:
        while not cancelled.is_set():
            try:
                items.put(item, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def pump():
        async def consume():
            agen = agen_factory()

            async def drain():
                async for item in agen:
                    if not deliver(item):
                        break

            # The watchdog lets cancellation land even while drain() is
            # awaiting the backend — a plain async-for would only notice
            # between items.
            task = asyncio.ensure_future(drain())
            try:
                while not task.done():
                    if cancelled.is_set():
                        task.cancel()
                        break
                    await asyncio.sleep(0.05)
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                await agen.aclose()

        try:
            asyncio.run(consume())
        except BaseException as exc:  # re-raised on the consumer thread
            deliver(exc)
            return
        deliver(_SENTINEL)

    threading.Thread(target=pump, daemon=True).start()
    try:
        while True:
            item = items.get()
            if item is _SENTINEL:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        cancelled.set()


# ── OpenAI engine (chat_completions / responses) ──

def _require_openai_agents(method: str) -> None:
    try:
        import agents  # noqa: F401
    except ImportError as exc:
        raise SuperIndexAPIError(
            f"{method} with your own chat model requires the OpenAI "
            "Agents SDK — "
            "pip install openai-agents."
        ) from exc


def _sdk_backend(backend) -> dict:
    """chat_backend for an SDK constructor: LiteLLM takes either endpoint
    spelling, the openai SDK only ``base_url``."""
    return {("base_url" if key == "api_base" else key): value
            for key, value in (backend or {}).items()}


def _openai_model(protocol: str, model_name: str, backend=None):
    """The backend protocol driver — the seam tests replace with a fake."""
    if protocol == "responses":
        model_name = model_name.removeprefix("litellm/")
        if "/" in model_name and not model_name.startswith("openai/"):
            raise SuperIndexAPIError(
                f"protocol='responses' cannot drive '{model_name}': "
                "provider-prefixed models route through LiteLLM, which speaks "
                "chat.completions, not the Responses API. Use chat() without "
                "protocol, or protocol='chat_completions', or point "
                "OPENAI_BASE_URL at a "
                "Responses-capable backend and use a bare or "
                "'openai/'-prefixed model name."
            )
        import openai
        model_name = model_name.removeprefix("openai/")
        try:
            sdk_client = openai.AsyncOpenAI(**_sdk_backend(backend))
        except (openai.OpenAIError, TypeError) as exc:
            raise SuperIndexAPIError(
                f"The OpenAI backend is not configured: {exc}") from exc
        # A caller-owned transport must survive the per-call close.
        sdk_client._superindex_caller_http = "http_client" in (backend or {})
        from agents.models.openai_responses import OpenAIResponsesModel
        return OpenAIResponsesModel(model_name, openai_client=sdk_client)
    try:
        from agents.extensions.models.litellm_model import LitellmModel
        import litellm
    except ImportError:
        raise SuperIndexAPIError(
            f"'{model_name}' routes through LiteLLM, but litellm is not "
            "installed. Run:  pip install 'litellm>=1.97'"
        )
    from .utils import (_litellm_model, _mute_litellm_bridge_usage_warning,
                        _quiet_litellm, _repair_litellm_types)
    _repair_litellm_types()
    _mute_litellm_bridge_usage_warning()
    _quiet_litellm()
    try:
        wire = _litellm_model(model_name)
    except litellm.NotFoundError as exc:
        raise SuperIndexAPIError(str(exc)) from exc
    return LitellmModel(wire, api_key=(backend or {}).get("api_key"),
                        base_url=(backend or {}).get("base_url"))


def _reported_model(model_name: str) -> str:
    """The name the provider actually serves — routing prefixes stripped."""
    return model_name.removeprefix("litellm/").removeprefix("openai/")


def _litellm_claude_marks(wire: str) -> Optional[dict]:
    """Claude's prompt caching is opt-in per request: on Claude models
    routed through LiteLLM (Anthropic direct, Bedrock, Vertex — each
    channel live-verified), mark the managed system prefix and the newest
    message via LiteLLM's injection param so the loop's later turns and a
    conversation's next calls read them instead of repaying full price.
    ``wire`` is the name LiteLLM itself resolves — each lane strips its
    own routing prefixes first, because the lanes normalize differently
    (the chat wire treats bare names as OpenAI shorthand; the Agents SDK
    hands bare names to LiteLLM's own resolution)."""
    try:
        from litellm import get_llm_provider
        from .utils import _quiet_litellm
        _quiet_litellm()
        model, provider, _, _ = get_llm_provider(model=wire)
    except Exception:
        return None
    if provider == "anthropic" or (provider in ("bedrock", "vertex_ai")
                                   and "claude" in model.lower()):
        # The stable prefix plus the newest message, so each turn re-reads
        # the turns before it. LiteLLM seeds nothing unprompted, so this
        # pair is the marks' sole source.
        return {"cache_control_injection_points": [
            {"location": "message", "role": "system"},
            {"location": "message", "index": -1}]}
    return None


def _cache_extra_args(model_name: str) -> Optional[dict]:
    """The chat lane's marks: normalized exactly as _litellm_model
    normalizes the wire (bare names get openai/), so this predicate
    cannot disagree with where chat_completions actually routes."""
    wire = model_name.removeprefix("litellm/")
    if "/" not in wire or wire.startswith("openai/"):
        return None
    return _litellm_claude_marks(wire)


def _openai_protocol(model_name: str) -> bool:
    """Destinations that speak the OpenAI protocol on the wire, where
    prompt_cache_key means something and extra_body lands in the request
    body. Resolution is LiteLLM's own (same as _cache_extra_args), so the
    answer follows actual routing; azure and openrouter ride the OpenAI
    protocol without appearing in openai_compatible_providers."""
    wire = model_name.removeprefix("litellm/")
    if "/" not in wire or wire.startswith("openai/"):
        return True
    try:
        import litellm
        from .utils import _quiet_litellm
        _quiet_litellm()
        _, provider, _, _ = litellm.get_llm_provider(model=wire)
    except Exception:
        return False
    return (provider in ("openai", "azure", "openrouter")
            or provider in getattr(litellm, "openai_compatible_providers",
                                   ()))


def _merged_backend(client, backend):
    """This call's connection overrides: the client's ``chat_backend``
    under the per-call dict, per-call keys winning."""
    merged = {**(getattr(client, "chat_backend", None) or {}),
              **(backend or {})}
    return merged or None


_SKELETON_KEYS = frozenset({"system", "instructions", "input", "messages",
                            "tools"})
_ARGUMENT_KEYS = frozenset({"stream", "doc_id"})


def _refuse_skeleton(extra_body) -> None:
    """The managed prompt, conversation and tools are the SDK's on every
    lane; extra_body merges last, so a caller's copy would replace them.
    Fields with their own argument select the SDK's parser and scope, so
    they are refused here too."""
    if extra_body is None:
        return
    if not isinstance(extra_body, Mapping):
        raise SuperIndexAPIError(
            "extra_body must be a dict of request fields, got "
            f"{type(extra_body).__name__}.")
    hit = sorted(_SKELETON_KEYS.intersection(extra_body))
    if hit:
        raise SuperIndexAPIError(
            f"extra_body cannot carry {', '.join(hit)}: the managed prompt, "
            "conversation and tools are the SDK's. Extend the prompt with "
            "instructions= or a leading system row; pass the conversation "
            "as messages, the first argument.")
    hit = sorted(_ARGUMENT_KEYS.intersection(extra_body))
    if hit:
        raise SuperIndexAPIError(
            f"extra_body cannot carry {', '.join(hit)}: use "
            f"{' / '.join(key + '=' for key in hit)} instead.")


def _openai_agent(client, protocol: str, model_name: str, instructions: str,
                  temperature, top_p, doc_ids=None, cache_key=None,
                  reasoning=None, reasoning_effort=None, extra_body=None,
                  max_tokens=None, backend=None, extra_headers=None):
    _refuse_skeleton(extra_body)
    from agents import Agent, ModelSettings
    from .integrations.openai_agents import build_openai_tools
    # ModelSettings.extra_body is the one channel all three engines put on
    # the wire: LiteLLM drops the bare prompt_cache_key kwarg (wire-verified),
    # and both OpenAI model classes pass extra_body through verbatim. OpenAI
    # destinations only — LiteLLM plants extra_body as a literal field in
    # other providers' request bodies, which Anthropic rejects as unknown.
    openai_backend = _openai_protocol(model_name)
    # Chat-lane effort rides extra_args: LiteLLM takes it as its own
    # top-level kwarg on every supported openai-agents version, and the
    # channel admits values outside the OpenAI enum ("none").
    extra_args = _cache_extra_args(model_name)
    if reasoning_effort:
        extra_args = {**(extra_args or {}),
                      "reasoning_effort": reasoning_effort}
    conn = _sdk_backend(backend) if backend else {}
    if conn and protocol == "chat":
        # LiteLLM takes connection params per call, except the two names
        # LitellmModel pins as its own keywords — those ride its constructor.
        lifted = {"api_key": conn.pop("api_key", None),
                  "base_url": conn.pop("base_url", None)}
        if conn:
            extra_args = {**(extra_args or {}), **conn}
        conn = {key: value for key, value in lifted.items()
                if value is not None}
    body = ({"prompt_cache_key": cache_key}
            if cache_key and openai_backend else None)
    # Caller extras merge last, so they win over ours; non-OpenAI
    # destinations take them as LiteLLM kwargs instead (see note above).
    routed: dict[str, Any] = {}
    if extra_body:
        if openai_backend:
            body = {**(body or {}), **extra_body}
        else:
            # openai-agents passes ModelSettings' own fields to litellm by
            # name beside **extra_args, so those ride their field.
            own = {field.name for field in dataclasses.fields(ModelSettings)}
            routed = {k: v for k, v in extra_body.items() if k in own}
            rest = {k: v for k, v in extra_body.items() if k not in own}
            if rest:
                extra_args = {**(extra_args or {}), **rest}
    from pydantic import ValidationError
    try:
        settings = ModelSettings(**{
            "temperature": temperature, "top_p": top_p,
            "max_tokens": max_tokens, "reasoning": reasoning,
            # Streamed runs otherwise carry no usage at all (agents forwards
            # this as stream_options only on streaming calls).
            "include_usage": True,
            "extra_body": body,
            "extra_headers": extra_headers,
            "extra_args": extra_args,
            **routed})
    except ValidationError as exc:
        raise SuperIndexAPIError(f"Invalid model settings: {exc}") from exc
    return Agent(
        name="SuperIndex",
        instructions=instructions,
        tools=build_openai_tools(client, doc_ids=doc_ids),
        model=_openai_model(protocol, model_name, conn or None),
        model_settings=settings,
    )


def _validate_max_turns(max_turns) -> None:
    if max_turns is not None and (not isinstance(max_turns, int)
                                  or max_turns < 1):
        raise SuperIndexAPIError("max_turns must be a positive integer.")


def _conversation_cache_key(model_name: str, instructions: str, doc_id,
                            items, folder_id=None) -> str:
    """Stable per-conversation cache-routing key, sent as the OpenAI
    ``prompt_cache_key`` through ModelSettings.extra_body (openai-agents
    0.20 no longer derives it from RunConfig.group_id — verified against a
    captured wire). Keyed on the prefix identity — model, instructions,
    doc targeting, first conversation item — so a conversation's
    continuations share one route without pooling unrelated conversations.
    Callers pass the conversation's own items, never the SDK-prepended
    doc-targeting block: that block is byte-identical for every
    conversation about a document and would pool them all under one key.
    doc_id and folder_id carry the targeting identity instead — the same
    opening question against different documents is different
    conversations."""
    scope = [doc_id] if isinstance(doc_id, str) else doc_id
    seed = json.dumps([model_name, instructions, scope,
                       *([folder_id] if folder_id else []),
                       items[0] if items else None],
                      sort_keys=True, default=str)
    return "pageindex-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _model_backend_error(exc, lane: str, client=None) -> SuperIndexAPIError:
    """Wrap a provider failure; the sol-class refusal (chatcmpl rejects
    function tools while reasoning is on) gets its documented exits
    appended, since the fix is a different route, not a retry. The exits
    are per-lane: of the chat lane's three, two are dead ends for a
    Responses-lane caller — it IS the other lane, and its reasoning knob is
    ``reasoning``, not ``reasoning_effort``. On a cloud client an
    auth-shaped failure gets the own-model architecture spelled out —
    the misreading it corrects ("the cloud runs my model") surfaces
    exactly here."""
    message = f"The model backend failed: {exc}"
    if "Function tools with reasoning_effort" in str(exc):
        message += (
            " — this model runs tools on the Responses lane: upgrade "
            "litellm (newer releases route it there automatically)"
        )
        message += (
            ", pass reasoning_effort (older litellm routes explicit "
            "efforts), or use chat(protocol='responses') instead."
            if lane == "chat"
            else "."
        )
    if (getattr(client, "api_key", None)
            and (getattr(exc, "status_code", None) == 401
                 or "api key" in str(exc).lower().replace("_", " "))):
        message += (
            " — note: your chat model runs in your process on your own "
            "provider credentials; the PageIndex api_key does not cover "
            "it. Set the provider key (or chat_backend)")
        message += (
            ", or drop the chat model configuration to use the managed "
            "cloud chat." if lane == "chat" else "."
        )
    return SuperIndexAPIError(message,
                             status_code=getattr(exc, "status_code", None))


def _translate_run_error(exc, max_turns, lane, client=None) -> SuperIndexAPIError:
    """The uncaught-run ladder every agent door shares."""
    from agents.exceptions import AgentsException, MaxTurnsExceeded
    if isinstance(exc, MaxTurnsExceeded):
        return _wrap_max_turns(max_turns)
    if isinstance(exc, AgentsException):
        cause = _superindex_cause(exc)
        if cause is not None:
            # a tool failure the invoker re-raised, wrapped on its way out
            return SuperIndexAPIError(str(cause), status_code=cause.status_code)
        return SuperIndexAPIError(f"The agent backend failed: {exc}")
    return _model_backend_error(exc, lane, client)


def _run_kwargs(max_turns) -> dict:
    # No traces — the caller opted into QA, not telemetry.
    from agents import RunConfig
    kwargs: dict = {"run_config": RunConfig(tracing_disabled=True)}
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    return kwargs


def _record_response_status(agent, recorded: dict) -> None:
    """Capture each turn's terminal Response status at the transport client:
    openai-agents' non-streaming path discards Response.status, so a final
    turn truncated at the output cap would otherwise report as a clean
    completion. No-op for backends without an OpenAI responses resource
    (the streaming path records from lifecycle events instead)."""
    responses = getattr(getattr(getattr(agent, "model", None), "_client", None),
                        "responses", None)
    create = getattr(responses, "create", None)
    if create is None:
        return

    async def recording_create(*args, **kwargs):
        response = await create(*args, **kwargs)
        if getattr(response, "status", None):
            recorded["status"] = response.status
            for field in ("incomplete_details", "error"):
                value = getattr(response, field, None)
                recorded[field] = (value.model_dump(mode="json")
                                   if hasattr(value, "model_dump") else value)
        # The backend's echo of what it actually ran with.
        for field in ("tool_choice", "parallel_tool_calls"):
            value = getattr(response, field, None)
            if value is not None:
                recorded[field] = (value.model_dump(mode="json")
                                   if hasattr(value, "model_dump") else value)
        return response

    responses.create = recording_create


def _record_chat_finish(agent, recorded: dict) -> None:
    """Capture each turn's native finish_reason from the raw LiteLLM
    response: openai-agents' ModelResponse drops it, so a truncated or
    content-filtered final turn would otherwise report as a clean "stop".
    The chat protocol has no transport client to hook (cf.
    _record_response_status), so this wraps the model's response fetch;
    no-op if that private seam moves."""
    model = getattr(agent, "model", None)
    fetch = getattr(model, "_fetch_response", None)
    if fetch is None:
        return

    def note(item) -> None:
        choices = getattr(item, "choices", None)
        finish = getattr(choices[0], "finish_reason", None) if choices else None
        if finish:
            recorded["finish_reason"] = finish

    class _Tee:
        """Iteration passthrough that notes each chunk; everything else
        (aclose/close/...) delegates to the provider stream itself."""

        def __init__(self, inner):
            self._inner = inner

        def __aiter__(self):
            return self

        async def __anext__(self):
            chunk = await self._inner.__anext__()
            note(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(self._inner, name)

    async def recording_fetch(*args, **kwargs):
        result = await fetch(*args, **kwargs)
        if isinstance(result, tuple):
            response, stream = result
            return response, _Tee(stream)
        note(result)
        return result

    model._fetch_response = recording_fetch


async def _aclose_backend(agent) -> None:
    """Close the per-call AsyncOpenAI client before its event loop ends —
    otherwise httpx tears down pooled connections on a closed loop and
    emits 'Task exception was never retrieved' noise. A client built on a
    caller-owned http_client stays open."""
    backend = getattr(getattr(agent, "model", None), "_client", None)
    if getattr(backend, "_superindex_caller_http", False):
        return
    close = getattr(backend, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:
            pass


async def _run_closing(agent, coro):
    try:
        return await coro
    finally:
        await _aclose_backend(agent)


def _wrap_max_turns(max_turns) -> SuperIndexAPIError:
    limit = max_turns if max_turns is not None else "the default limit"
    return SuperIndexAPIError(
        f"The agent did not finish within max_turns ({limit}). Raise "
        "max_turns, or narrow the question."
    )


def _usage_sums(raw_responses) -> "tuple[int, int, int, int, int]":
    prompt = completion = cached = cache_write = reasoning = 0
    for r in raw_responses:
        if r.usage is None:
            continue
        prompt += r.usage.input_tokens or 0
        completion += r.usage.output_tokens or 0
        details = getattr(r.usage, "input_tokens_details", None)
        cached += getattr(details, "cached_tokens", 0) or 0
        cache_write += getattr(details, "cache_write_tokens", 0) or 0
        details = getattr(r.usage, "output_tokens_details", None)
        reasoning += getattr(details, "reasoning_tokens", 0) or 0
    return prompt, completion, cached, cache_write, reasoning


def _openai_usage(raw_responses) -> dict:
    """Cross-turn sums, chat.completions dialect."""
    prompt, completion, cached, _, reasoning = _usage_sums(raw_responses)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached},
            "completion_tokens_details": {"reasoning_tokens": reasoning}}


def _responses_usage(raw_responses) -> dict:
    """Cross-turn sums, Responses dialect."""
    prompt, completion, cached, cache_write, reasoning = (
        _usage_sums(raw_responses))
    return {"input_tokens": prompt,
            "input_tokens_details": {"cached_tokens": cached,
                                     "cache_write_tokens": cache_write},
            "output_tokens": completion,
            "output_tokens_details": {"reasoning_tokens": reasoning},
            "total_tokens": prompt + completion}


def _chat_agent(client, messages, doc_id, model, temperature=None,
                top_p=None, reasoning_effort=None, extra_body=None,
                max_tokens=None, backend=None, extra_headers=None,
                folder_id=None,
                ) -> "tuple[Any, list, str]":
    """The chat lane's shared prologue: validated history, doc targeting,
    and the configured agent. Returns (agent, input items, model name)."""
    system_texts, history = _split_chat_messages(messages)
    scope = client._local_doc_scope(doc_id)
    block = targeting_block(client, doc_id, folder_id)
    items = ([{"role": "user", "content": block}] if block else []) + history
    model_name = model or client.chat_model
    managed = _managed_instructions(client, system_texts)
    agent = _openai_agent(client, "chat", model_name, managed,
                          temperature, top_p, doc_ids=scope,
                          cache_key=_conversation_cache_key(
                              model_name, managed, doc_id, history,
                              folder_id),
                          reasoning_effort=reasoning_effort,
                          extra_body=extra_body, max_tokens=max_tokens,
                          backend=_merged_backend(client, backend),
                          extra_headers=extra_headers)
    return agent, items, model_name


def _output_text(output) -> str:
    """A tool result for the display: a string as it is, the framework's
    structured items by their text, anything else as JSON with a data
    URL's base64 payload elided."""
    if isinstance(output, str):
        return output
    lines = []
    for item in output if isinstance(output, list) else [output]:
        kind = item.get("type") if isinstance(item, dict) else None
        if kind == "text":
            lines.append(item["text"])
        elif kind == "image" and str(item.get("image_url", "")).startswith("data:"):
            head, _, _ = item["image_url"].partition(",")
            lines.append(json.dumps({**item, "image_url": head + ",..."},
                                    ensure_ascii=False))
        else:
            lines.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(lines)


def _clip(text, cap: int = 200) -> str:
    """One display line: whitespace flattened, capped for the terminal."""
    flat = " ".join(str(text).split())
    if len(flat) <= cap:
        return flat
    return f"{flat[:cap]}... (+{len(flat) - cap} chars)"


_PROCESS_DEFAULTS = {"thinking": True, "tool_call": True,
                     "tool_result": True, "max_chars": 200}


def _process_options(show_process) -> dict:
    """chat(show_process=...) normalized: True (or {}) is all defaults, a
    mapping overrides per key, anything else chokes loudly."""
    if show_process is True:
        return dict(_PROCESS_DEFAULTS)
    if not isinstance(show_process, Mapping):
        raise SuperIndexAPIError(
            "show_process must be True, False, or a dict with the keys "
            "thinking / tool_call / tool_result (bools) and max_chars "
            "(int).")
    unknown = set(show_process) - set(_PROCESS_DEFAULTS)
    if unknown:
        raise SuperIndexAPIError(
            "Unknown show_process keys: "
            f"{', '.join(sorted(map(repr, unknown)))} "
            "— valid keys: thinking, tool_call, tool_result, max_chars.")
    options = {**_PROCESS_DEFAULTS, **show_process}
    for key in ("thinking", "tool_call", "tool_result"):
        if not isinstance(options[key], bool):
            raise SuperIndexAPIError(f"show_process[{key!r}] must be a bool.")
    cap = options["max_chars"]
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise SuperIndexAPIError(
            "show_process['max_chars'] must be a positive int.")
    return options


async def _chat_events_agen(client, agent, items, run_kwargs):
    """The chat stream's primitive: the run as typed event dicts —
    thinking/answer deltas, each tool call (arguments parsed when JSON)
    and its full result. Both ChatStream views are built on it."""
    import openai
    from agents import Runner
    from agents.exceptions import AgentsException, MaxTurnsExceeded
    from openai.types.responses import (
        ResponseReasoningSummaryTextDeltaEvent,
        ResponseReasoningTextDeltaEvent, ResponseTextDeltaEvent)
    streamed = Runner.run_streamed(agent, input=items, **run_kwargs)
    completed = False
    names = {}  # call_id -> tool name, to label results
    try:
        async for event in streamed.stream_events():
            if event.type == "raw_response_event":
                data = event.data
                if isinstance(data, ResponseTextDeltaEvent):
                    if data.delta:
                        yield {"type": "answer", "delta": data.delta}
                elif isinstance(data, (
                        ResponseReasoningTextDeltaEvent,
                        ResponseReasoningSummaryTextDeltaEvent)):
                    if data.delta:
                        yield {"type": "thinking", "delta": data.delta}
            elif event.type == "run_item_stream_event":
                raw = getattr(event.item, "raw_item", None)
                if event.name == "tool_called":
                    name = getattr(raw, "name", None) or "tool"
                    call_id = getattr(raw, "call_id", None)
                    if call_id:
                        names[call_id] = name
                    arguments = getattr(raw, "arguments", "") or ""
                    try:
                        arguments = json.loads(arguments)
                    except (ValueError, TypeError):
                        pass
                    yield {"type": "tool_call", "call_id": call_id,
                           "name": name, "arguments": arguments}
                elif event.name == "tool_output":
                    call_id = (raw.get("call_id") if isinstance(raw, dict)
                               else getattr(raw, "call_id", None))
                    yield {"type": "tool_result", "call_id": call_id,
                           "name": names.get(call_id, "tool"),
                           "output": event.item.output}
        completed = True
    except (MaxTurnsExceeded, AgentsException, openai.OpenAIError) as exc:
        raise _translate_run_error(exc, run_kwargs.get("max_turns"),
                                   "chat", client) from exc
    finally:
        if not completed and hasattr(streamed, "cancel"):
            streamed.cancel()  # abandoned/failed: stop the agent task
        await _aclose_backend(agent)


def _weave(events, options) -> Iterator[str]:
    """Render the typed event stream as display text: a "[thinking] "
    section per thinking burst, a "[tool_call] name args" line per call
    with its "[tool_result]" line, the answer unlabeled — labels are the
    event type names. options=None is the plain answer-only view;
    closing this generator closes the source."""
    try:
        if options is None:
            for ev in events:
                if ev["type"] == "answer":
                    yield ev["delta"]
            return
        section = None   # the open flowing section: thinking/answer/tool
        opened = False   # anything yielded yet (first section takes no gap)
        cap = options["max_chars"]
        last_call = None  # the [tool_call] line still open for nesting
        call_args = {}    # call_id -> clipped arguments, to label orphans

        def enter(kind, label: str = "") -> str:
            nonlocal section, opened
            gap = "\n\n" if opened else ""
            opened = True
            section = kind
            return gap + label

        for ev in events:
            kind = ev["type"]
            if kind == "answer":
                head = enter("answer") if section != "answer" else ""
                yield head + ev["delta"]
            elif kind == "thinking":
                if not options["thinking"]:
                    continue
                head = (enter("thinking", "[thinking] ")
                        if section != "thinking" else "")
                yield head + ev["delta"]
            elif kind == "tool_call":
                arguments = ev["arguments"]
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                clipped = _clip(arguments, cap)
                call_args[ev["call_id"]] = clipped  # even with call lines hidden
                if not options["tool_call"]:
                    continue
                last_call = ev["call_id"]
                line = f"[tool_call] {ev['name']} {clipped}"
                yield enter("tool") + line.rstrip()
            elif kind == "tool_result":
                if not options["tool_result"]:
                    continue
                out = _clip(_output_text(ev["output"]), cap)
                if section == "tool" and ev["call_id"] == last_call:
                    # directly under its own call line
                    yield f"\n[tool_result] {ev['name']}: {out}"
                else:
                    # parallel calls, or call lines hidden: standalone,
                    # arguments echoed to say whose result this is
                    args = call_args.get(ev["call_id"], "")
                    head = f"[tool_result] {ev['name']} {args}".rstrip()
                    gap = ("\n" if section == "tool" and last_call is None
                           else enter("tool"))
                    yield f"{gap}{head}: {out}"
                last_call = None
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()  # cancel the underlying run on abandonment


def _cloud_chunk_events(chunks) -> Iterator[dict]:
    """Typed events from the managed endpoint's chunk stream: answer
    deltas, and each tool call (name + accumulated arguments) from the
    block_metadata tags — the endpoint interleaves tool-argument JSON
    into delta.content, distinguished only by those tags. It streams no
    thinking and no tool results. Outside a tool block, chunks without
    block_metadata (an older server) are answer text."""
    tool = None  # [name, argument pieces] while inside a tool_use block
    try:
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            meta = chunk.get("block_metadata") or {}
            kind = meta.get("type")
            if kind == "mcp_tool_use_start":
                tool = [meta.get("tool_name") or "tool", []]
                continue
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            content = delta.get("content")
            if kind == "tool_use_stop":
                if tool is not None:
                    name, pieces = tool
                    arguments = "".join(map(str, pieces))
                    try:
                        arguments = json.loads(arguments)
                    except ValueError:
                        pass
                    yield {"type": "tool_call", "call_id": None,
                           "name": name, "arguments": arguments}
                    tool = None
                continue
            if kind == "tool_use" or tool is not None:
                # inside an open block nothing is answer text: argument
                # chunks accumulate under any tag rather than leaking
                if tool is not None and content:
                    tool[1].append(content)
                continue
            if content:
                yield {"type": "answer", "delta": content}
    finally:
        close = getattr(chunks, "close", None)
        if close is not None:
            close()


def run_cloud_chat_stream(chunks,
                          show_process: Union[bool, Mapping[str, Any]] = True,
                          ) -> ChatStream:
    """chat(stream=True) on a managed client: the text view weaves what
    the endpoint serves — tool-call lines from its block_metadata tags
    (that wire carries no thinking and no tool results); .events needs
    the in-process agent."""
    options = (None if show_process is False
               else _process_options(show_process))
    return ChatStream(
        text=lambda: _weave(_cloud_chunk_events(chunks), options),
        events=("chat events are produced by the in-process agent, "
                "which the managed chat endpoint does not serve — "
                "construct the client with chat_model=... (or a chat= "
                "model) to run the agent in your process."))


@dataclasses.dataclass
class ChatExtras:
    """Per-call additions to the streamed answer lane (``chat(stream=True,
    extras=...)`` on a client with its own chat model).

    ``user_content`` replaces the content of the last user message sent to
    the model — e.g. its text followed by ``input_image`` parts; the
    conversation's prompt cache key still covers the text form.
    ``instructions`` is appended to the system prompt. ``tools`` are extra
    Agents SDK tools, placed after the client's tools.
    ``call_model_input_filter`` is set on the run's ``RunConfig``."""

    user_content: Optional[list[dict[str, Any]]] = None
    instructions: Optional[str] = None
    tools: list[Any] = dataclasses.field(default_factory=list)
    call_model_input_filter: Optional[Callable[[Any], Any]] = None


def _apply_extras(agent, items: list, run_kwargs: dict,
                  extras: ChatExtras) -> None:
    if extras.user_content is not None:
        if not items or items[-1].get("role") != "user":
            raise SuperIndexAPIError(
                "extras.user_content replaces the last user message, but "
                "the conversation does not end with one.")
        items[-1] = {"role": "user", "content": extras.user_content}
    if extras.instructions:
        agent.instructions = f"{agent.instructions}\n\n{extras.instructions}"
    agent.tools.extend(extras.tools)
    if extras.call_model_input_filter is not None:
        run_kwargs["run_config"].call_model_input_filter = (
            extras.call_model_input_filter)


def run_chat_stream(client, messages, doc_id=None, model=None,
                    reasoning_effort=None,
                    show_process: Union[bool, Mapping[str, Any]] = False,
                    max_turns=None, backend=None, extra_headers=None,
                    extra_body=None, folder_id=None,
                    extras: Optional[ChatExtras] = None,
                    ) -> ChatStream:
    """chat(stream=True): validation and the agent build run here, eagerly;
    the run itself starts when the returned stream's chosen view is first
    consumed."""
    options = (None if show_process is False or show_process is None
               else _process_options(show_process))
    _require_openai_agents("chat")
    _validate_max_turns(max_turns)
    if isinstance(messages, str):
        if not messages.strip():
            raise SuperIndexAPIError(
                "messages must be a non-empty string or a list of "
                "message dicts.")
        messages = [{"role": "user", "content": messages}]
    agent, items, _ = _chat_agent(client, messages, doc_id, model,
                                  reasoning_effort=reasoning_effort,
                                  extra_body=extra_body, backend=backend,
                                  extra_headers=extra_headers,
                                  folder_id=folder_id)
    run_kwargs = _run_kwargs(max_turns)
    if extras is not None:
        _apply_extras(agent, items, run_kwargs, extras)

    def events():
        return _stream_sync(
            lambda: _chat_events_agen(client, agent, items, run_kwargs))

    return ChatStream(text=lambda: _weave(events(), options), events=events)


def run_chat_completions(client, messages, stream: bool = False,
                         doc_id=None, temperature: Optional[float] = None,
                         stream_metadata: bool = False,
                         enable_citations: bool = False,
                         model: Optional[str] = None,
                         max_turns: Optional[int] = None,
                         top_p: Optional[float] = None,
                         max_tokens: Optional[int] = None,
                         reasoning_effort: Optional[str] = None,
                         extra_body: Optional[dict] = None,
                         extra_headers: Optional[dict] = None,
                         backend: Optional[dict] = None,
                         folder_id: Optional[str] = None,
                         ) -> Union[dict, Iterator[str], Iterator[dict]]:
    if enable_citations:
        raise SuperIndexAPIError(
            "enable_citations needs the managed chat endpoint — "
            + ("drop the chat model configuration to use it, or "
               if getattr(client, "api_key", None) else "")
            + "cite with your own model via chat(citations=True).")
    _require_openai_agents("chat")
    _validate_max_turns(max_turns)
    agent, items, model_name = _chat_agent(
        client, messages, doc_id, model, temperature=temperature,
        top_p=top_p, reasoning_effort=reasoning_effort,
        extra_body=extra_body, max_tokens=max_tokens, backend=backend,
        extra_headers=extra_headers, folder_id=folder_id)
    reported_model = _reported_model(model_name)
    recorded: dict = {}
    _record_chat_finish(agent, recorded)
    run_kwargs = _run_kwargs(max_turns)
    import openai
    from agents import Runner
    from agents.exceptions import AgentsException, MaxTurnsExceeded
    if not stream:
        try:
            result = _run_sync(_run_closing(agent,
                Runner.run(agent, input=items, **run_kwargs)))
        except (MaxTurnsExceeded, AgentsException,
                openai.OpenAIError) as exc:
            raise _translate_run_error(exc, max_turns, "chat", client) from exc
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": reported_model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": result.final_output or ""},
                "finish_reason": recorded.get("finish_reason") or "stop",
            }],
            "usage": _openai_usage(result.raw_responses),
        }

    chat_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    def chunk(delta: dict, finish=None) -> dict:
        return {
            "id": chat_id, "object": "chat.completion.chunk",
            "created": created, "model": reported_model,
            "choices": [{"index": 0, "delta": delta,
                         "finish_reason": finish}],
        }

    async def agen():
        from openai.types.responses import ResponseTextDeltaEvent
        streamed = Runner.run_streamed(agent, input=items, **run_kwargs)
        completed = False
        # First yield inside the try: a consumer that stops on the opening
        # chunk must still tear the run down via the finally below.
        try:
            yield chunk({"role": "assistant", "content": ""})
            async for event in streamed.stream_events():
                if (event.type == "raw_response_event"
                        and isinstance(event.data, ResponseTextDeltaEvent)):
                    yield chunk({"content": event.data.delta})
            completed = True
        except (MaxTurnsExceeded, AgentsException,
                openai.OpenAIError) as exc:
            raise _translate_run_error(exc, max_turns, "chat", client) from exc
        finally:
            if not completed and hasattr(streamed, "cancel"):
                streamed.cancel()  # abandoned/failed: stop the agent task
            await _aclose_backend(agent)
        yield chunk({}, finish=recorded.get("finish_reason") or "stop")
        yield {
            "id": chat_id, "object": "chat.completion.chunk",
            "created": created, "model": reported_model, "choices": [],
            "usage": _openai_usage(streamed.raw_responses),
        }

    if stream_metadata:
        return _stream_sync(agen)
    return (piece["choices"][0]["delta"]["content"]
            for piece in _stream_sync(agen)
            if piece.get("choices")
            and "content" in piece["choices"][0]["delta"]
            and piece["choices"][0]["delta"]["content"])


def run_responses(client, input, model: Optional[str] = None,
                  stream: bool = False, doc_id=None,
                  instructions: Optional[str] = None,
                  temperature: Optional[float] = None,
                  top_p: Optional[float] = None,
                  max_turns: Optional[int] = None,
                  max_output_tokens: Optional[int] = None,
                  reasoning: Optional[dict] = None,
                  extra_body: Optional[dict] = None,
                  extra_headers: Optional[dict] = None,
                  backend: Optional[dict] = None,
                  folder_id: Optional[str] = None,
                  ) -> Union[dict, Iterator[dict]]:
    _require_openai_agents("chat(protocol='responses')")
    _validate_max_turns(max_turns)
    if isinstance(input, str) and input.strip():
        items = [{"role": "user", "content": input}]
    elif (isinstance(input, list) and input
            and all(isinstance(item, dict) for item in input)):
        items = list(input)
    else:
        raise SuperIndexAPIError("messages must be a non-empty string or list "
                                "of item dicts.")
    scope = client._local_doc_scope(doc_id)
    block = targeting_block(client, doc_id, folder_id)
    conversation = items
    if block:
        items = [{"role": "user", "content": block}] + items
    extra = [instructions] if instructions else []
    model_name = model or client.chat_model
    managed = _managed_instructions(client, extra)
    agent = _openai_agent(client, "responses", model_name, managed,
                          temperature, top_p, doc_ids=scope,
                          cache_key=_conversation_cache_key(
                              model_name, managed, doc_id, conversation,
                              folder_id),
                          reasoning=reasoning, extra_body=extra_body,
                          max_tokens=max_output_tokens,
                          backend=_merged_backend(client, backend),
                          extra_headers=extra_headers)
    run_kwargs = _run_kwargs(max_turns)
    recorded: dict = {}
    import openai
    from agents import Runner
    from agents.exceptions import AgentsException, MaxTurnsExceeded

    response_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(time.time())
    given = extra_body or {}

    def envelope(transcript: list, raw_responses) -> dict:
        return {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "model": _reported_model(model_name),
            "status": recorded.get("status") or "completed",
            "output": [item for item in transcript
                       if item.get("type") != "function_call_output"],
            "items": transcript,
            "usage": _responses_usage(raw_responses),
            "instructions": managed,
            "tools": [{"type": "function", "name": tool.name,
                       "description": tool.description,
                       "parameters": tool.params_json_schema,
                       "strict": getattr(tool, "strict_json_schema", True)}
                      for tool in agent.tools],
            # Backend echo when captured; the request sends neither param.
            "tool_choice": recorded.get("tool_choice", "auto"),
            "parallel_tool_calls": recorded.get("parallel_tool_calls", True),
            "temperature": given.get("temperature", temperature),
            "top_p": given.get("top_p", top_p),
            "reasoning": given.get("reasoning", reasoning),
            "max_output_tokens": given.get("max_output_tokens",
                                           max_output_tokens),
            "error": recorded.get("error"),
            "incomplete_details": recorded.get("incomplete_details"),
            "metadata": given.get("metadata"),
        }

    if not stream:
        _record_response_status(agent, recorded)
        try:
            result = _run_sync(_run_closing(agent,
                Runner.run(agent, input=[dict(item) for item in items],
                           **run_kwargs)))
        except (MaxTurnsExceeded, AgentsException,
                openai.OpenAIError) as exc:
            raise _translate_run_error(exc, max_turns,
                                       "responses", client) from exc
        transcript = result.to_input_list()[len(items):]
        return envelope(transcript, result.raw_responses)

    lifecycle = {"response.created", "response.in_progress",
                 "response.completed", "response.failed",
                 "response.incomplete", "response.queued"}

    async def agen():
        streamed = Runner.run_streamed(agent,
                                       input=[dict(item) for item in items],
                                       **run_kwargs)
        sequence = 0
        # output_index addresses an item's position in the logical
        # response.output (the final envelope's list). Backend events
        # carry per-turn indexes that restart at 0 each turn, so they are
        # re-based by the count of items already committed by prior turns.
        output_offset = 0
        completed = False
        opened = False
        try:
            async for event in streamed.stream_events():
                if event.type == "raw_response_event":
                    data = event.data.model_dump(exclude_unset=True)
                    if data.get("type") in lifecycle:
                        if data["type"] == "response.created" and not opened:
                            # N per-turn openings collapse to one, carrying
                            # the id and created_at the terminal event will
                            # report.
                            opened = True
                            if data.get("response"):
                                data["response"]["id"] = response_id
                                data["response"]["created_at"] = created_at
                            sequence += 1
                            data["sequence_number"] = sequence
                            yield data
                            continue
                        if data["type"] in ("response.completed",
                                            "response.incomplete",
                                            "response.failed"):
                            # Per-turn terminal state; the last turn's wins
                            # and feeds the final envelope below.
                            state = data.get("response") or {}
                            for field in ("status", "incomplete_details",
                                          "error"):
                                recorded[field] = state.get(field)
                            for field in ("tool_choice",
                                          "parallel_tool_calls"):
                                if state.get(field) is not None:
                                    recorded[field] = state[field]
                            output_offset += len(state.get("output") or [])
                        continue
                    if isinstance(data.get("output_index"), int):
                        data["output_index"] += output_offset
                    sequence += 1
                    data["sequence_number"] = sequence
                    yield data
            completed = True
        except AgentsException as exc:
            # a run the envelope already reports failed/incomplete is done —
            # except for max_turns, which always gets its guidance
            if (isinstance(exc, MaxTurnsExceeded)
                    or recorded.get("status") not in ("failed", "incomplete")):
                raise _translate_run_error(exc, max_turns,
                                           "responses", client) from exc
            completed = True
        except openai.OpenAIError as exc:
            raise _translate_run_error(exc, max_turns,
                                       "responses", client) from exc
        finally:
            if not completed and hasattr(streamed, "cancel"):
                streamed.cancel()  # abandoned/failed: stop the agent task
            await _aclose_backend(agent)
        transcript = streamed.to_input_list()[len(items):]
        sequence += 1
        status = recorded.get("status") or "completed"
        terminal = {"incomplete": "response.incomplete",
                    "failed": "response.failed"}.get(status,
                                                     "response.completed")
        yield {"type": terminal, "sequence_number": sequence,
               "response": envelope(transcript, streamed.raw_responses)}

    return _stream_sync(agen)

