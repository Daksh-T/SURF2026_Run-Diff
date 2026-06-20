#!/usr/bin/env bash
# Build the macOS-native Run·Diff.app (WKWebView shell). No Rust, no Chromium — just swiftc
# (from the Xcode Command Line Tools) plus the prebuilt PyInstaller backend and frontend dist.
#
# Prereqs (same artifacts the Electron build consumes):
#   ../backend/dist_backend/rundiff-backend/   (PyInstaller sidecar dir)  -> `bun run build:backend` in ../desktop
#   ../frontend/dist/                          (built frontend)           -> `bun run build:frontend` in ../desktop
#
# Output: release/Run·Diff.app
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBAPP="$(cd "$HERE/.." && pwd)"

APP_NAME="Run·Diff"
BUNDLE_ID="edu.sewanee.surf.rundiff"
VERSION="${VERSION:-0.1.0}"
# Target CPU for the Swift shell. Override with ARCH=x86_64 for an Intel build (CI builds both).
ARCH="${ARCH:-arm64}"

BACKEND_SRC="$WEBAPP/backend/dist_backend/rundiff-backend"
FRONTEND_SRC="$WEBAPP/frontend/dist"
ICON_SRC="$WEBAPP/branding/AppIcon.icns"

OUT="$HERE/release"
APP="$OUT/$APP_NAME.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"

# --- sanity: required inputs present ---
[ -x "$BACKEND_SRC/rundiff-backend" ] || { echo "ERROR: backend sidecar missing: $BACKEND_SRC/rundiff-backend (run build:backend)"; exit 1; }
[ -f "$FRONTEND_SRC/index.html" ]     || { echo "ERROR: frontend dist missing: $FRONTEND_SRC (run build:frontend)"; exit 1; }

echo "==> Clean $APP"
rm -rf "$APP"
mkdir -p "$MACOS" "$RESOURCES"

echo "==> Compile Swift shell ($ARCH)"
swiftc -O -o "$MACOS/$APP_NAME" "$HERE/RunDiff.swift" \
  -framework AppKit -framework WebKit -target "${ARCH}-apple-macos12.0"

echo "==> Bundle backend sidecar + frontend dist into Resources"
cp -R "$BACKEND_SRC" "$RESOURCES/rundiff-backend"
cp -R "$FRONTEND_SRC" "$RESOURCES/dist"
[ -f "$ICON_SRC" ] && cp "$ICON_SRC" "$RESOURCES/AppIcon.icns" || echo "   (no icon found, skipping)"

echo "==> Write Info.plist"
cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.education</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- backend talks plain HTTP to loopback; allow it -->
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

echo "==> Ad-hoc codesign (unsigned distribution)"
codesign --force --deep --sign - "$APP" 2>/dev/null || echo "   (codesign skipped)"

echo "==> Done: $APP"
du -sh "$APP"

# Optional: `./build.sh --dmg` also emits a compressed installer (LZMA ~17 MB vs the 38 MB .app).
if [ "${1:-}" = "--dmg" ]; then
  DMG="$OUT/$APP_NAME.dmg"
  echo "==> Building compressed DMG (ULMO/LZMA)"
  rm -f "$DMG"
  hdiutil create -volname "$APP_NAME" -srcfolder "$APP" -ov -format ULMO "$DMG" >/dev/null
  du -sh "$DMG"
fi
