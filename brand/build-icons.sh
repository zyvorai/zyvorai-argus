#!/usr/bin/env bash
# Copyright 2026 Zyvor AI Labs · https://zyvor.dev
# SPDX-License-Identifier: LicenseRef-Zyvor-Production-1.0
#
# Rasterize the brand SVGs into the PNG/ICNS files the app and site ship.
# Needs rsvg-convert (`brew install librsvg`) and, for the .icns, macOS iconutil.
# Run `python3 brand/panoptes.py` first if you changed the mark itself.
set -euo pipefail
cd "$(dirname "$0")/.."

ICON=brand/argus-icon.svg          # full detail, >= 64px
SMALL=brand/argus-icon-small.svg   # thicker, for <= 32px
TEMPLATE=brand/argus-mark-template.svg
ICONS=desktop/src-tauri/icons

png() { rsvg-convert -w "$2" -h "$2" "$1" -o "$3"; }

# Tauri bundle icons
png "$ICON"  1024 "$ICONS/icon-source.png"
png "$ICON"   512 "$ICONS/icon.png"
png "$ICON"   256 "$ICONS/128x128@2x.png"
png "$ICON"   128 "$ICONS/128x128.png"
png "$ICON"    64 "$ICONS/64x64.png"
png "$SMALL"   32 "$ICONS/32x32.png"

# macOS icon (.icns): the small variant below 64px, the full one above
if command -v iconutil >/dev/null 2>&1; then
  SET="$(mktemp -d)/argus.iconset"; mkdir -p "$SET"
  png "$SMALL"   16 "$SET/icon_16x16.png";      png "$SMALL"   32 "$SET/icon_16x16@2x.png"
  png "$SMALL"   32 "$SET/icon_32x32.png";      png "$ICON"    64 "$SET/icon_32x32@2x.png"
  png "$ICON"   128 "$SET/icon_128x128.png";    png "$ICON"   256 "$SET/icon_128x128@2x.png"
  png "$ICON"   256 "$SET/icon_256x256.png";    png "$ICON"   512 "$SET/icon_256x256@2x.png"
  png "$ICON"   512 "$SET/icon_512x512.png";    png "$ICON"  1024 "$SET/icon_512x512@2x.png"
  iconutil -c icns "$SET" -o "$ICONS/icon.icns"
else
  echo "iconutil not found: skipped icon.icns" >&2
fi

# Menu-bar template icon (black on transparent; macOS tints it)
png "$TEMPLATE" 44 "$ICONS/tray-template.png"
png "$TEMPLATE" 88 "$ICONS/tray-template@2x.png"

# Site
cp "$SMALL" site/favicon.svg
png "$ICON" 180 site/apple-touch-icon.png
rsvg-convert -w 1200 brand/share-card.svg -o site/assets/argus-share-card.png
echo "icons rebuilt"
