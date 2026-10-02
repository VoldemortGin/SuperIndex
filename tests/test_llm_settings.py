"""Offline tests for `LLMSettings.resolve` — the OPENAI_MODEL / OPENAI_BASE_URL /
OPENAI_API_KEY spelling next to the SUPERINDEX_* settings. No LLM, no network.

    pytest tests/test_llm_settings.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from superindex.runtime import ConfigError, LEGACY_ENV, LLMSettings  # noqa: E402

_NAMES = ("OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY",
          *LEGACY_ENV, *LEGACY_ENV.values())


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _NAMES:
        monkeypatch.delenv(name, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_no_openai_vars_changes_nothing() -> None:
    s = LLMSettings.resolve()
    assert (s.chat_model, s.index_model, s.base_url, s.api_key) == (None, None, None, None)


def test_three_openai_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="my-model", OPENAI_BASE_URL="http://gw/v1",
         OPENAI_API_KEY="k1")
    s = LLMSettings.resolve()
    assert s.chat_model == "openai/my-model"
    assert s.index_model == "openai/my-model"  # one set of vars covers both lanes
    assert s.base_url == "http://gw/v1"
    assert s.api_key == "k1"
    assert s.chat_backend() == {"base_url": "http://gw/v1", "api_key": "k1"}


def test_key_and_url_alone_do_not_switch_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_BASE_URL="http://gw/v1", OPENAI_API_KEY="k1")
    s = LLMSettings.resolve()
    assert (s.chat_model, s.index_model, s.base_url, s.api_key) == (None, None, None, None)
    assert s.chat_backend() is None
    with pytest.raises(ConfigError, match="No chat model configured"):
        s.require("chat")


def test_empty_model_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="  ", OPENAI_BASE_URL="http://gw/v1", OPENAI_API_KEY="k1")
    s = LLMSettings.resolve()
    assert s.chat_model is None and s.base_url is None and s.api_key is None


def test_model_name_with_slash_still_gets_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="Qwen/Qwen2.5-72B")
    assert LLMSettings.resolve().chat_model == "openai/Qwen/Qwen2.5-72B"


def test_existing_openai_prefix_is_not_doubled(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="openai/my-model")
    assert LLMSettings.resolve().chat_model == "openai/my-model"


def test_superindex_wins_per_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="a", OPENAI_BASE_URL="http://openai/v1",
         OPENAI_API_KEY="openai-key",
         SUPERINDEX_CHAT_MODEL="deepseek/deepseek-chat",
         SUPERINDEX_API_KEY_OVERRIDE="si-key")
    s = LLMSettings.resolve()
    assert s.chat_model == "deepseek/deepseek-chat"
    assert s.api_key == "si-key"
    assert s.base_url == "http://openai/v1"  # SUPERINDEX_BASE_URL unset -> OPENAI_BASE_URL
    assert s.index_model == "openai/a"       # SUPERINDEX_INDEX_MODEL unset -> OPENAI_MODEL


def test_empty_superindex_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="a", OPENAI_BASE_URL="http://openai/v1",
         SUPERINDEX_CHAT_MODEL="", SUPERINDEX_BASE_URL="  ")
    s = LLMSettings.resolve()
    assert s.chat_model == "openai/a"
    assert s.base_url == "http://openai/v1"


def test_legacy_pageindex_name_beats_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="a", PAGEINDEX_CHAT_MODEL="ollama_chat/q")
    assert LLMSettings.resolve().chat_model == "ollama_chat/q"


def test_cli_values_win(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="a", OPENAI_BASE_URL="http://openai/v1",
         OPENAI_API_KEY="openai-key")
    s = LLMSettings.resolve(chat_model="x/y", base_url="http://cli", api_key="cli-key",
                            index_model="x/z")
    assert (s.chat_model, s.base_url, s.api_key, s.index_model) == (
        "x/y", "http://cli", "cli-key", "x/z")


def test_reasoning_effort_is_not_taken_from_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, OPENAI_MODEL="a")
    assert LLMSettings.resolve().reasoning_effort is None
