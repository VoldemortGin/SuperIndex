"""SuperIndex: PageIndex question answering over Azure DI Markdown.

`python -m superindex index|ask|search|serve|batch` — see `superindex.cli`.
"""
import importlib.util
import sys
from pathlib import Path

# Source checkout: use the vendored PageIndex/ when pageindex is not installed,
# so `pip install -e PageIndex` is optional.
_VENDORED = Path(__file__).resolve().parent.parent / "PageIndex"
if _VENDORED.is_dir() and importlib.util.find_spec("pageindex") is None:
    sys.path.append(str(_VENDORED))
