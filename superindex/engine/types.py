"""Config shapes for the constructor's ``index=`` / ``chat=`` slots.

The slots are the grouped spelling of the flat constructor arguments —
the same arguments with the side prefix factored out of the names
(``index={"model": ...}`` is ``index_model=``), one spelling per side.
An optional ``"mode"`` field may state ``"local"``, the only mode.
"""
from __future__ import annotations

import os
from typing import Literal, TypedDict, Union


class LocalIndexConfig(TypedDict, total=False):
    """Documents indexed and stored locally."""

    mode: Literal["local"]
    model: str
    summary_model: str
    backend: dict
    storage_path: Union[str, os.PathLike[str]]


class ChatConfig(TypedDict, total=False):
    """The chat side: your own model — the agent runs in your process on
    your keys."""

    mode: Literal["local"]
    model: str
    backend: dict


class ChatProcessOptions(TypedDict, total=False):
    """``chat(show_process=...)``'s display config. Omitted keys default
    on (``max_chars``: 200); ``show_process=True`` is all defaults."""

    thinking: bool
    tool_call: bool
    tool_result: bool
    max_chars: int


IndexConfig = LocalIndexConfig
