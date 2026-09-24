#!/usr/bin/env bash
# Build the `superindex` executable on macOS (same flow as build_windows.ps1).
#
#   bash packaging/build_macos.sh            # onedir (default)
#   SUPERINDEX_ONEFILE=1 bash packaging/build_macos.sh
#   PYTHON=3.11 bash packaging/build_macos.sh     # uv --python (version or path)
#
# Needs uv (https://docs.astral.sh/uv/); it downloads Python 3.12 (.python-version)
# if missing. Output: dist/superindex/ and dist/superindex-macos-<arch>.zip
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIST="$ROOT/dist"
APP="$DIST/superindex"

# ── 1. uv ──────────────────────────────────────────────────────────────────
command -v uv >/dev/null 2>&1 || {
  echo "error: uv not found; install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2; exit 1; }
# A separate env with only runtime + build deps (no dev/pdf groups), exactly as uv.lock.
export UV_PROJECT_ENVIRONMENT="$ROOT/build/venv-bundle"
PY_ARGS=()
if [[ -n "${PYTHON:-}" ]]; then PY_ARGS=(--python "$PYTHON"); fi

# ── 2. locked dependencies ─────────────────────────────────────────────────
uv sync --locked --no-default-groups --group build ${PY_ARGS[@]+"${PY_ARGS[@]}"}
echo "==> python: $(uv run --no-sync python -V)"

# ── 3. PyInstaller ─────────────────────────────────────────────────────────
rm -rf "$APP" "$DIST/superindex-onefile" "$ROOT/build/superindex"
uv run --no-sync pyinstaller packaging/superindex.spec \
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
