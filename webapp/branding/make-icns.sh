#!/usr/bin/env bash
# Render branding/icon.svg into AppIcon.icns (all macOS icon sizes). Run after editing icon.svg.
# Both desktop shells consume the result: ../desktop (Electron, via pack.mjs) and ../desktop-macos
# (Swift, via build.sh).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SVG="$HERE/icon.svg"
SET="$HERE/AppIcon.iconset"
ICNS="$HERE/AppIcon.icns"

rm -rf "$SET"; mkdir -p "$SET"

# Each iconset slot: <size> <filename>. macOS expects 1x + @2x for every base size.
render() { # size, outfile
  uv run --with cairosvg python -c "import cairosvg,sys; cairosvg.svg2png(url=sys.argv[1], write_to=sys.argv[2], output_width=int(sys.argv[3]), output_height=int(sys.argv[3]))" "$SVG" "$SET/$2" "$1"
}

render 16   icon_16x16.png
render 32   icon_16x16@2x.png
render 32   icon_32x32.png
render 64   icon_32x32@2x.png
render 128  icon_128x128.png
render 256  icon_128x128@2x.png
render 256  icon_256x256.png
render 512  icon_256x256@2x.png
render 512  icon_512x512.png
render 1024 icon_512x512@2x.png

iconutil -c icns "$SET" -o "$ICNS"
rm -rf "$SET"
echo "wrote $ICNS"
