"""Put PDF page screenshots (`superindex.page_images`) in front of the agent.

The engine's own chat lane takes text messages only, so with images on the run
is assembled here from the same parts `superindex.engine.local_chat.run_chat_stream`
uses (agent, input items, event stream) with three additions:

- prefetch images: the question's user message becomes a content list —
  the text, then each page as an ``input_image`` part with a base64 data URL
  (openai-agents turns it into a Chat Completions ``image_url`` part, which
  LiteLLM passes to OpenAI-compatible and Azure OpenAI endpoints as is);
- the `get_page_image` tool: a function tool whose result is text only —
  OpenAI Chat Completions accepts no images in ``tool`` messages — while the
  image itself goes to the model as a user message placed right after that
  turn's tool results, by the run's ``call_model_input_filter`` (re-inserted
  on every later turn, so the image stays in the conversation);
- a short system-prompt rule: screenshots win over the OCR Markdown.

With images off, `chat` is plain ``client.chat``.
"""
from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from superindex import page_render
from superindex.page_images import Attached, Session

TOOL_NAME = "get_page_image"

DESCRIPTION = (
    "Attach a screenshot of one original PDF page, for when the page text from "
    "get_page_content() looks wrong or incomplete — garbled or misaligned table "
    "columns, doubtful figures, charts or diagrams. The image arrives in the next "
    "message. Limited per question; pages already attached are not sent again."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_name": {"type": "string",
                     "description": "The document; copy its `name` verbatim."},
        "page": {"type": "integer", "description": "Physical page number (page_index)."},
    },
    "required": ["doc_name", "page"],
    "additionalProperties": False,
}


def guidance(limit: int) -> str:
    return (
        "PAGE IMAGES:\n"
        "- Some messages carry screenshots of original PDF pages, each labelled with "
        "its document and page. Where the page text (OCR Markdown) disagrees with a "
        "screenshot — misaligned table columns, misread numbers — the screenshot is "
        "right; say in the answer which page image the figure comes from.\n"
        f"- {TOOL_NAME}(doc_name, page) attaches one more page (at most {limit} images "
        "per question in total). Use it only when a table, chart or figure you need "
        "reads badly in get_page_content()."
    )


def data_url(image: bytes) -> str:
    return f"data:{page_render.MIME};base64,{base64.b64encode(image).decode('ascii')}"


def _label(a: Attached) -> str:
    return f"[PDF 原页截图] {a.doc_name} 第 {a.page} 页"


