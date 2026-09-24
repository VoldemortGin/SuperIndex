"""Which extraction backend is active, and a single façade over it.

Policy (this is what "Azure DI by default" means in this project):

* `AZURE_DI_ENDPOINT` **and** `AZURE_DI_KEY` both set  ->  **Azure Document
  Intelligence is the default extractor** for every PDF, in every entry point:
  `scripts/02_qa_test.py` (engine indexing) and `superindex/nav/build.py` (the two-level
  navigator). Nothing else needs to be passed on the command line.
* Otherwise -> the built-in text-layer path (PyPDF2, exactly what the engine's
  local mode does), unchanged.

Two things this module is careful about:

1. **It never switches silently.** `describe()` returns a human-readable line
   and every entry point prints it at startup, so a run always says which
   extractor produced its text.
2. **A configured-but-failing Azure call fails loudly by default.** Quietly
   degrading to the text layer would hide a broken key or a wrong endpoint and
   silently change the quality of the index. Set `AZURE_DI_FALLBACK=1` to opt
   into the soft behaviour.

The Azure path analyses each PDF once and caches the result in-process, because
two callers want different views of it: the engine wants per-page text, `nav`
wants the whole Markdown document.
"""
from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from superindex.extractors.azure_di import (
    PAGE_MARKER,
    AzureDIConfig,
    AzureDIError,
    AzureDocIntelligence,
)

BACKEND_AZURE = "azure-di"
BACKEND_TEXT_LAYER = "text-layer"

_cache_lock = threading.Lock()
_analysis_cache: dict[tuple[str, float], dict[str, Any]] = {}


# ------------------------------------------------------------------ policy
def azure_config(env: Optional[dict[str, str]] = None) -> AzureDIConfig:
    return AzureDIConfig.from_env(env)


def is_azure_configured(env: Optional[dict[str, str]] = None) -> bool:
    cfg = azure_config(env)
    return bool(cfg.endpoint and cfg.key)


def fallback_enabled(env: Optional[dict[str, str]] = None) -> bool:
    e = env if env is not None else os.environ
    return (e.get("AZURE_DI_FALLBACK") or "").strip().lower() in {
        "1", "true", "yes", "on"}


@dataclass(frozen=True)
class BackendInfo:
    name: str
    detail: str

    def __str__(self) -> str:
        return f"{self.name} — {self.detail}"


def describe(env: Optional[dict[str, str]] = None) -> BackendInfo:
    """One line naming the active extractor and why."""
    e = env if env is not None else os.environ
    cfg = azure_config(e)
    if cfg.endpoint and cfg.key:
        host = cfg.endpoint.split("//", 1)[-1].split("/", 1)[0]
        extra = []
        if cfg.features:
            extra.append("features=" + ",".join(cfg.features))
        if fallback_enabled(e):
            extra.append("fallback=on")
        suffix = ("  [" + " ".join(extra) + "]") if extra else ""
        return BackendInfo(
            BACKEND_AZURE,
            f"Azure Document Intelligence is configured ({host}, model={cfg.model}, "
            f"format={cfg.output_format}) — used for all PDFs{suffix}")
    missing = [n for n, v in (("AZURE_DI_ENDPOINT", cfg.endpoint),
                              ("AZURE_DI_KEY", cfg.key)) if not v]
    return BackendInfo(
        BACKEND_TEXT_LAYER,
        "PDF text layer via PyPDF2 (engine default). "
        f"Set {' and '.join(missing)} in .env to switch to Azure Document Intelligence.")


def announce(env: Optional[dict[str, str]] = None) -> BackendInfo:
    """Print the active backend. Every entry point calls this at startup."""
    info = describe(env)
    print(f"提取后端: {info.name}")
    print(f"          {info.detail}")
    return info


