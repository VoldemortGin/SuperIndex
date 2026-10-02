"""Process-wide setup: frozen-aware paths, `.env` loading, offline LLM knobs,
and the model/endpoint settings every subcommand shares.

Works the same from a pip install, a source checkout and a PyInstaller
build: ``app_dir()`` is where user-editable files live — `.env`, the default
store — the executable's folder when frozen, the working directory otherwise.
(The web UI's static files ship inside the package; see
`superindex/webapp/server.py`.)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Settings renamed from PAGEINDEX_* to SUPERINDEX_*; the old names still work.
LEGACY_ENV = {
    "SUPERINDEX_INDEX_MODEL": "PAGEINDEX_INDEX_MODEL",
    "SUPERINDEX_CHAT_MODEL": "PAGEINDEX_CHAT_MODEL",
    "SUPERINDEX_BASE_URL": "PAGEINDEX_BASE_URL",
    "SUPERINDEX_API_KEY_OVERRIDE": "PAGEINDEX_API_KEY_OVERRIDE",
    "SUPERINDEX_REASONING_EFFORT": "PAGEINDEX_REASONING_EFFORT",
}
_warned: set[str] = set()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def getenv(name: str, default: str | None = None) -> str | None:
    """``os.getenv`` that also reads the pre-rename PAGEINDEX_* spelling of a
    SUPERINDEX_* setting (the new name wins), telling stderr once per name."""
    value = os.environ.get(name)
    if value is not None:
        return value
    old = LEGACY_ENV.get(name)
    if old is None or old not in os.environ:
        return default
    if old not in _warned:
        _warned.add(old)
        print(f"note: {old} has been renamed to {name}; please update your .env",
              file=sys.stderr)
    return os.environ[old]


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
    return app_dir() / "superindex_store"


class ConfigError(RuntimeError):
    """A required setting is missing; the message says how to set it."""


def _env_places() -> str:
    if is_frozen():
        return f"working directory and {app_dir()}"
    return f"working directory {Path.cwd()}"


# Standing guidance for the answering agent (ask / serve / batch) unless
# --instructions[-file] or SUPERINDEX_INSTRUCTIONS[_FILE] replaces it.
DEFAULT_INSTRUCTIONS = (
    "You are a financial analyst answering questions about the documents in "
    "the store, such as companies' annual and interim reports. Answer with "
    "the exact figures, units and periods stated in the documents, and name "
    "the reporting period each figure belongs to. If the documents do not "
    "contain the answer, say so plainly instead of guessing."
)
INSTRUCTIONS_ENV = "SUPERINDEX_INSTRUCTIONS"
INSTRUCTIONS_FILE_ENV = "SUPERINDEX_INSTRUCTIONS_FILE"


def resolve_instructions(text: str | None = None, file: str | None = None) -> str:
    """The agent's standing guidance, first set of: `text` (--instructions),
    `file` (--instructions-file), SUPERINDEX_INSTRUCTIONS,
    SUPERINDEX_INSTRUCTIONS_FILE, else DEFAULT_INSTRUCTIONS."""
    if text and text.strip():
        return text.strip()
    if file:
        return _read_instructions(file)
    env_text = os.getenv(INSTRUCTIONS_ENV, "").strip()
    if env_text:
        return env_text
    env_file = os.getenv(INSTRUCTIONS_FILE_ENV, "").strip()
    if env_file:
        return _read_instructions(env_file)
    return DEFAULT_INSTRUCTIONS


def _read_instructions(path: str) -> str:
    source = Path(path).expanduser()
    try:
        content = source.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read instructions file {source}: {exc}") from exc
    if not content:
        raise ConfigError(f"instructions file {source} is empty")
    return content


def _openai_compat() -> dict[str, str]:
    """The OPENAI_MODEL / OPENAI_BASE_URL / OPENAI_API_KEY spelling of a model
    on an OpenAI-compatible endpoint, as ``{"model", "base_url", "api_key"}``
    (empty values left out). Only OPENAI_MODEL switches it on — OPENAI_API_KEY
    and OPENAI_BASE_URL are often set for other tools and mean nothing alone.
    The model gets the ``openai/`` prefix LiteLLM routes by, even when the name
    itself contains a ``/`` (``Org/model`` -> ``openai/Org/model``)."""
    model = os.environ.get("OPENAI_MODEL", "").strip()
    if not model:
        return {}
    if not model.startswith("openai/"):
        model = f"openai/{model}"
    found = {"model": model,
             "base_url": os.environ.get("OPENAI_BASE_URL", "").strip(),
             "api_key": os.environ.get("OPENAI_API_KEY", "").strip()}
    return {k: v for k, v in found.items() if v}


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
        hard-coded cloud model. Per setting: CLI > SUPERINDEX_* > PAGEINDEX_* >
        OPENAI_* (see `_openai_compat`; the index model falls back to
        OPENAI_MODEL too, so one set of OPENAI_* variables covers both lanes)."""
        openai = _openai_compat()

        def pick(cli: str | None, env: str, fallback: str | None = None) -> str | None:
            if cli is not None:
                return cli.strip() or None
            value = (getenv(env) or "").strip()
            return value or fallback

        # Unset means "send nothing":
        # local Ollama models reject the parameter — ollama_chat with
        # "does not support thinking", the /v1 route with UnsupportedParamsError.
        effort = pick(reasoning_effort, "SUPERINDEX_REASONING_EFFORT")
        return cls(
            index_model=pick(index_model, "SUPERINDEX_INDEX_MODEL", openai.get("model")),
            chat_model=pick(chat_model, "SUPERINDEX_CHAT_MODEL", openai.get("model")),
            base_url=pick(base_url, "SUPERINDEX_BASE_URL", openai.get("base_url")),
            api_key=pick(api_key, "SUPERINDEX_API_KEY_OVERRIDE", openai.get("api_key")),
            reasoning_effort=effort,
        )

    def require(self, role: str) -> str:
        model = self.index_model if role == "index" else self.chat_model
        if not model:
            env = "SUPERINDEX_INDEX_MODEL" if role == "index" else "SUPERINDEX_CHAT_MODEL"
            flag = "--index-model" if role == "index" else "--chat-model"
            raise ConfigError(
                f"No {role} model configured. Set {env} in .env (looked in the "
                f"{_env_places()}) or pass {flag}; "
                "OPENAI_MODEL / OPENAI_BASE_URL / OPENAI_API_KEY also work. "
                "Example for a local Ollama: "
                f"{env}=ollama_chat/qwen2.5:7b and "
                "SUPERINDEX_BASE_URL=http://localhost:11434 — see .env.example.")
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
        """Connection overrides for the engine's chat lane (`base_url` spelling)."""
        backend: dict[str, str] = {}
        if self.base_url:
            backend["base_url"] = self.base_url
        if self.api_key:
            backend["api_key"] = self.api_key
        return backend or None
