"""Process-wide setup: frozen-aware paths, `.env` loading, offline LLM knobs,
and the model/endpoint settings every subcommand shares.

Works the same from a source checkout and from a PyInstaller build:
``app_dir()`` is where user-editable files live — `.env`, the default store —
the executable's folder when frozen, the repository root otherwise. (Bundled
read-only data, the web UI's static files, resolves under ``sys._MEIPASS``;
see `webapp/server.py`.)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _REPO_ROOT


def set_offline_defaults() -> None:
    """Must run before litellm is imported anywhere: litellm otherwise fetches
    its model-cost map over the network at import time."""
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


def load_env() -> list[Path]:
    """Load `.env` from the working directory, then from the executable's
    folder. Neither overrides a variable that is already set, so the process
    environment wins over the working directory, which wins over the app dir."""
    from dotenv import load_dotenv

    loaded: list[Path] = []
    seen: set[Path] = set()
    for folder in (Path.cwd(), app_dir()):
        env = (folder / ".env").resolve()
        if env in seen or not env.is_file():
            continue
        seen.add(env)
        load_dotenv(env, override=False, encoding="utf-8")
        loaded.append(env)
    return loaded


def configure_litellm() -> None:
    """Offline-safe litellm: never download a HuggingFace tokenizer (token
    counting then always uses the tiktoken encoding litellm bundles)."""
    set_offline_defaults()
    import litellm

    litellm.disable_hf_tokenizer_download = True


def default_store() -> Path:
    env = os.getenv("SUPERINDEX_STORE", "").strip()
    if env:
        return Path(env).expanduser()
    if is_frozen():
        return app_dir() / "superindex_store"
    return app_dir() / "results" / "superindex_store"


class ConfigError(RuntimeError):
    """A required setting is missing; the message says how to set it."""


@dataclass
class LLMSettings:
    index_model: str | None
    chat_model: str | None
    base_url: str | None
    api_key: str | None
    reasoning_effort: str | None

    @classmethod
    def resolve(cls, index_model: str | None = None,
                chat_model: str | None = None, base_url: str | None = None,
                api_key: str | None = None,
                reasoning_effort: str | None = None) -> LLMSettings:
        """CLI values win over the environment; nothing falls back to a
        hard-coded cloud model."""
        def pick(cli: str | None, env: str) -> str | None:
            value = cli if cli is not None else os.getenv(env)
            if value is None:
                return None
            return value.strip() or None

        # Unset means "send nothing" (webapp/server.py defaults to "low"):
        # local Ollama models reject the parameter — ollama_chat with
        # "does not support thinking", the /v1 route with UnsupportedParamsError.
        effort = pick(reasoning_effort, "PAGEINDEX_REASONING_EFFORT")
        return cls(
            index_model=pick(index_model, "PAGEINDEX_INDEX_MODEL"),
            chat_model=pick(chat_model, "PAGEINDEX_CHAT_MODEL"),
            base_url=pick(base_url, "PAGEINDEX_BASE_URL"),
            api_key=pick(api_key, "PAGEINDEX_API_KEY_OVERRIDE"),
            reasoning_effort=effort,
        )

    def require(self, role: str) -> str:
        model = self.index_model if role == "index" else self.chat_model
        if not model:
            env = "PAGEINDEX_INDEX_MODEL" if role == "index" else "PAGEINDEX_CHAT_MODEL"
            flag = "--index-model" if role == "index" else "--chat-model"
            raise ConfigError(
                f"No {role} model configured. Set {env} in .env (looked in the "
                f"working directory and {app_dir()}) or pass {flag}. "
                "Example for a local Ollama: "
                f"{env}=ollama_chat/qwen2.5:7b and "
                "PAGEINDEX_BASE_URL=http://localhost:11434 — see .env.example.")
        return model

    def index_backend(self) -> dict[str, str] | None:
        """LiteLLM connection overrides for the indexing lane."""
        backend: dict[str, str] = {}
        if self.base_url:
            backend["api_base"] = self.base_url
        if self.api_key:
            backend["api_key"] = self.api_key
        return backend or None

    def chat_backend(self) -> dict[str, str] | None:
        """Connection overrides for PageIndex's chat lane (`base_url` spelling)."""
        backend: dict[str, str] = {}
        if self.base_url:
            backend["base_url"] = self.base_url
        if self.api_key:
            backend["api_key"] = self.api_key
        return backend or None