def _image_parts(attached: list[Attached]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for a in attached:
        parts.append({"type": "input_text", "text": _label(a)})
        parts.append({"type": "input_image", "image_url": data_url(a.image), "detail": "auto"})
    return parts


def user_content(message: str, attached: list[Attached]) -> list[dict[str, Any]]:
    """The question's content list: its text, then the prefetch screenshots."""
    pages = "；".join(f"{a.doc_name} 第 {a.page} 页" for a in attached)
    text = f"{message}\n\n【附图】以下为检索候选页的 PDF 原页截图：{pages}"
    return [{"type": "input_text", "text": text}, *_image_parts(attached)]


def _reply(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


_REASONS = {
    "limit": "The image limit for this question is used up; answer from the page text.",
    "no_pdf": "No PDF is linked to this document; use get_page_content().",
    "out_of_range": "The PDF has no such page.",
    "render_failed": "The page could not be rendered; use get_page_content().",
}


def request_page(client: Any, session: Session, doc_ids: Any, doc_name: str, page: Any,
                 call_id: str | None = None) -> str:
    """One `get_page_image` call: attach the page to `session`; the reply text."""
    try:
        number = int(page)
    except (TypeError, ValueError):
        return _reply({"error": "page must be a page number", "errorCode": "INVALID_INPUT"})
    from superindex.engine.agent_tools import _resolve_document

    scope = None if doc_ids is None else frozenset(
        [doc_ids] if isinstance(doc_ids, str) else [str(d) for d in doc_ids])
    entry, error = _resolve_document(client, str(doc_name or ""), allowed_ids=scope)
    if error is not None:
        return json.dumps(error[0], ensure_ascii=False)
    assert entry is not None
    got = session.request(entry["id"], entry["name"], number, call_id)
    base = {"doc_name": entry["name"], "page": number}
    if isinstance(got, Attached):
        return _reply({"success": True, **base, "attached": True,
                       "note": "The page screenshot follows in the next message."})
    if got == "duplicate":
        return _reply({"success": True, **base, "attached": False,
                       "note": "This page's screenshot is already in the conversation."})
    return _reply({"error": _REASONS[got], "errorCode": got.upper(), **base})


def page_image_tool(client: Any, session: Session, doc_ids: Any) -> Any:
    from agents import FunctionTool

    async def invoke(ctx: Any, raw: str) -> str:
        try:
            args = json.loads(raw or "{}")
        except ValueError:
            args = {}
        call_id = getattr(ctx, "tool_call_id", None)
        try:
            return await asyncio.to_thread(request_page, client, session, doc_ids,
                                           args.get("doc_name"), args.get("page"), call_id)
        except Exception as exc:  # noqa: BLE001 - tool calls never raise into the agent
            return _reply({"error": f"{TOOL_NAME} failed: {exc}", "errorCode": "INTERNAL_ERROR"})

    return FunctionTool(name=TOOL_NAME, description=DESCRIPTION, params_json_schema=SCHEMA,
                        on_invoke_tool=invoke, strict_json_schema=False)


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def input_filter(session: Session) -> Any:
    """``call_model_input_filter``: after the tool results of each
    `get_page_image` call, a user message with that page's screenshot."""
    from agents.run_config import ModelInputData

    def apply(data: Any) -> Any:
        items = list(data.model_data.input)
        inserted: set[int] = set()
        for a in list(session.attached):
            if a.source != "tool" or not a.call_id:
                continue
            at = next((i for i, it in enumerate(items)
                       if _field(it, "type") == "function_call_output"
                       and _field(it, "call_id") == a.call_id), None)
            if at is None:
                continue
            at += 1
            while at < len(items) and (_field(items[at], "type") == "function_call_output"
                                       or id(items[at]) in inserted):
                at += 1
            message = {"role": "user", "content": _image_parts([a])}
            items.insert(at, message)
            inserted.add(id(message))
        return ModelInputData(input=items, instructions=data.model_data.instructions)

    return apply


def chat(client: Any, message: str, *, doc_id: Any, reasoning_effort: str | None,
         session: Session | None) -> Any:
    """``client.chat(message, stream=True)``, with `session`'s images and the
    `get_page_image` tool when it is given. Returns a ChatStream."""
    if session is None:
        return client.chat(message, doc_id=doc_id, stream=True,
                           reasoning_effort=reasoning_effort)
    from superindex.engine import local_chat
    from superindex.engine.chat_stream import ChatStream

    local_chat._require_openai_agents("chat")
    agent, items, _ = local_chat._chat_agent(client, [{"role": "user", "content": message}],
                                             doc_id, None, reasoning_effort=reasoning_effort)
    prefetched = [a for a in session.attached if a.source != "tool"]
    if prefetched:
        items[-1] = {"role": "user", "content": user_content(message, prefetched)}
    agent.instructions = f"{agent.instructions}\n\n{guidance(session.limit)}"
    agent.tools.append(page_image_tool(client, session, client._local_doc_scope(doc_id)))
    run_kwargs = local_chat._run_kwargs(None)
    run_kwargs["run_config"].call_model_input_filter = input_filter(session)

    def events() -> Any:
        return local_chat._stream_sync(
            lambda: local_chat._chat_events_agen(client, agent, items, run_kwargs))

    return ChatStream(text=lambda: local_chat._weave(events(), None), events=events)
