#!/usr/bin/env bash
# Stage the prebuilt artifacts the Tauri bundle ships as resources.
#
# Tauri's single-file `externalBin` can't represent a PyInstaller *onedir* bundle (an executable
# plus an `_internal/` tree), so the backend rides as a bundled resource directory and is launched
# with std::process::Command from src/main.rs — the same resource-dir pattern the Electron and
# Swift shells use. This script copies the two prebuilt inputs into src-tauri/resources/ where
# tauri.conf.json picks them up. Both copied dirs are gitignored.
#
# Prereqs (same artifacts the Electron / Swift builds consume):
#   ../backend/dist_backend/rundiff-backend/   (PyInstaller sidecar dir)
#   ../frontend/dist/                          (built frontend)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEBAPP="$(cd "$HERE/.." && pwd)"

BACKEND_SRC="$WEBAPP/backend/dist_backend/rundiff-backend"
FRONTEND_SRC="$WEBAPP/frontend/dist"
RES="$HERE/src-tauri/resources"

[ -e "$BACKEND_SRC/rundiff-backend" ] || [ -e "$BACKEND_SRC/rundiff-backend.exe" ] || {
  echo "ERROR: backend sidecar missing under $BACKEND_SRC (build it first)"; exit 1; }
[ -f "$FRONTEND_SRC/index.html" ] || {
  echo "ERROR: frontend dist missing: $FRONTEND_SRC (build it first)"; exit 1; }

echo "==> Staging resources into $RES"
rm -rf "$RES"
mkdir -p "$RES"
cp -R "$BACKEND_SRC" "$RES/rundiff-backend"
cp -R "$FRONTEND_SRC" "$RES/frontend-dist"
echo "==> Done."
