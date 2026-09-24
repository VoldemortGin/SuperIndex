"""SuperIndex SDK client: the local document store and own-model chat."""
from __future__ import annotations

import os
import re
import threading
import warnings
from typing import (TYPE_CHECKING, Any, Callable, Iterator, Literal, Mapping,
                    Optional, Union, cast, overload)

from .chat_stream import ChatStream
from .errors import SuperIndexAPIError

if TYPE_CHECKING:
    from .agent_tools import AgentTool
    from .local_chat import ChatExtras


_litellm_preload_started = False


def _preload_litellm() -> None:
    """Start litellm's multi-second import in the background, once per
    process — a per-client thread would churn under per-request clients."""
    global _litellm_preload_started
    if _litellm_preload_started:
        return
    _litellm_preload_started = True

    def _import() -> None:
        try:
            import litellm  # noqa: F401
        except Exception:
            pass

    threading.Thread(target=_import, daemon=True).start()


def _parse_pages(pages: str) -> list[int]:
    from .agent_tools import _PageSpecError, _expand_pages
    if isinstance(pages, str):
        # 0.2.10 tolerated whitespace on this surface; the tool layer stays
        # on the strict contract pattern.
        pages = re.sub(r"\s*([,-])\s*", r"\1", pages.strip())
    try:
        return _expand_pages(pages)
    except _PageSpecError as exc:
        raise SuperIndexAPIError(str(exc)) from exc


# The two citation tag formats SuperIndex chat writes and renders.
_OLD_CITATION_RE = re.compile(
    r"<doc=([^;<>]+);page=(\d+)(?:;block(?:_id)?=([^;<>]+))?>")
_CITE_TAG_RE = re.compile(r"<cite\s([^<>]*)>(?:(?P<inner>[^<>]*)</cite>)?")
_CITE_ATTR_RE = re.compile(r"""\b(\w+)=(["'])(.*?)\2""", re.S)


def _citation_key(m: re.Match) -> Optional[tuple[str, int, Optional[str]]]:
    """(document, page, block_id) of one matched tag, or None when it names
    no document or no positive page."""
    if m.re is _OLD_CITATION_RE:
        doc, page_str, block_id = m.group(1), m.group(2), m.group(3)
    else:
        attrs = {name: value for name, _, value in
                 _CITE_ATTR_RE.findall(m.group(1))}
        doc, page_str, block_id = (attrs.get("doc", ""), attrs.get("page", ""),
                                   attrs.get("block"))
    doc = doc.strip()
    block_id = (block_id or "").strip() or None
    try:
        page = int(page_str.split("-")[0])
    except ValueError:
        return None
    return (doc, page, block_id) if doc and page > 0 else None


