# PyInstaller spec for the `superindex` executable (Windows and macOS).
#
#   pyinstaller packaging/superindex.spec --noconfirm --distpath dist --workpath build
#
# Default is onedir: dist/superindex/superindex(.exe) + dist/superindex/_internal/.
# Set SUPERINDEX_ONEFILE=1 for a single self-extracting file dist/superindex(.exe).
# Run it from the repository root in an env from `uv sync --group build` (see build_*.sh/.ps1).
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).resolve().parent
PAGEINDEX = ROOT / "PageIndex"
ONEFILE = os.environ.get("SUPERINDEX_ONEFILE", "").strip() == "1"

# Only the Markdown path ships: PDF parsing (pypdfium2 / pageindex.flash),
# image highlighting (Pillow), litellm's proxy server and its optional Rust OCR
# bridge (loaded behind try/except ImportError) are never reached.
PDF_ONLY = ("pageindex.flash", "pageindex.imaging")
LITELLM_UNUSED = ("litellm.proxy", "litellm.rust_bridge._native")


def _keep_pageindex(name: str) -> bool:
    return not name.startswith(PDF_ONLY)


def _keep_litellm(name: str) -> bool:
    return not name.startswith(LITELLM_UNUSED)


datas = [
    (str(ROOT / "webapp" / "static"), "webapp/static"),
    (str(PAGEINDEX / "pageindex" / "config.yaml"), "pageindex"),
]
# Tokenizer files (tiktoken cl100k/o200k encodings, keyed by their URL hash —
# litellm points TIKTOKEN_CACHE_DIR at them, so no download) and the offline
# model-cost map. The proxy's UI/config files and the Rust bridge are dead weight.
datas += collect_data_files(
    "litellm",
    excludes=["proxy/**", "rust_bridge/**"],
)
# openai-agents reads its prompt templates (agents/sandbox/**/*.md) at import.
datas += collect_data_files("agents")
# Packages that read their own version through importlib.metadata.
for dist in ("litellm", "openai", "openai-agents", "mcp", "tiktoken", "tokenizers",
             "huggingface_hub", "pydantic", "pydantic-settings"):
    datas += copy_metadata(dist)

hiddenimports = [
    "tiktoken_ext",
    "tiktoken_ext.openai_public",
    "agents.extensions.models.litellm_model",
    "webapp.server",
    "superindex.cli",
    "superindex.md_ingest",
    "superindex.bm25",
    "superindex.agent_search",
    "superindex.calc",
    "superindex.prefetch",
    "superindex.batch",
    "nav.build",
    "nav.store",
    "nav.llm",
]
# pageindex/__init__ and litellm load most modules lazily (module __getattr__,
# string imports), which static analysis cannot follow.
hiddenimports += collect_submodules("pageindex", filter=_keep_pageindex)
hiddenimports += collect_submodules("litellm", filter=_keep_litellm)
if sys.platform == "win32":
    # mcp.os.win32.utilities imports these unguarded at module level on Windows.
    hiddenimports += ["pywintypes", "win32api", "win32con", "win32job"]

excludes = [
    "pypdfium2", "pypdfium2_raw", *PDF_ONLY, "PIL", "litellm.rust_bridge._native",
    "tkinter", "_tkinter", "pytest", "_pytest", "IPython", "matplotlib",
    "numpy", "pandas", "torch", "onnxruntime", "fastembed", "fastapi",
]

a = Analysis(
    [str(ROOT / "superindex" / "__main__.py")],
    pathex=[str(ROOT), str(PAGEINDEX)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="superindex",
        console=True,
        upx=False,  # UPX-packed binaries are a common antivirus false positive
        strip=False,
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="superindex",
        console=True,
        upx=False,
        strip=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="superindex", upx=False, strip=False)
