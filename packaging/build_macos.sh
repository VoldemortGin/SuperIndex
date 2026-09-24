#!/usr/bin/env bash
# Build the `superindex` executable on macOS (same flow as build_windows.ps1).
#
#   bash packaging/build_macos.sh            # onedir (default)
#   SUPERINDEX_ONEFILE=1 bash packaging/build_macos.sh
#   PYTHON=/path/to/python3.12 bash packaging/build_macos.sh
#
# Output: dist/superindex/ and dist/superindex-macos-<arch>.zip
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/build/venv-bundle"
DIST="$ROOT/dist"
APP="$DIST/superindex"

# ── 1. Python 3.11 / 3.12 ──────────────────────────────────────────────────
PY="${PYTHON:-}"
if [[ -z "$PY" ]]; then
  for cand in python3.12 python3.11 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver="$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
      if [[ "$ver" == "3.11" || "$ver" == "3.12" ]]; then PY="$(command -v "$cand")"; break; fi
    fi
  done
fi
[[ -n "$PY" ]] || { echo "error: need Python 3.11 or 3.12 (set PYTHON=...)" >&2; exit 1; }
echo "==> python: $PY ($("$PY" -V))"

# ── 2. venv + locked dependencies ──────────────────────────────────────────
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r packaging/requirements-bundle.txt

# ── 3. PyInstaller ─────────────────────────────────────────────────────────
rm -rf "$APP" "$DIST/superindex-onefile" "$ROOT/build/superindex"
"$VENV/bin/python" -m PyInstaller packaging/superindex.spec \
  --noconfirm --clean --distpath "$DIST" --workpath "$ROOT/build"

if [[ "${SUPERINDEX_ONEFILE:-}" == "1" ]]; then
  EXE="$DIST/superindex"
  mkdir -p "$DIST/superindex-onefile" && mv "$EXE" "$DIST/superindex-onefile/superindex"
  APP="$DIST/superindex-onefile"
  EXE="$APP/superindex"
else
  EXE="$APP/superindex"
fi

# ── 4. smoke test: fresh cwd, no network, no LLM ──────────────────────────
SMOKE="$(mktemp -d)"
trap 'rm -rf "$SMOKE"' EXIT
cp samples/aia_ar2021_excerpt.md "$SMOKE/"
(
  cd "$SMOKE"
  export HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9
  export http_proxy=$HTTP_PROXY https_proxy=$HTTPS_PROXY
  "$EXE" --help >/dev/null
  "$EXE" index aia_ar2021_excerpt.md --no-summary --store "$SMOKE/store"
  test -f "$SMOKE/store/manifest.json"
)
echo "==> smoke test passed"

# ── 5. zip: app folder + .env.example + README ─────────────────────────────
cp .env.example "$APP/.env.example"
cp packaging/README.md "$APP/README.md"
ZIP="$DIST/$(basename "$APP" | sed "s/^superindex/superindex-macos-$(uname -m)/").zip"
rm -f "$ZIP"
(cd "$DIST" && ditto -c -k --keepParent "$(basename "$APP")" "$ZIP")
echo "==> $(du -sh "$APP" | cut -f1)  $APP"
echo "==> $(du -sh "$ZIP" | cut -f1)  $ZIP"