def _parse_citations(text: str) -> list[dict[str, Any]]:
    """``<doc=…;page=…;block=…>`` tags (the upstream hosted chat's format), then
    ``<cite doc= page= block=/>`` tags; deduplicated, ``block_id`` only
    when the tag carries one."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int, Optional[str]]] = set()
    for m in [*_OLD_CITATION_RE.finditer(text), *_CITE_TAG_RE.finditer(text)]:
        key = _citation_key(m)
        if key and key not in seen:
            seen.add(key)
            entry: dict[str, Any] = {"document": key[0], "page": key[1]}
            if key[2]:
                entry["block_id"] = key[2]
            found.append(entry)
    return found


def _agents_sdk_model_name(model: str) -> str:
    """Preserve supported Agents SDK prefixes and route other provider paths via LiteLLM."""
    passthrough_prefixes = ("litellm/", "openai/")
    if not model or "/" not in model:
        return model
    if model.startswith(passthrough_prefixes):
        return model
    return f"litellm/{model}"


_LOCAL_INDEX_KEYS = ("model", "summary_model", "backend", "storage_path")

# Mode words that would otherwise parse as model names — a silent wrong
# model. The hosted service is not supported; they error instead.
_UNSUPPORTED_MODE_WORDS = {"cloud", "pageindex-cloud", "hosted", "managed"}


# One argument vocabulary regardless of spelling: these values are shape-
# checked in the constructor, so a wrong type or an empty value refuses
# there as a SuperIndexAPIError — never later, never silently.
_ARG_TYPES: "dict[str, tuple[type, ...]]" = {
    "model": (str,), "index_model": (str,), "summary_model": (str,),
    "chat_model": (str,), "retrieve_model": (str,),
    "storage_path": (str, os.PathLike), "index_backend": (dict,),
    "chat_backend": (dict,)}


def _declared_mode(value, side: str):
    if isinstance(value, str):
        value = value.strip().lower()
    if value not in (None, "local"):
        raise SuperIndexAPIError(
            f'{side} "mode" must be "local", not {value!r} — SuperIndex '
            "runs locally only.")
    return value


def _unsupported_mode_word(side: str, value: str) -> SuperIndexAPIError:
    return SuperIndexAPIError(
        f'{side}="{value}" is not supported — SuperIndex runs locally only. '
        f'Pass a model name, or "local".')


def _resolve_index_slot(index) -> dict[str, Any]:
    """The ``index=`` slot as local overrides. A dict declares the local
    store by its keys; an optional "mode" must say "local"."""
    if isinstance(index, str):
        # Normalized compare: a case/whitespace variant of a mode word
        # must never fall through and silently become a model name.
        word = index.strip().lower()
        if word == "local":
            return {}
        if word in _UNSUPPORTED_MODE_WORDS:
            raise _unsupported_mode_word("index", index)
        if index.strip():
            return {"index_model": index}
        raise SuperIndexAPIError(
            "index is an empty string — pass a local index model name, "
            'or "local".')
    if isinstance(index, Mapping):
        # None-valued keys mean "absent", exactly like the flat arguments.
        conf = {name: value for name, value in index.items()
                if value is not None}
        declared = _declared_mode(conf.pop("mode", None), "index")
        if not conf:
            if declared == "local":
                return {}
            raise SuperIndexAPIError(
                "index is an empty dict — it takes "
                f"{', '.join(_LOCAL_INDEX_KEYS)}.")
        unknown = set(conf) - set(_LOCAL_INDEX_KEYS)
        if unknown:
            raise SuperIndexAPIError(
                f"Unknown index keys ({', '.join(sorted(unknown))}) — "
                f"index takes {', '.join(_LOCAL_INDEX_KEYS)}.")
        mapped = {"index_model": conf.get("model"),
                  "summary_model": conf.get("summary_model"),
                  "index_backend": conf.get("backend"),
                  "storage_path": conf.get("storage_path")}
        return {name: value for name, value in mapped.items()
                if value is not None}
    raise SuperIndexAPIError("index must be a string or a dict.")


def _resolve_chat_slot(chat) -> dict[str, Any]:
    """The ``chat=`` slot as own-model overrides."""
    if isinstance(chat, str):
        word = chat.strip().lower()
        if word == "local":
            return {}
        if word in _UNSUPPORTED_MODE_WORDS:
            raise _unsupported_mode_word("chat", chat)
        if chat.strip():
            return {"chat_model": chat}
        raise SuperIndexAPIError(
            'chat is an empty string — pass a model name, or "local".')
    if isinstance(chat, Mapping):
        # None-valued keys mean "absent", exactly like the flat arguments.
        conf = {name: value for name, value in chat.items()
                if value is not None}
        declared = _declared_mode(conf.pop("mode", None), "chat")
        unknown = set(conf) - {"model", "backend"}
        if (not conf and declared is None) or unknown:
            raise SuperIndexAPIError(
                ("chat is an empty dict" if not conf else
                 f"Unknown chat keys ({', '.join(sorted(unknown))})")
                + ' — chat takes "model" and "backend".')
        mapped = {"chat_model": conf.get("model"),
                  "chat_backend": conf.get("backend")}
        return {name: value for name, value in mapped.items()
                if value is not None}
    raise SuperIndexAPIError("chat must be a string or a dict.")


class SuperIndexClient:
    """
    Python SDK client for SuperIndex.

    Documents are indexed on your machine (your own LLM provider key,
    e.g. ``OPENAI_API_KEY``) and stored under ``storage_path``; the
    document-QA agent runs in your process against your own chat model
    and credentials.

    Usage:
        client = SuperIndexClient()
        client = SuperIndexClient(chat_model="openai/gpt-5.2")

    ``index=`` / ``chat=`` are the grouped spelling of the same flat
    arguments — a string as shorthand, a dict for the full config; each
    side picks one spelling per client.

    Args:
        index (str | dict, optional): The index side, grouped —
            ``"local"``, a local index model name, or a dict
            ``{"model", "summary_model", "backend", "storage_path"}``. An
            optional ``"mode"`` key must be ``"local"``. Not combinable
            with this side's flat arguments.
        chat (str | dict, optional): The chat side, grouped — a model
            name, ``"local"`` (the default model), or ``{"model",
            "backend"}``. An optional ``"mode"`` key must be ``"local"``.
            Not combinable with this side's flat arguments.
        mode (str, optional): ``"local"`` — accepted for compatibility;
            the client is always local.
        index_model (str, optional): LLM used to index documents
            (structure and summaries). Defaults to the SDK default (fast
            and cheap).
        chat_model (str, optional): Your own model for the chat surfaces
            (``chat``, ``chat_completions``), exposed as
            ``client.chat_model``. Chat names route through LiteLLM and
            mean what LiteLLM says they mean; bare names are
            OpenAI-compatible shorthand, and ``openai/Qwen/...`` is the
            form for an OpenAI-compatible server that itself serves
            slashed model ids (vLLM, TGI). Defaults to the SDK default
            (strong).
        model (str, optional): One model for both roles: sets the default
            for ``index_model`` and ``chat_model`` at once. The
            role-specific arguments win over it. (Also the 0.2.8-era name
            for the indexing model — old configs keep working unchanged.)
        summary_model (str, optional): Legacy: overrides the model used
            for node summaries and document descriptions; ``index_model``
            covers this.
        retrieve_model (str, optional): Legacy name for ``chat_model``.
        storage_path (str or os.PathLike, optional): Directory where
            indexed documents are stored. Defaults to ``./.pageindex``.
        index_backend (dict, optional): Connection overrides for the
            indexing lane's LLM calls. Keys are LiteLLM's own connection
            params — ``api_key``, ``api_base``, ``api_version``,
            ``aws_*``, … — passed through verbatim.
        chat_backend (dict, optional): Default connection overrides for
            the chat surfaces. A call's own ``backend`` keys win over it.
            The dict reaches whichever door runs, in that door's
            vocabulary (see each method) — ``api_key`` / ``base_url``
            mean the same thing on every door.
        instructions (str, optional): Standing guidance for the answering
            agent — persona, language, format — appended after the
            managed system prompt on every chat surface, and in
            ``agent_instructions()`` and ``openai_agent_config()``.
            ``chat(instructions=...)`` adds to it per call. Indexing has
            no prompt to extend.
        tools (list[AgentTool], optional): Extra tools for the chat
            agent, served after the built-in tools.
        page_text_extractor (callable, optional): Replaces the PDF page
            text extraction used while indexing.

    SuperIndexLocalClient is the same client under its older name.

    Local differences from the upstream SDK (documented per method):
    indexing is synchronous, only PDFs are supported, and there are no
    folders.
    """

    def __init__(
        self,
        *,
        index: Optional[Union[Mapping[str, Any], str]] = None,
        chat: Optional[Union[Mapping[str, Any], str]] = None,
        mode: Optional[str] = None,
        index_model: Optional[str] = None,
        chat_model: Optional[str] = None,
        model: Optional[str] = None,
        summary_model: Optional[str] = None,
        retrieve_model: Optional[str] = None,
        storage_path: Optional[Union[str, os.PathLike[str]]] = None,
        index_backend: Optional[dict[str, Any]] = None,
        chat_backend: Optional[dict[str, Any]] = None,
        instructions: Optional[str] = None,
        tools: Optional[list[AgentTool]] = None,
        page_text_extractor: Optional[Callable[[str], list[str]]] = None,
    ):
        if instructions is not None and not isinstance(instructions, str):
            raise SuperIndexAPIError(
                f"instructions must be a str, got {type(instructions).__name__}. "
                "Pass the guidance as text.")
        self.instructions = (instructions or "").strip() or None
        self.tools: tuple[AgentTool, ...] = tuple(tools or ())
        # Each side picks one spelling — its slot, or the flat arguments.
        # ``model`` sets every role, so it claims both sides.
        index_flat: dict[str, Any] = {
            name: value for name, value in
            (("index_model", index_model),
             ("summary_model", summary_model),
             ("index_backend", index_backend),
             ("storage_path", storage_path), ("model", model))
            if value is not None}
        chat_flat: dict[str, Any] = {
            name: value for name, value in
            (("chat_model", chat_model),
             ("retrieve_model", retrieve_model),
             ("chat_backend", chat_backend), ("model", model))
            if value is not None}
        if model is not None and (index is not None or chat is not None):
            raise SuperIndexAPIError(
                "model= sets both roles at once, so no slot can absorb "
                'it — name the model inside the slot ({"model": ...}) '
                "and use index_model= / chat_model= for a side written "
                "flat.")
        if index is not None and index_flat:
            raise SuperIndexAPIError(
                "index= and the flat index-side arguments "
                f"({', '.join(sorted(index_flat))}) are two spellings of "
                "the same thing — use one or the other.")
        if chat is not None and chat_flat:
            raise SuperIndexAPIError(
                "chat= and the flat chat-side arguments "
                f"({', '.join(sorted(chat_flat))}) are two spellings of "
                "the same thing — use one or the other.")
        _declared_mode(mode, "client")
        index_conf = (_resolve_index_slot(index) if index is not None
                      else index_flat)
        chat_conf = _resolve_chat_slot(chat) if chat is not None else chat_flat
        # Every spelling lands here: strings are stripped, wrong types and
        # empty values refuse loudly.
        for side, slot, conf in (("index", index, index_conf),
                                 ("chat", chat, chat_conf)):
            for name, value in conf.items():
                # Slot keys are the flat names with the side prefix off.
                shown = (f'{side}["{name.removeprefix(side + "_")}"]'
                         if slot is not None else name)
                if not isinstance(value, _ARG_TYPES[name]):
                    raise SuperIndexAPIError(
                        f"{shown} must be a {_ARG_TYPES[name][0].__name__}, "
                        f"got {type(value).__name__}.")
                if isinstance(value, str):
                    value = conf[name] = value.strip()
                if not value:
                    raise SuperIndexAPIError(
                        f"{shown} is empty — it configures nothing. Pass a "
                        "real value, or drop the argument.")

        from .utils import ConfigLoader
        overrides = {name: value for name, value in
                     {**index_conf, **chat_conf}.items()
                     if name in ("model", "index_model", "summary_model",
                                 "chat_model", "retrieve_model")
                     and value}
        opt = ConfigLoader().load(overrides or None)
        self.model = opt.model
        self.index_model = opt.index_model
        self.summary_model = opt.summary_model
        self.chat_model = opt.chat_model
        self.chat_backend = chat_conf.get("chat_backend")
        self.storage_path = index_conf.get("storage_path") or ".pageindex"
        from .local_api import LocalAPI
        self._api = LocalAPI(
            storage_path=self.storage_path,
            model=self.model,
            summary_model=self.summary_model,
            index_backend=index_conf.get("index_backend"),
            page_text_extractor=page_text_extractor,
        )
        # LiteLLM's multi-second import would otherwise land on the
        # first chat call; failures resurface there with real context.
        _preload_litellm()

    @property
    def _local_chat(self) -> bool:
        # Derived, never stored: own-model chat is exactly "a chat model
        # is configured". Blank configures nothing — the constructor
        # refuses it, and assignment must agree.
        model = getattr(self, "chat_model", None)
        if isinstance(model, str):
            return bool(model.strip())
        return model is not None

    def _require_own_chat(self) -> None:
        # The one refusal for every chat door: shared, so the doors cannot
        # drift from chat().
        if not self._local_chat:
            raise SuperIndexAPIError(
                "chat_model is empty — it configures nothing. Set "
                "chat_model=... to run the agent with your own model.")

    if not TYPE_CHECKING:
        # The protocol doors live behind chat(protocol=...); their old
        # names are the vendor SDKs' own, so an agent-written
        # client.responses(...) fails here with the way in. Runtime-only:
        # a __getattr__ the type checker can see would silence every
        # attribute typo on the client.
        def __getattr__(self, name):
            if name == "responses":
                raise AttributeError(
                    f"{name}() moved: call chat(protocol={name!r}, ...) "
                    "— the same protocol, engine, and envelope. Pass the "
                    "rest by keyword; its sampling and thinking fields "
                    "ride extra_body under their wire names.")
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}")

    @property
    def retrieve_model(self):
        """Legacy name for ``chat_model``."""
        return self.chat_model

    @retrieve_model.setter
    def retrieve_model(self, value):
        # 0.2.9 allowed assignment; keep the write path working too.
        self.chat_model = value

    # ---------- DOCUMENT SUBMISSION ----------

    def submit_document(
        self,
        file_path: str,
        mode: Optional[str] = None,
        beta_headers: Optional[list[str]] = None,
        folder_id: Optional[str] = None,
        metadata: Optional[dict] = None,
        wait: bool = False,
    ) -> dict[str, Any]:
        """
        Submit a PDF document for processing. Returns {'doc_id': ..., 'name': ...}.

        Indexes the document in this call and stores it under
        ``storage_path``. Defaults to Flash indexing: layout-based extraction,
        refined for retrieval (a deterministic merge, then an LLM expansion
        pass); node summaries, the expansion pass, and the document
        description use ``summary_model``. Pass ``mode="standard"`` for a
        full LLM-built tree (slower). ``beta_headers`` and ``folder_id`` are
        not supported (they raise).

        Args:
            file_path (str): Path to the PDF file.
            mode (str, optional): Processing mode. Defaults to "flash";
                pass "standard" for a full LLM-built tree.
            beta_headers (list[str], optional): Not supported; raises.
            folder_id (str, optional): Not supported; raises.
            metadata (dict, optional): Your own JSON-serializable tags for the
                document; returned in get_document/get_tree/get_ocr responses
                and list_documents entries.
            wait (bool): Indexing is synchronous already, so this changes
                nothing; kept for compatibility.

        Returns:
            dict: {'doc_id': ..., 'name': ...}. 'name' is the stored document
                name: a taken name gains a numeric suffix (name_1..name_99)
                and a UserWarning is emitted.
        """
        result = self._api.submit_document(
            file_path=file_path, mode=mode,
            beta_headers=beta_headers, folder_id=folder_id, metadata=metadata,
        )
        stored = result.get("name")
        if stored and stored != os.path.basename(file_path):
            warnings.warn(
                f'Document "{os.path.basename(file_path)}" was stored as '
                f'"{stored}".',
                stacklevel=2,
            )
        return result

    # ---------- OCR FUNCTIONALITY ----------

    def get_ocr(self, doc_id: str, format: str = "page") -> dict[str, Any]:
        """
        Get OCR status and results.

        Args:
            doc_id (str): Document ID.
            format (str): 'page' for page-based results, 'node' for node-based
                results, or 'raw' for concatenated markdown.

        Returns:
            dict: {'doc_id', 'status', 'retrieval_ready', 'result', ...}.
            With 'page', result entries are {'page_index', 'markdown', ...}.

        The "OCR" result is the text extracted from the PDF while
        indexing (no OCR model runs locally, so scanned/image-only PDFs have
        no local text).
        """
        return self._api.get_ocr(doc_id=doc_id, format=format)

    def get_page_content(self, doc_id: str, pages: str) -> list[dict[str, Any]]:
        """
        Get text content of specific pages.

        Args:
            doc_id (str): Document ID.
            pages (str): Page specifier — '5-7', '3,8', or '12'.

        Returns:
            list: Matching entries from get_ocr (format='page').
        """
        wanted = set(_parse_pages(pages))
        result = self.get_ocr(doc_id, format="page")
        all_pages = result["result"]
        if all_pages is None:
            raise SuperIndexAPIError(
                f"Document '{doc_id}' is not ready "
                f"(status: {result.get('status', 'unknown')})"
            )
        return [p for p in all_pages if p["page_index"] in wanted]

    # ---------- TREE GENERATION ----------

    def get_tree(self, doc_id: str, node_summary: bool = False,
                 include_text: bool = True) -> dict[str, Any]:
        """
        Get tree generation status and results.

        Args:
            doc_id (str): Document ID.
            node_summary (bool): Include node summaries in the tree.
            include_text (bool): Include node text (default True).
                False is useful for structure-only views (saves tokens).

        Returns:
            dict: {'doc_id', 'status', 'retrieval_ready', 'result', ...} where
            result nodes are {'title', 'node_id', 'page_index', ('summary' /
            'prefix_summary',) ('text',) 'nodes'}.
        """
        tree = self._api.get_tree(doc_id=doc_id, node_summary=node_summary,
                                  include_text=include_text)
        if not include_text and tree.get("result"):
            from .utils import remove_fields
            tree["result"] = remove_fields(tree["result"], fields=["text"])
        return tree

    def get_document_structure(self, doc_id: str) -> list[dict[str, Any]]:
        """
        Get the document's tree structure without text — summaries included.

        Returns:
            list: Tree nodes with titles, page ranges, and summaries.
        """
        return self.get_tree(doc_id, node_summary=True, include_text=False)["result"]

    def is_retrieval_ready(self, doc_id: str) -> bool:
        """
        Check if a document is ready for retrieval. API errors (including a
        missing document) are reported as False; transport errors (connection
        failures, timeouts) propagate.
        """
        try:
            result = self.get_tree(doc_id)
            return result.get("retrieval_ready", False)
        except SuperIndexAPIError:
            return False

    # ---------- CHAT ----------

    # stream and protocol pick the return type: the docstring's `.events`
    # usage and the protocol envelopes must type-check for py.typed
    # consumers
    @overload
    def chat(
        self,
        messages: Union[str, list[dict[str, Any]]],
        *,
        doc_id: Optional[Union[str, list[str]]] = None,
        stream: Literal[False] = False,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        show_process: Union[bool, Mapping[str, Any], None] = None,
        folder_id: Optional[str] = None,
        protocol: None = None,
        instructions: Optional[str] = None,
        citations: bool = False,
        max_turns: Optional[int] = None,
        backend: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extras: Optional[ChatExtras] = None,
    ) -> str: ...

    @overload
    def chat(
        self,
        messages: Union[str, list[dict[str, Any]]],
        *,
        doc_id: Optional[Union[str, list[str]]] = None,
        stream: Literal[True],
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        show_process: Union[bool, Mapping[str, Any], None] = None,
        folder_id: Optional[str] = None,
        protocol: None = None,
        instructions: Optional[str] = None,
        citations: bool = False,
        max_turns: Optional[int] = None,
        backend: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extras: Optional[ChatExtras] = None,
    ) -> ChatStream: ...

    @overload
    def chat(
        self,
        messages: Union[str, list[dict[str, Any]]],
        *,
        doc_id: Optional[Union[str, list[str]]] = None,
        stream: Literal[False] = False,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        show_process: Union[bool, Mapping[str, Any], None] = None,
        folder_id: Optional[str] = None,
        protocol: Literal["chat_completions", "responses"],
        instructions: Optional[str] = None,
        citations: bool = False,
        max_turns: Optional[int] = None,
        backend: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extras: Optional[ChatExtras] = None,
    ) -> dict[str, Any]: ...

    @overload
    def chat(
        self,
        messages: Union[str, list[dict[str, Any]]],
        *,
        doc_id: Optional[Union[str, list[str]]] = None,
        stream: Literal[True],
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        show_process: Union[bool, Mapping[str, Any], None] = None,
        folder_id: Optional[str] = None,
        protocol: Literal["chat_completions", "responses"],
        instructions: Optional[str] = None,
        citations: bool = False,
        max_turns: Optional[int] = None,
        backend: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extras: Optional[ChatExtras] = None,
    ) -> Iterator[dict[str, Any]]: ...

    @overload
    def chat(
        self,
        messages: Union[str, list[dict[str, Any]]],
        *,
        doc_id: Optional[Union[str, list[str]]] = None,
        stream: bool = False,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        show_process: Union[bool, Mapping[str, Any], None] = None,
        folder_id: Optional[str] = None,
        protocol: None = None,
        instructions: Optional[str] = None,
        citations: bool = False,
        max_turns: Optional[int] = None,
        backend: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extras: Optional[ChatExtras] = None,
    ) -> Union[str, ChatStream]: ...

    @overload
    def chat(
        self,
        messages: Union[str, list[dict[str, Any]]],
        *,
        doc_id: Optional[Union[str, list[str]]] = None,
        stream: bool = False,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        show_process: Union[bool, Mapping[str, Any], None] = None,
        folder_id: Optional[str] = None,
        protocol: Optional[Literal["chat_completions", "responses"]] = None,
        instructions: Optional[str] = None,
        citations: bool = False,
        max_turns: Optional[int] = None,
        backend: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extras: Optional[ChatExtras] = None,
    ) -> Union[str, ChatStream, dict[str, Any], Iterator[dict[str, Any]]]: ...

    def chat(
        self,
        messages: Union[str, list[dict[str, Any]]],
        *,
        doc_id: Optional[Union[str, list[str]]] = None,
        stream: bool = False,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        show_process: Union[bool, Mapping[str, Any], None] = None,
        folder_id: Optional[str] = None,
        protocol: Optional[Literal["chat_completions", "responses"]] = None,
        instructions: Optional[str] = None,
        citations: bool = False,
        max_turns: Optional[int] = None,
        backend: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extras: Optional[ChatExtras] = None,
    ) -> Union[str, ChatStream, dict[str, Any], Iterator[dict[str, Any]]]:
        """
        Ask a question about your documents.

        The answer lane (no ``protocol``): thin sugar over the same engine
        as ``chat_completions()`` — same wire, minus the envelope. Returns
        the answer string (a ``ChatStream`` when streaming). Multi-turn: keep your own role/content list of the
        visible conversation (append each answer as an assistant message;
        join a stream into one only with ``show_process=False``) and pass
        it back.

        The protocol lanes: ``protocol="chat_completions"`` is the answer
        lane's own engine with its envelope kept — the Chat Completions
        response (``choices``/``usage``), or its chunk dicts when
        streaming. ``protocol="responses"``: own-model chat driven natively
        over the OpenAI Responses API. Input and output are that protocol's own
        shapes — the history may carry its transcript (Responses items),
        and the return is its response envelope, streaming its native
        events. A round-tripped transcript continues the
        agent's memory and the provider's cached prefix: follow-ups re-read
        the run instead of redoing the tool work. The protocol is declared,
        never inferred from the model name. Keep ``doc_id`` and
        ``protocol`` constant across a conversation's calls.

        Args:
            messages: A question string, or the conversation history —
                role/content messages on every lane. ``system`` rows join
                the managed prompt on the answer lane and
                ``protocol="chat_completions"``, wherever they sit; the
                other protocol lanes pass rows to the wire as they
                are (use ``instructions`` for persona there). Responses
                also accepts its native transcript items; own-model Chat
                Completions takes text history only.
            doc_id: Document ID or list of IDs to scope the conversation.
                Keep it identical across a conversation's calls. Also
                enforced at the tool layer, not just prompted.
            folder_id: Local libraries have no folders: only ``"root"``
                (the whole library) or ``""`` is accepted; any other
                value raises. Keep it identical across a conversation's
                calls.
            stream: Answer lane: return a ``ChatStream`` — iterate it for
                the answer as text chunks as they are produced
                (``show_process`` is on by default, so the run's process
                arrives woven in; ``show_process=False`` gives the bare
                answer), or read its ``.events`` property instead for the
                run as typed event dicts — thinking/answer deltas, each
                tool call and its full result (never clipped). One run
                serves one view. Protocol lanes: the protocol's own event
                stream.
            model: Backend model name (defaults to ``chat_model``).
            reasoning_effort: How hard the model
                thinks (``"low"`` / ``"medium"`` / ``"high"``; what a
                backend accepts is its own). Each lane sends its native
                spelling: LiteLLM's ``reasoning_effort``, Responses
                ``reasoning.effort``.
                Unset sends nothing — the model's default applies.
            show_process: Answer lane, streamed chat — weave the run into
                the text stream for display: thinking flows as
                "[thinking] " sections, each tool call as a "[tool_call]
                name arguments" line with its "[tool_result]" line, and
                the answer unlabeled. **On by default**, weaving the
                in-process agent's full run. Pass ``False``
                for the bare answer stream — do that before appending a
                streamed answer to the conversation history.
                ``True`` shows everything; a dict (typed as
                ``superindex.engine.ChatProcessOptions``) selects the parts —
                ``thinking`` / ``tool_call`` / ``tool_result``, bools
                defaulting on — and sets ``max_chars``, the per-line
                summary cap in characters (default 200). Omitted keys
                keep their defaults, so ``{"thinking": False}`` hides
                only thinking and ``{}`` equals ``True``.
                Thinking appears when the backend streams it
                (e.g. Claude models with ``reasoning_effort``; OpenAI
                models expose none on the chat protocol). The labels are
                not a parse format, and a process stream must not be
                appended back as conversation history — for the
                machine-readable process use ``.events``, or the
                Responses lane's transcript. A protocol lane
                returns its own shape, so ``show_process`` is an error
                there.
            protocol: ``None`` for the answer lane, or
                ``"chat_completions"`` / ``"responses"``
                — the wire protocol, engine, and input/output shapes of
                this call.
            instructions: Persona or extra guidance for this call,
                appended after the managed system prompt (which stays: it
                carries the tool guidance) and the client's own
                ``instructions``. A string on every lane. On the answer
                lane and ``protocol="chat_completions"`` it precedes any ``system``
                rows in the history.
            citations: Cite every claim the way SuperIndex chat does —
                ``<cite doc="…" page="…"/>`` tags. The guidance
                (``citation_prompt()``, page-level) joins the system
                prompt after the managed prompt and before
                ``instructions``; for another format pass
                ``citation_prompt()`` through ``instructions=`` instead.
            max_turns: Cap on agent turns per call
                (default 10). The lanes raise at the cap.
            backend: Connection overrides for this
                call's backend, merged over the client's ``chat_backend``
                (per-call keys win): LiteLLM's connection params on the
                answer lane and ``protocol="chat_completions"``; the
                openai SDK's client params on Responses. Passed through
                verbatim.
            extra_headers: Extra HTTP headers
                merged into each backend request; caller headers win.
                LiteLLM's anthropic adapter owns ``anthropic-beta`` on the
                answer lane and ``protocol="chat_completions"``.
            extra_body: The wire's own request fields beyond this
                method's parameters, in the lane's wire names (Responses
                ``max_output_tokens``), merged last so they win.
                Responses and OpenAI-compatible chat backends take these
                verbatim in the request body. Other chat backends take
                LiteLLM's own params, mapped or refused per provider
                (``response_format`` is unsupported there). The managed
                prompt, conversation and tools are not fields here (``system`` /
                ``instructions`` / ``input`` / ``messages`` / ``tools``
                are refused); extend the prompt with ``instructions=`` or a
                leading system row in ``messages``. ``stream`` / ``doc_id``
                are refused too: each has its own argument. Credentials
                belong in ``backend``, never here.
            extras (ChatExtras, optional): Additions for this run of the
                streamed answer lane: a multimodal
                last user message, appended instructions, extra Agents SDK
                tools and a ``call_model_input_filter``. See
                ``local_chat.ChatExtras``.

        Returns:
            - answer lane, stream=False: the answer string
            - answer lane, stream=True: a ``ChatStream`` — iterating it
              yields text chunks (with show_process, on by default, the
              run's process woven in as labeled sections); ``.events``
              yields typed event dicts:
              ``{"type": "thinking"|"answer", "delta": ...}``,
              ``{"type": "tool_call", "call_id", "name", "arguments"}``,
              ``{"type": "tool_result", "call_id", "name", "output"}``
            - protocol lane, stream=False: the protocol's response
              envelope — Chat Completions: ``choices`` and ``usage``;
              Responses: ``output`` plus an ``items``
              transcript and cross-turn ``usage``
            - protocol lane, stream=True: an iterator of the protocol's
              own stream events
        """
        if protocol not in (None, "chat_completions", "responses"):
            raise SuperIndexAPIError(
                "protocol selects the wire: \"chat_completions\" (OpenAI Chat "
                "Completions) or \"responses\" (OpenAI Responses), or leave "
                f"it unset for the answer lane — got {protocol!r}.")
        if (protocol is not None and show_process is not False
                and show_process is not None):
            raise SuperIndexAPIError(
                "show_process weaves the answer lane's run; with "
                f"protocol={protocol!r} the return is the protocol's own "
                "envelope and events — drop show_process, or drop "
                "protocol for the woven text stream.")
        if show_process is not False and show_process is not None:
            from .local_chat import _process_options
            _process_options(show_process)  # a bad value chokes first
            if not stream:
                raise SuperIndexAPIError(
                    "show_process shows the run as it happens and requires "
                    "stream=True; only show_process=False (or None) means "
                    f"off — got {show_process!r}.")
        if instructions is not None and not isinstance(instructions, str):
            raise SuperIndexAPIError(
                "instructions must be a string, got "
                f"{type(instructions).__name__}.")
        from .local_chat import _refuse_skeleton
        _refuse_skeleton(extra_body)
        if citations and citations is not True:
            raise SuperIndexAPIError(
                "citations must be True or False — for another format pass "
                "citation_prompt(format=...) as instructions= (own-model "
                "chat).")
        if citations and self._local_chat:
            text = self.citation_prompt()
            instructions = (f"{text}\n\n{instructions}"
                            if instructions else text)
        if extras is not None and not (stream and protocol is None
                                       and self._local_chat):
            raise SuperIndexAPIError(
                "extras extend the streamed answer lane of your own chat "
                "model — pass stream=True, no protocol, on a client with "
                "chat_model=... set.")
        self._require_own_chat()
        if protocol == "responses":
            body = extra_body
            if reasoning_effort:
                # OpenAI's own effort field, beside the caller's other
                # reasoning keys — theirs still win.
                given = extra_body or {}
                body = {**given, "reasoning": {
                    "effort": reasoning_effort,
                    **given.get("reasoning", {})}}
            return self._responses(
                messages, model=model, stream=stream, doc_id=doc_id,
                folder_id=folder_id, instructions=instructions,
                max_turns=max_turns, extra_body=body,
                extra_headers=extra_headers, backend=backend)
        if instructions:
            if isinstance(messages, str):
                if not messages.strip():
                    raise SuperIndexAPIError(
                        "messages must be a non-empty string or a list of "
                        "message dicts.")
                messages = [{"role": "user", "content": messages}]
            # The first system text: managed prompt, then instructions,
            # then the history's own system rows.
            messages = [{"role": "system", "content": instructions},
                        *messages]
        if protocol == "chat_completions":
            return self.chat_completions(
                messages, stream=stream, stream_metadata=True, doc_id=doc_id,
                folder_id=folder_id, model=model, max_turns=max_turns,
                reasoning_effort=reasoning_effort, extra_body=extra_body,
                extra_headers=extra_headers, backend=backend)
        if stream:
            # the default means "on"
            resolved = True if show_process is None else show_process
            from .local_chat import run_chat_stream
            return run_chat_stream(self, messages, doc_id=doc_id,
                                   folder_id=folder_id, model=model,
                                   reasoning_effort=reasoning_effort,
                                   show_process=resolved,
                                   max_turns=max_turns, backend=backend,
                                   extra_headers=extra_headers,
                                   extra_body=extra_body, extras=extras)
        result = self.chat_completions(messages, doc_id=doc_id, model=model,
                                       folder_id=folder_id,
                                       reasoning_effort=reasoning_effort,
                                       max_turns=max_turns, backend=backend,
                                       extra_headers=extra_headers,
                                       extra_body=extra_body)
        envelope = cast(dict[str, Any], result)
        try:
            return envelope["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise SuperIndexAPIError(
                "The chat response carries no answer: "
                f"{str(envelope)[:200]}") from exc

    def chat_completions(
        self,
        messages: Union[str, list[dict[str, str]]],
        stream: bool = False,
        doc_id: Optional[Union[str, list[str]]] = None,
        temperature: Optional[float] = None,
        stream_metadata: bool = False,
        enable_citations: bool = False,
        model: Optional[str] = None,
        max_turns: Optional[int] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        backend: Optional[dict[str, Any]] = None,
        *,
        folder_id: Optional[str] = None,
    ) -> Union[dict[str, Any], Iterator[str], Iterator[dict[str, Any]]]:
        """
        Kept for existing code — new code calls ``chat()``. Everything
        here is ``chat(protocol="chat_completions")``: the same engine
        and envelope, with this method's sampling fields riding
        ``extra_body`` under their wire names. The one exception is the
        text-only stream (``stream=True`` without ``stream_metadata``):
        that is ``chat(stream=True, show_process=False)``.

        A managed document-QA agent runs in your process over the local
        store's tools against your own LLM backend, routed through
        LiteLLM — model names mean what LiteLLM says they mean.
        Bare names are OpenAI-compatible shorthand (the OpenAI SDK's usual
        env config — OPENAI_API_KEY, OPENAI_BASE_URL — selects the
        backend, so any OpenAI-compatible server works; write
        ``openai/Qwen/...`` when the server itself serves slashed ids),
        provider-prefixed names — ``anthropic/…``, ``bedrock/…`` — reach
        that provider, and LiteLLM-routed Claude models get the managed
        prompt prefix cache-marked automatically. The non-stream
        response carries the final answer only; streaming yields the
        agent's visible text as it is produced, including narration before
        tool calls. ``finish_reason`` carries the final turn's native
        finish reason — "stop", or the backend's "length" /
        "content_filter" when the last turn was cut short. For
        the tool-use process and prompt-cache round-trip use
        ``chat(protocol="responses")``.

        Args:
            messages: Conversation messages with 'role' and 'content' keys,
                or a bare query string (it becomes a single user message).
                System/developer messages, wherever they sit, join the
                managed system prompt after the client's
                ``instructions``; the history is text only: tool-role
                turns are rejected, and message fields beyond
                role/content are dropped.
            stream: Enable streaming responses.
            doc_id: Document ID or list of IDs to scope the conversation.
                Keep it identical across a conversation's calls — the
                targeting block it adds is re-set each call and is part
                of the cached prompt prefix. Also enforced at the tool
                layer, not just prompted.
            folder_id: Only ``"root"`` (the whole library) or ``""`` —
                local libraries have no folders; any other value raises.
            temperature: Sampling temperature, passed through to the model.
            stream_metadata: With stream=True, yield chunk dicts instead of
                text pieces.
            enable_citations: Not supported — raises; cite with
                ``chat(citations=True)``, which adds ``<cite>`` markup
                (nothing is resolved).
            model: Backend model name (defaults to ``chat_model``).
            max_turns: Cap on agent turns per call.
            top_p: Nucleus sampling, passed through to the model.
            max_tokens: Per-call output cap,
                passed through; it bounds each backend call in the agent
                loop (the way max_turns bounds the loop), not the whole
                run.
            reasoning_effort: Passed through verbatim as
                LiteLLM's ``reasoning_effort``; each provider maps it to
                its own thinking control, and the values mean what the
                backend says they mean. Unset sends nothing (the
                backend's default applies).
            extra_body: Extra request fields beyond this method's
                parameters, merged last so they win. OpenAI-compatible
                backends take them verbatim in the
                request body; LiteLLM-routed providers take them as
                LiteLLM's own params (mapped or refused per provider).
                The managed prompt, conversation and tools are not
                fields here (``system`` / ``instructions`` / ``input`` /
                ``messages`` / ``tools`` are refused), nor are ``stream``
                / ``doc_id``: each has its own argument. Credentials belong
                in ``backend``, never here.
            extra_headers: Extra HTTP headers merged into
                each backend request; caller headers win. One exception:
                LiteLLM's anthropic adapter owns the ``anthropic-beta``
                header (your value is dropped there).
            backend: Connection overrides for this call's
                backend, merged over the client's ``chat_backend``
                (per-call keys win). Keys are LiteLLM's own connection
                params — ``api_key``, ``base_url``, ``api_version``,
                ``aws_*``, … — passed through verbatim.

        Returns:
            - stream=False: complete response dict ({'id', 'object', 'created',
              'choices', 'usage'})
            - stream=True, stream_metadata=False: iterator of text chunks
            - stream=True, stream_metadata=True: iterator of chunk dicts
        """
        if isinstance(messages, str):
            if not messages.strip():
                raise SuperIndexAPIError(
                    "messages must be a non-empty string or a list of "
                    "message dicts.")
            messages = [{"role": "user", "content": messages}]
        from .local_chat import _refuse_skeleton
        _refuse_skeleton(extra_body)
        self._require_own_chat()
        from .local_chat import run_chat_completions
        return run_chat_completions(
            self, messages, stream=stream, doc_id=doc_id,
            folder_id=folder_id,
            temperature=temperature, stream_metadata=stream_metadata,
            enable_citations=enable_citations, model=model,
            max_turns=max_turns, top_p=top_p, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, extra_body=extra_body,
            extra_headers=extra_headers, backend=backend,
        )

    def _responses(
        self,
        input: Union[str, list[dict[str, Any]]],
        model: Optional[str] = None,
        stream: bool = False,
        doc_id: Optional[Union[str, list[str]]] = None,
        instructions: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_turns: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        reasoning: Optional[dict[str, Any]] = None,
        extra_body: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        backend: Optional[dict[str, Any]] = None,
        *,
        folder_id: Optional[str] = None,
    ) -> Union[dict[str, Any], Iterator[dict[str, Any]]]:
        """
        The engine behind ``chat(protocol="responses")``: document QA over
        the OpenAI Responses protocol.

        Drives your backend's /responses
        end to end (no translation layer). The envelope is official
        Responses shape — ``output`` carries the model-produced items
        and parses with the openai SDK types — and the whole process
        transcript (including the tool outputs the SDK executed) rides
        in the extra ``items`` field.
        Append the returned ``items`` to your next call's ``input`` verbatim
        to keep provider prompt-cache prefix continuity and the agent's
        memory of what it already read.

        Requires a backend that supports the
        Responses API; backends that only speak chat.completions should use
        ``chat(protocol="chat_completions")``. Provider-prefixed models
        (``anthropic/…``) route through LiteLLM's chat.completions adapter
        and are therefore refused here — use
        ``chat(protocol="chat_completions")`` for those.

        Args:
            input: A user message string, or a list of Responses input items
                (round-trip prior ``items`` here).
            model: Backend model name (defaults to ``chat_model``).
            stream: Yield Responses stream events as dicts — one logical
                response per call: per-turn backend lifecycle events are
                collapsed to one opening ``response.created`` and one
                final terminal event, sequence numbers are reassigned
                monotonically, and ``output_index`` is re-based onto the
                single logical ``output``. The final event is the
                terminal ``response.*`` for the run's status; its
                ``response`` carries the tool outputs in ``items``.
            doc_id: Document ID or list of IDs to scope the conversation.
                Keep it identical across a conversation's calls — the
                targeting block it adds is re-set each call and is part
                of the cached prompt prefix. Also enforced at the tool
                layer.
            folder_id: Only ``"root"`` (the whole library) or ``""`` —
                local libraries have no folders; any other value raises.
            instructions: Appended to the managed system prompt.
            temperature / top_p: Passed through to the model.
            max_turns: Cap on agent turns per call.
            max_output_tokens: Per-call output cap, passed through; it
                bounds each backend call in the agent loop (the way
                max_turns bounds the loop), not the whole run. Echoed in
                the envelope.
            reasoning: Responses reasoning options, forwarded verbatim
                (e.g. ``{"effort": "low", "summary": "auto"}``) — the
                values mean what the backend says they mean. Unset sends
                nothing (the backend's default applies).
            extra_body: Extra request fields beyond this method's
                parameters, merged verbatim into the request body (last,
                so they win). Credentials belong in ``backend``, never
                here.
            extra_headers: Extra HTTP headers merged into each request;
                caller headers win over defaults.
            backend: Connection overrides for this call's backend client,
                merged over the client's ``chat_backend`` (per-call keys
                win). Keys are the openai SDK's client params —
                ``api_key``, ``base_url``, ``organization``, … — passed
                verbatim; unknown keys raise.
        """
        self._require_own_chat()
        from .local_chat import run_responses
        return run_responses(
            self, input, model=model, stream=stream, doc_id=doc_id,
            folder_id=folder_id,
            instructions=instructions, temperature=temperature, top_p=top_p,
            max_turns=max_turns, max_output_tokens=max_output_tokens,
            reasoning=reasoning, extra_body=extra_body,
            extra_headers=extra_headers, backend=backend,
        )

    # ---------- DOCUMENT MANAGEMENT ----------

    def get_document(self, doc_id: str) -> dict[str, Any]:
        """
        Get document metadata: {'id', 'name', 'description', 'status',
        'createdAt', 'pageNum', 'folderId', 'metadata'}. Status is always
        "completed" and 'folderId' always None. 'metadata' is your own
        tags from ``submit_document``, or None.

        'createdAt' is UTC with no timezone marker. To show
        it in the user's timezone::

            from datetime import datetime, timezone
            datetime.fromisoformat(doc["createdAt"]).replace(
                tzinfo=timezone.utc).astimezone()
        """
        return self._api.get_document(doc_id=doc_id)

    def get_document_id(self, name: str) -> str:
        """
        Look up a document's ID by its name or path. A path like
        ``"Research/Papers/attention.pdf"`` is accepted: the folder
        part is stripped because document names are unique across
        the library.

        Raises SuperIndexAPIError if no document with that name exists.
        """
        name = name.rsplit("/", 1)[-1] if "/" in name else name
        result = self._api.list_documents(limit=1, name=name)
        docs = result.get("documents", [])
        if docs:
            return docs[0]["id"]
        raise SuperIndexAPIError(f"No document named {name!r} found.")

    def get_document_path(self, doc_id: str) -> str:
        """
        A document's path — its name: local libraries have no folders.
        """
        return self.get_document(doc_id)["name"]

    def delete_document(self, doc_id: str) -> dict[str, Any]:
        """
        Delete a SuperIndex document and all its associated data.

        Returns:
            dict: {'message': 'Document deleted successfully.'}.
        """
        return self._api.delete_document(doc_id=doc_id)

    def list_documents(
        self,
        limit: int = 50,
        offset: int = 0,
        folder_id: Optional[str] = None,
        recursive: bool = False,
    ) -> dict[str, Any]:
        """
        List documents with pagination, newest first.

        Args:
            limit (int): Maximum documents to return (1-100).
            offset (int): Number of documents to skip.
            folder_id (str, optional): Not supported — local libraries
                have no folders; raises.
            recursive (bool): No effect — there are no folders to
                descend into.

        Returns:
            dict: {'documents': [...], 'total', 'limit', 'offset'}.
        """
        return self._api.list_documents(limit=limit, offset=offset,
                                        folder_id=folder_id, recursive=recursive)

    # ---------- AGENT INTEGRATION ----------

    def agent_tools(
        self, include_management: bool = False,
    ) -> list[Callable[..., str]]:
        """
        Plain functions for any agent framework (LangChain, PydanticAI, ...).
        For the OpenAI Agents SDK, prefer ``as_openai_tools()``.

        The built-in tools over the local store (``browse_documents``,
        ``get_document``, ``get_document_structure``,
        ``get_page_content``), then the client's own ``tools``.

        Each function takes JSON-serializable arguments, returns a JSON
        string, and reports failures inside that JSON instead of raising.

        Args:
            include_management (bool): Also expose tools that modify the
                library (adds ``remove_document``).
        """
        from .agent_tools import build_agent_tools
        return build_agent_tools(self, include_management)

    def as_openai_tools(self, include_management: bool = False) -> list:
        """
        Tools for the OpenAI Agents SDK — pass to ``Agent(tools=...)``
        (or ``openai_agent_config()`` for all the Agent slots in one
        call): the in-process tools, any model backend. The tools are the
        framework's own MCP conversion of an in-process server, so
        results reach the model in its shapes.

        ``openai-agents`` is imported only when this method is called.

        Args:
            include_management (bool): Also expose tools that modify the
                library (``remove_document``).
        """
        from .integrations.openai_agents import build_openai_tools
        return build_openai_tools(self, include_management)

    def _local_doc_scope(self, doc_id):
        """doc_id for the tool layer: validated, then passed through as
        the structural allowlist."""
        from .agent_tools import _require_doc_selection
        _require_doc_selection(doc_id)
        return doc_id

    def openai_agent_config(
        self,
        *,
        include_management: bool = False,
        model: Optional[str] = None,
        model_settings: Optional[Any] = None,
        name: str = "SuperIndex",
    ) -> dict[str, Any]:
        """
        Document QA ``Agent`` kwargs for the OpenAI Agents SDK in one
        call::

            agent = Agent(**client.openai_agent_config())

        Sugar over the explicit form — ``agent_instructions`` as the
        instructions and ``as_openai_tools`` as the tools, plus the
        configured ``chat_model``. To target documents, prepend
        ``document_context(doc_id)`` to your first message; to
        customize further, switch to those methods directly. You run this
        config in your own environment, so its model auth comes from
        there — ``chat_backend`` does not travel with it.

        Prompt caching: OpenAI models cache server-side on their own;
        LiteLLM-routed Claude (Anthropic, Bedrock, Vertex) gets its
        cache marks from the bundled ``model_settings``. Pass
        ``model_settings`` here to layer your own on top — your fields
        win and ``extra_args`` merge. Replacing the returned key
        wholesale drops the marks instead.

        Args:
            include_management (bool): Also expose tools that modify the
                library.
            model: Backend model name; overrides the local default. Same
                grammar as ``chat_model`` (LiteLLM names; bare names are
                OpenAI-compatible shorthand).
            model_settings: Your own ``ModelSettings``, merged on top of
                the bundled cache marks; included verbatim when no marks
                apply.
            name (str): Agent display name; in composition it also seeds
                the SDK-derived handoff and ``as_tool`` names.
        """
        from .agent_tools import _base_instructions
        config: dict[str, Any] = {
            "name": name,
            "instructions": _base_instructions(self, include_management),
            "tools": self.as_openai_tools(include_management),
        }
        model = model or (self.chat_model if self._local_chat else None)
        if model:
            config["model"] = _agents_sdk_model_name(model)
            if config["model"].startswith("litellm/"):
                # The runner resolves this model through LiteLLM in the
                # caller's process, outside our completion helpers.
                from .utils import (_mute_litellm_bridge_usage_warning,
                                    _repair_litellm_types)
                _repair_litellm_types()
                _mute_litellm_bridge_usage_warning()
                # Marks follow this lane's routing: the SDK strips litellm/
                # and LiteLLM resolves the rest (bare claude-* → Anthropic);
                # names without the prefix ride the SDK's OpenAI provider.
                from .local_chat import _litellm_claude_marks
                extra_args = _litellm_claude_marks(
                    config["model"].removeprefix("litellm/"))
                if extra_args:
                    from agents import ModelSettings
                    config["model_settings"] = ModelSettings(
                        extra_args=extra_args)
        if model_settings is not None:
            marks = config.get("model_settings")
            config["model_settings"] = (marks.resolve(model_settings)
                                        if marks else model_settings)
        return config

    def agent_instructions(self, *, include_management: bool = False) -> str:
        """
        Orchestration guidance for document QA agents — pass as the agent's
        system prompt (or append to your own).

        The built-in guidance for the in-process tools. The client's
        ``instructions``, if set, follow the guidance, then each extra
        tool's ``guidance``.

        Static by design: document targeting is conversation content, not
        guidance — see ``document_context()``.

        ``include_management``: accepted for symmetry with the tool
        builders; the guidance is a single set.
        """
        from .agent_tools import _base_instructions
        return _base_instructions(self, include_management)

    def document_context(self, doc_id: Union[str, list[str]]) -> str:
        """
        Document targeting text for the first user message: the target
        documents' names and metadata, and the directive to work within
        them. ``chat(doc_id=...)`` places it for you; on the framework
        routes you own the conversation, so lead with it yourself::

            Runner.run_sync(agent, [
                {"role": "user", "content": client.document_context(doc_id)},
                {"role": "user", "content": question},
            ])

        (or prepend it to the prompt text where the framework takes a
        string). Conversation content, not system prompt: it varies per
        request, so keeping it out of the system prompt leaves the cached
        prefix stable.

        ``doc_id``: a document ID or list of IDs, as in ``chat``. Raises
        SuperIndexAPIError if a document does not exist.
        """
        from .agent_tools import doc_targeting_block
        if doc_id is None:
            raise SuperIndexAPIError("doc_id must be a string or a list of "
                                    "strings.")
        return cast(str, doc_targeting_block(self, doc_id))

    def citation_prompt(self, format: str = "cite") -> str:
        """
        The citation discipline for cited answers — grounding rules plus
        how each citation is written — what ``chat(citations=True)``
        adds. Fetch it here to append to
        ``agent_instructions()`` for an agent you build with a framework,
        or to pass another format through ``chat``'s
        ``instructions=`` in place of ``citations=True`` — it is
        guidance, so it belongs in the system prompt.

        ``format`` picks how a citation is written: ``"cite"`` (the
        ``<cite doc= page= block=/>`` tags SuperIndex chat writes and
        renders — the default) or ``"markdown"`` (a bracketed
        ``[doc, p. N]`` reference, for hosts that strip tags); any
        other value raises. Page-level: local page content has no
        blocks.
        """
        from .agent_tools import fetch_citation_prompt
        return fetch_citation_prompt(self, format or "cite")

    def get_citations(
        self,
        answer: str,
        doc_id: Optional[Union[str, list[str]]] = None,
    ) -> list[dict[str, Any]]:
        """
        The citations in a cited answer, each with the id of the document
        it names. Reads both tag formats: ``<cite doc= page= block=/>``
        (``chat(citations=True)``) and ``<doc=…;page=…;block=…>`` (the
        upstream hosted chat's format). The markdown format of
        ``citation_prompt()`` is prose for readers and is not parsed.

        Args:
            answer (str): The answer text, tags included.
            doc_id (str | list[str], optional): The documents the answer
                was about — what ``chat(doc_id=...)`` took. Citations name
                documents, and the ids come from here; without it your own
                library is listed. Two documents sharing a cited name
                raise SuperIndexAPIError naming both ids: pass ``doc_id``
                to pick.

        Returns:
            list: One dict per distinct citation: ``{'document', 'doc_id',
            'page'}``, plus ``'block_id'`` when the tag carries one (local
            page content has no blocks, so nothing more is looked up). A
            document not in the library keeps ``doc_id: None``.
        """
        if not isinstance(answer, str):
            raise SuperIndexAPIError("answer must be a str — the answer text "
                                    "with its citation tags.")
        if doc_id is not None and not isinstance(doc_id, (str, list)):
            raise SuperIndexAPIError("doc_id must be a string or a list of "
                                    "strings.")
        if doc_id is not None and not doc_id:
            raise SuperIndexAPIError("doc_id is empty. Pass the answer's "
                                    "document ids, or omit doc_id to "
                                    "resolve against your library.")
        citations = _parse_citations(answer)
        if not citations:
            return []
        names: dict[str, list[str]] = {}
        if doc_id is not None:
            doc_ids = [doc_id] if isinstance(doc_id, str) else doc_id
            for one_id in dict.fromkeys(doc_ids):
                name = self.get_document(one_id)["name"]
                names.setdefault(name, []).append(one_id)
        else:
            from .agent_tools import _all_documents
            for doc in _all_documents(self):
                if doc.get("name") and doc.get("id"):
                    names.setdefault(doc["name"], []).append(doc["id"])
        resolved: list[dict[str, Any]] = []
        for citation in citations:
            ids = list(dict.fromkeys(names.get(citation["document"], [])))
            if len(ids) > 1:
                raise SuperIndexAPIError(
                    f"{citation['document']!r} names {len(ids)} documents "
                    f"({', '.join(ids)}) — pass doc_id= to pick one.")
            entry: dict[str, Any] = {"document": citation["document"],
                                     "doc_id": ids[0] if ids else None,
                                     "page": citation["page"]}
            block_id = citation.get("block_id")
            if block_id:
                entry["block_id"] = block_id
            resolved.append(entry)
        return resolved

    def resolve_citations(
        self,
        answer: str,
        doc_id: Optional[Union[str, list[str]]] = None,
    ) -> dict[str, Any]:
        """
        Display-ready citations: the answer text with citation tags
        replaced by numbered markdown links, and each citation's full
        data from ``get_citations()`` plus an anchor and index.

        Tags (``<cite doc= page= block=/>`` and ``<doc=…;page=…>``)
        become ``[[1]](#pageindex-citation-01)``, one number per distinct
        citation, so a repeated citation reuses its number. The host
        renders the anchor targets from the ``anchor`` field.

        Args:
            answer (str): The answer text, tags included.
            doc_id (str | list[str], optional): As in ``get_citations()``.

        Returns:
            dict: ``{'answer': str, 'citations': list}`` where each
            citation carries ``'anchor'``, ``'index'`` and the fields
            ``get_citations()`` returns (``'document'``, ``'doc_id'``,
            ``'page'``, and for block-level citations ``'block_id'``).
        """
        entries = self.get_citations(answer, doc_id=doc_id)
        index: dict[Any, int] = {
            (c["document"], c["page"], c.get("block_id")): i
            for i, c in enumerate(_parse_citations(answer), 1)}

        def link(m: re.Match) -> str:
            i = index.get(_citation_key(m))
            if not i:
                return m.group(0)
            return (f"[[{i}]](#pageindex-citation-{i:02d})"
                    f"{m.groupdict().get('inner') or ''}")

        return {
            "answer": _CITE_TAG_RE.sub(link, _OLD_CITATION_RE.sub(link, answer)),
            "citations": [{"anchor": f"pageindex-citation-{i:02d}", "index": i,
                           **entry} for i, entry in enumerate(entries, 1)],
        }


class SuperIndexLocalClient(SuperIndexClient):
    """The same client under its older name."""
