#!/usr/bin/env bash
# Stage the frozen backend, Node and Playwright into desktop/src-tauri/stage/
# for `tauri build`. Chromium is NOT shipped; the app downloads it on first run.
# Usage: scripts/desktop-stage.sh   (run from anywhere; needs .venv with pyinstaller)
set -euo pipefail
cd "$(dirname "$0")/.."
STAGE=desktop/src-tauri/stage
PY="${PYTHON:-.venv/bin/python}"
rm -rf "$STAGE" && mkdir -p "$STAGE"

"$PY" -m pip install -q pyinstaller
"$PY" -m PyInstaller desktop/build/argus.spec --noconfirm \
  --distpath "$STAGE" --workpath "${TMPDIR:-/tmp}/argus-pyi"

# Production node_modules (playwright, axe-core) inside the bundle root.
npm ci --omit=dev --ignore-scripts --workspaces=false
cp -R node_modules "$STAGE/argus/_internal/node_modules"

# Node runtime: copy the one on PATH (arch matches the build host).
mkdir -p "$STAGE/node/bin"
cp "$(command -v node)" "$STAGE/node/bin/node"

echo "staged: $(du -sh "$STAGE" | cut -d$'\t' -f1)"