# ------------------------------------------------------------------ façade
class Extractor:
    """Unified extraction façade over the active backend."""

    def __init__(self, env: Optional[dict[str, str]] = None,
                 *, verbose: bool = False):
        self._env = env if env is not None else os.environ
        self._verbose = verbose
        self._cfg: Optional[AzureDIConfig] = None
        self._adi: Optional[AzureDocIntelligence] = None
        self.info = describe(self._env)
        self._fallback = fallback_enabled(self._env)

    # -- azure plumbing ---------------------------------------------------
    @property
    def uses_azure(self) -> bool:
        return self.info.name == BACKEND_AZURE

    @property
    def strict(self) -> bool:
        """True when a failed extraction must abort rather than degrade.

        A configured-but-broken Azure endpoint is a config bug, and carrying on
        would quietly produce an index built from the weaker text layer — the
        exact silent quality change this module exists to prevent.
        """
        return self.uses_azure and not self._fallback

    @property
    def adi(self) -> AzureDocIntelligence:
        if self._adi is None:
            self._cfg = azure_config(self._env)
            self._cfg.validate()
            self._adi = AzureDocIntelligence(self._cfg)
        return self._adi

    def _warn(self, msg: str) -> None:
        print(f"  ⚠️  {msg}", file=sys.stderr, flush=True)

    def _analyze(self, pdf: Path) -> dict[str, Any]:
        """Analyse once per (path, mtime) and reuse."""
        try:
            key = (str(pdf.resolve()), pdf.stat().st_mtime)
        except OSError:
            key = (str(pdf), 0.0)
        with _cache_lock:
            hit = _analysis_cache.get(key)
        if hit is not None:
            return hit
        if self._verbose:
            print(f"    Azure DI 分析 {pdf.name} ...", flush=True)
        result = self.adi.analyze(pdf)
        with _cache_lock:
            _analysis_cache[key] = result
        return result

    def _azure_or_text(self, fn_name: str, pdf: Path):
        """Run the Azure branch, honouring AZURE_DI_FALLBACK on failure."""
        try:
            return getattr(self, fn_name)(pdf)
        except AzureDIError as exc:
            if not self._fallback:
                raise AzureDIError(
                    f"Azure Document Intelligence failed on {pdf.name}:\n  {exc}\n"
                    "  Set AZURE_DI_FALLBACK=1 in .env to fall back to the PDF "
                    "text layer instead of failing."
                ) from exc
            self._warn(f"{pdf.name}: Azure DI 失败，回退到文本层（{exc}）")
            return _text_layer(self, fn_name, pdf)

    # -- public API -------------------------------------------------------
    def page_texts(self, pdf: "str | Path") -> list[str]:
        """Per-page plain text. Drop-in for the engine's `_extract_page_texts`."""
        pdf = Path(pdf)
        if self.uses_azure:
            return self._azure_or_text("_azure_page_texts", pdf)
        return _text_layer(self, "_azure_page_texts", pdf)

    def document_text(self, pdf: "str | Path") -> str:
        """Whole document as Markdown, with `<!-- page: N -->` markers.

        This is what `nav` wants: headings and tables preserved so the chapter
        tree and the page anchors both survive.
        """
        pdf = Path(pdf)
        if self.uses_azure:
            return self._azure_or_text("_azure_document_text", pdf)
        return _text_layer(self, "_azure_document_text", pdf)

    # -- azure branches ---------------------------------------------------
    def _azure_page_texts(self, pdf: Path) -> list[str]:
        result = self._analyze(pdf)
        content = result.get("content") or ""
        pages = sorted(result.get("pages") or [],
                       key=lambda p: p.get("pageNumber") or 0)
        out: list[str] = []
        for page in pages:
            spans = [s for s in (page.get("spans") or [])
                     if isinstance(s.get("offset"), int)]
            if not spans:
                out.append("")
                continue
            start = min(s["offset"] for s in spans)
            end = max(s["offset"] + (s.get("length") or 0) for s in spans)
            out.append(content[start:end].strip())
        return out

    def _azure_document_text(self, pdf: Path) -> str:
        return AzureDocIntelligence.to_markdown(self._analyze(pdf),
                                                page_markers=True)


def _text_layer(extractor: Extractor, fn_name: str, pdf: Path):
    """The non-Azure path: PyPDF2 per page, page markers synthesised for nav."""
    if fn_name == "_azure_page_texts":
        return _pypdf2_page_texts(pdf)
    pages = _pypdf2_page_texts(pdf)
    parts = []
    for i, text in enumerate(pages, 1):
        parts.append(PAGE_MARKER.format(n=i))
        parts.append(text)
    return "\n\n".join(parts)


def _pypdf2_page_texts(pdf: Path) -> list[str]:
    """Delegate to the engine's own extractor so the fallback is byte-identical."""
    try:
        from superindex.engine.local_api import LocalAPI
        return LocalAPI._extract_page_texts(str(pdf))
    except Exception:  # noqa: BLE001 - engine not importable; inline copy
        import PyPDF2
        with open(pdf, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            return [_scrub(page.extract_text() or "") for page in reader.pages]


def _scrub(text: str) -> str:
    """Drop lone surrogates — they break every downstream utf-8 JSON write."""
    return text.encode("utf-8", "replace").decode("utf-8")


# ------------------------------------------------------ engine integration
def install_into_pageindex(env: Optional[dict[str, str]] = None,
                           *, verbose: bool = True) -> BackendInfo:
    """Make the engine's local mode use the active backend.

    The engine reads the PDF text layer in `LocalAPI._extract_page_texts`, a
    staticmethod returning one string per page. We swap in our own callable
    that returns the same shape, so nothing inside `superindex/engine/` is edited and
    the upstream diff stays clean.

    Returns the active `BackendInfo`. A no-op (beyond reporting) when Azure is
    not configured, because then the built-in behaviour already is the right one.
    """
    info = describe(env)
    if info.name != BACKEND_AZURE:
        if verbose:
            announce(env)
        return info

    from superindex.engine.local_api import LocalAPI

    extractor = Extractor(env, verbose=verbose)
    LocalAPI._extract_page_texts = staticmethod(  # type: ignore[assignment]
        lambda file_path: extractor.page_texts(Path(file_path)))
    if verbose:
        announce(env)
        print("          → 已接管引擎的 PDF 文本抽取（_extract_page_texts）")
    return info


def reset_cache() -> None:
    """Drop the in-process analysis cache (used by tests)."""
    with _cache_lock:
        _analysis_cache.clear()


__all__ = [
    "BACKEND_AZURE", "BACKEND_TEXT_LAYER", "BackendInfo", "Extractor",
    "announce", "azure_config", "describe", "fallback_enabled",
    "install_into_pageindex", "is_azure_configured", "reset_cache",
]
