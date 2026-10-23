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

# node_modules the test runner needs (playwright + axe are devDependencies,
# so don't --omit=dev); drop the build-only packages afterwards.
npm ci --ignore-scripts --workspaces=false
cp -R node_modules "$STAGE/argus/_internal/node_modules"
rm -rf "$STAGE/argus/_internal/node_modules"/{typescript,@types,@tauri-apps,.bin}

# Node runtime: official self-contained build (Homebrew's node links libnode.dylib).
NODE_VERSION="${NODE_VERSION:-v22.22.0}"
ARCH="$(uname -m | sed 's/x86_64/x64/')"
TARBALL="node-$NODE_VERSION-darwin-$ARCH.tar.gz"
DL="$(mktemp -d)"
curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/$TARBALL" -o "$DL/$TARBALL"
curl -fsSL "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt" -o "$DL/SHASUMS256.txt"
(cd "$DL" && grep " $TARBALL\$" SHASUMS256.txt | shasum -a 256 -c -)
mkdir -p "$STAGE/node/bin"
tar -xzf "$DL/$TARBALL" -C "$DL"
cp "$DL/node-$NODE_VERSION-darwin-$ARCH/bin/node" "$STAGE/node/bin/node"
rm -rf "$DL"

chmod -R u+w "$STAGE"
echo "staged: $(du -sh "$STAGE" | cut -d$'\t' -f1)"
