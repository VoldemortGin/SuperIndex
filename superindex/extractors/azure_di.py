"""Azure AI Document Intelligence extractor.

Turns a PDF into Markdown — with tables preserved as Markdown tables and page
anchors injected as HTML comments — so it can be fed to `nav.build` or to
the engine's Markdown path.

Why this exists
---------------
The engine's built-in local path reads a PDF's own text layer with PyPDF2.
On financial reports that works for running prose but degrades badly on
tables and charts: a bar-chart page comes out as `175230`, two numbers fused,
and the label-to-value association is gone. It also refuses scanned PDFs
outright (no text layer, no OCR).

Azure Document Intelligence runs a layout model over the rendered page, so it
handles all three cases:

* **tables** — returned as real Markdown tables
* **scanned / image-only PDFs** — OCR'd, so the local-mode refusal no longer applies
* **page numbers** — `analyzeResult.pages[].spans[].offset` tells us where each
  page starts in the content string, so we can inject `<!-- page: N -->`
  markers and keep the page-level citations the Markdown path otherwise loses

Transport is plain REST over httpx — no Azure SDK dependency.

API reference:
https://learn.microsoft.com/rest/api/aiservices/document-models/analyze-document
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx

DEFAULT_API_VERSION = "2024-11-30"
DEFAULT_MODEL = "prebuilt-layout"
# Models worth knowing: prebuilt-read (text only, cheapest), prebuilt-layout
# (adds tables + headings — the one you want for reports), prebuilt-document
# (adds key/value pairs), or a custom model id.
LAYOUT_MODELS = ("prebuilt-read", "prebuilt-layout", "prebuilt-document")
# Features that are useful on financial reports. `formulas` matters because
# ratio and per-share computations appear as inline math in some filings.
DEFAULT_FEATURES = ("formulas",)
PAGE_MARKER = "<!-- page: {n} -->"


class AzureDIError(RuntimeError):
    """Raised for configuration problems and non-retryable service errors."""


@dataclass
class AzureDIConfig:
    endpoint: str = ""
    key: str = ""
    api_version: str = DEFAULT_API_VERSION
    model: str = DEFAULT_MODEL
    output_format: str = "markdown"          # markdown | text
    # unicodeCodePoint keeps offsets aligned with Python string indices; the
    # default (textElements) counts grapheme clusters and would misplace the
    # page markers.
    string_index_type: str = "unicodeCodePoint"
    features: tuple[str, ...] = DEFAULT_FEATURES
    locale: str = ""                          # e.g. "en-US", "zh-Hans"; "" = auto
    poll_interval: float = 2.0
    poll_timeout: float = 900.0
    request_timeout: float = 180.0

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "AzureDIConfig":
        e = env if env is not None else os.environ

        def get(*names: str, default: str = "") -> str:
            for n in names:
                v = e.get(n)
                if v:
                    return v.strip()
            return default

        feats = get("AZURE_DI_FEATURES")
        return cls(
            endpoint=get("AZURE_DI_ENDPOINT", "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"),
            key=get("AZURE_DI_KEY", "AZURE_DOCUMENT_INTELLIGENCE_KEY"),
            api_version=get("AZURE_DI_API_VERSION", default=DEFAULT_API_VERSION),
            model=get("AZURE_DI_MODEL", default=DEFAULT_MODEL),
            output_format=get("AZURE_DI_OUTPUT_FORMAT", default="markdown"),
            string_index_type=get("AZURE_DI_STRING_INDEX_TYPE",
                                   default="unicodeCodePoint"),
            features=tuple(f.strip() for f in feats.split(",") if f.strip()),
            locale=get("AZURE_DI_LOCALE"),
            poll_interval=float(get("AZURE_DI_POLL_INTERVAL", default="2")),
            poll_timeout=float(get("AZURE_DI_POLL_TIMEOUT", default="900")),
        )

    def validate(self) -> None:
        missing = [n for n, v in (("AZURE_DI_ENDPOINT", self.endpoint),
                                  ("AZURE_DI_KEY", self.key)) if not v]
        if missing:
            raise AzureDIError(
                "Azure Document Intelligence is not configured. Missing: "
                + ", ".join(missing)
                + "\n  Copy .env.example to .env and fill in the Azure section."
            )
        if not self.endpoint.startswith(("http://", "https://")):
            raise AzureDIError(
                f"AZURE_DI_ENDPOINT must be a full URL, got {self.endpoint!r}.\n"
                "  It looks like https://<your-resource>.cognitiveservices.azure.com/"
            )
        if self.output_format not in ("markdown", "text"):
            raise AzureDIError(
                f"AZURE_DI_OUTPUT_FORMAT must be markdown or text, got "
                f"{self.output_format!r}")
        if self.string_index_type not in ("textElements", "unicodeCodePoint",
                                          "utf16CodeUnit"):
            raise AzureDIError(
                f"AZURE_DI_STRING_INDEX_TYPE must be textElements, "
                f"unicodeCodePoint or utf16CodeUnit, got {self.string_index_type!r}")

    @property
    def base_url(self) -> str:
        return self.endpoint.rstrip("/")


class AzureDocIntelligence:
    """Thin REST client for the analyze-document operation."""

    def __init__(self, config: Optional[AzureDIConfig] = None,
                 client: Optional[httpx.Client] = None):
        self.cfg = config or AzureDIConfig.from_env()
        self.cfg.validate()
        self._client = client or httpx.Client(timeout=self.cfg.request_timeout)

    # ------------------------------------------------------------ internals
    @property
    def _headers(self) -> dict[str, str]:
        return {"Ocp-Apim-Subscription-Key": self.cfg.key}

    def _analyze_url(self, pages: Optional[str]) -> str:
        url = (f"{self.cfg.base_url}/documentintelligence/documentModels/"
               f"{self.cfg.model}:analyze")
        params = {
            "api-version": self.cfg.api_version,
            "outputContentFormat": self.cfg.output_format,
            "stringIndexType": self.cfg.string_index_type,
        }
        if self.cfg.features:
            params["features"] = ",".join(self.cfg.features)
        if self.cfg.locale:
            params["locale"] = self.cfg.locale
        if pages:
            params["pages"] = pages
        return str(httpx.URL(url, params=params))

    def _raise_for(self, resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            body = resp.json()
            err = body.get("error") or {}
            detail = err.get("message") or json.dumps(body)[:300]
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:300]
        hint = {
            401: "  → key rejected. Check AZURE_DI_KEY.",
            403: "  → key valid but not authorised for this resource.",
            404: "  → endpoint or model not found. Check AZURE_DI_ENDPOINT "
                 "and AZURE_DI_MODEL.",
            429: "  → rate limited (free tier F0 allows very few pages/min). "
                 "Lower --workers or wait.",
        }.get(resp.status_code)
        raise AzureDIError(
            f"Azure DI returned HTTP {resp.status_code}: {detail}"
            + (f"\n{hint}" if hint else ""))

    # -------------------------------------------------------------- analyze
    def analyze(self, source: "Path | bytes", *,
                pages: Optional[str] = None) -> dict[str, Any]:
        """Submit a PDF and return the finished `analyzeResult`.

        `source` is a local path or raw PDF bytes. `pages` is a 1-based range
        spec like "1-3,5" — useful for a cheap smoke test.
        """
        if isinstance(source, (str, Path)):
            data = Path(source).read_bytes()
        else:
            data = source
        if not data.startswith(b"%PDF-"):
            raise AzureDIError("input does not look like a PDF (missing %PDF- header)")

        url = self._analyze_url(pages)
        try:
            resp = self._client.post(
                url,
                headers={**self._headers, "Content-Type": "application/pdf"},
                content=data,
            )
        except httpx.HTTPError as exc:
            raise AzureDIError(f"could not reach Azure DI: {exc}") from exc
        self._raise_for(resp)
        if resp.status_code != 202:
            raise AzureDIError(
                f"expected 202 Accepted, got {resp.status_code}: {resp.text[:200]}")
        op = resp.headers.get("Operation-Location")
        if not op:
            raise AzureDIError("response had no Operation-Location header")
        return self._poll(op, first_delay=_retry_after(resp))

    def _poll(self, op_location: str, first_delay: float = 0.0) -> dict[str, Any]:
        deadline = time.time() + self.cfg.poll_timeout
        delay = first_delay or self.cfg.poll_interval
        while True:
            if time.time() > deadline:
                raise AzureDIError(
                    f"analysis did not finish within {self.cfg.poll_timeout:.0f}s")
            time.sleep(delay)
            try:
                resp = self._client.get(op_location, headers=self._headers)
            except httpx.HTTPError as exc:
                raise AzureDIError(f"polling failed: {exc}") from exc
            self._raise_for(resp)
            body = resp.json()
            status = (body.get("status") or "").lower()
            if status == "succeeded":
                result = body.get("analyzeResult")
                if not result:
                    raise AzureDIError("analysis succeeded but returned no analyzeResult")
                return result
            if status == "failed":
                err = body.get("error") or {}
                raise AzureDIError(
                    "analysis failed: "
                    + (err.get("message") or json.dumps(body)[:300]))
            delay = _retry_after(resp) or self.cfg.poll_interval

    # ------------------------------------------------------------ rendering
    @staticmethod
    def to_markdown(result: dict[str, Any], *,
                    page_markers: bool = True) -> str:
        """`analyzeResult` -> Markdown, optionally with `<!-- page: N -->` markers.

        Azure returns one flat content string; the page boundaries live in
        `pages[].spans[].offset`. Inserting markers at those offsets restores
        the page-level addressing that the Markdown path otherwise loses.
        """
        content = result.get("content") or ""
        if not page_markers or not content:
            return content

        marks: list[tuple[int, int]] = []
        for page in result.get("pages") or []:
            pno = page.get("pageNumber")
            spans = page.get("spans") or []
            if isinstance(pno, int) and spans:
                off = spans[0].get("offset")
                if isinstance(off, int):
                    marks.append((off, pno))
        # descending so earlier offsets stay valid as we splice
        for off, pno in sorted(marks, reverse=True):
            off = max(0, min(off, len(content)))
            content = (content[:off]
                       + f"\n{PAGE_MARKER.format(n=pno)}\n"
                       + content[off:])
        return content

    def extract(self, source: "Path | bytes", *, pages: Optional[str] = None,
                page_markers: bool = True) -> tuple[str, dict[str, Any]]:
        """Convenience: analyze + render. Returns (markdown, analyzeResult)."""
        result = self.analyze(source, pages=pages)
        return self.to_markdown(result, page_markers=page_markers), result

    @staticmethod
    def describe(result: dict[str, Any]) -> dict[str, Any]:
        """A few numbers worth logging after an extraction."""
        pages = result.get("pages") or []
        return {
            "pages": len(pages),
            "tables": len(result.get("tables") or []),
            "paragraphs": len(result.get("paragraphs") or []),
            "content_chars": len(result.get("content") or ""),
        }


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


# ------------------------------------------------------------------ corpus
def extract_corpus(src_dir: "str | Path", out_dir: "str | Path", *,
                   pattern: str = "*.pdf", force: bool = False,
                   workers: int = 2, pages: Optional[str] = None,
                   page_markers: bool = True,
                   client: Optional[AzureDocIntelligence] = None,
                   on_progress=None) -> dict[str, Any]:
    """Convert every PDF under `src_dir` into Markdown under `out_dir`.

    Writes one `.md` per PDF plus a `.meta.json` sidecar (page count, table
    count, model, api version). Existing outputs are skipped unless `force`,
    so re-running after adding a few PDFs is cheap.

    Keep `workers` low: the free tier throttles aggressively and Azure answers
    429 with a Retry-After that this module honours, but a low concurrency
    avoids the wait entirely.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    src, out = Path(src_dir), Path(out_dir)
    if not src.is_dir():
        raise AzureDIError(f"not a directory: {src}")
    out.mkdir(parents=True, exist_ok=True)
    adi = client or AzureDocIntelligence()

    pdfs = sorted(p for p in src.rglob(pattern) if p.is_file())
    if not pdfs:
        raise AzureDIError(f"no files matching {pattern} under {src}")

    summary = {"total": len(pdfs), "converted": 0, "skipped": 0,
               "failed": 0, "tables": 0, "pages": 0, "errors": []}

    def one(pdf: Path):
        rel = pdf.relative_to(src)
        md_path = (out / rel).with_suffix(".md")
        meta_path = (out / rel).with_suffix(".meta.json")
        if md_path.exists() and not force:
            return ("skipped", pdf, None, None)
        md, result = adi.extract(pdf, pages=pages, page_markers=page_markers)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md, encoding="utf-8")
        meta = {
            "source": str(pdf),
            "model": adi.cfg.model,
            "api_version": adi.cfg.api_version,
            "output_format": adi.cfg.output_format,
            "page_markers": page_markers,
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **AzureDocIntelligence.describe(result),
        }
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return ("converted", pdf, md_path, meta)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, p): p for p in pdfs}
        for i, fut in enumerate(as_completed(futures), 1):
            pdf = futures[fut]
            try:
                status, _, md_path, meta = fut.result()
            except Exception as exc:  # noqa: BLE001
                summary["failed"] += 1
                summary["errors"].append(f"{pdf.name}: {exc}")
                if on_progress:
                    on_progress(i, len(pdfs), pdf, "failed", str(exc))
                continue
            if status == "skipped":
                summary["skipped"] += 1
            else:
                summary["converted"] += 1
                summary["pages"] += meta["pages"]
                summary["tables"] += meta["tables"]
            if on_progress:
                on_progress(i, len(pdfs), pdf, status, md_path)
    return summary


__all__ = ["AzureDIConfig", "AzureDocIntelligence", "AzureDIError",
           "extract_corpus", "DEFAULT_MODEL", "LAYOUT_MODELS", "PAGE_MARKER"]
