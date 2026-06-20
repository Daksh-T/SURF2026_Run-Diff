"""Serve the built Vite frontend from the FastAPI process, so the whole app is one process
(this is what the Electron shell points its window at).

Resolution order for the built `dist/`:
  1. TUTOR_FRONTEND_DIST env var (set by the packaged Electron app, points at the bundled dist)
  2. ../frontend/dist relative to this file (the dev/local layout)

If neither exists, this is a no-op: dev mode (vite on :5180 proxying to :8077, or any API-only
use) is completely unaffected. /api/* always takes precedence because this is mounted LAST and
the SPA fallback explicitly refuses to shadow /api paths.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def _dist_dir() -> Path | None:
    env = os.environ.get("TUTOR_FRONTEND_DIST")
    if env:
        p = Path(env)
        if (p / "index.html").exists():
            return p
    local = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if (local / "index.html").exists():
        return local
    return None


def mount_frontend(app: FastAPI) -> bool:
    """Mount static asset serving + SPA fallback if a built frontend exists. Returns whether
    it was mounted. MUST be called after all /api routes are registered."""
    dist = _dist_dir()
    if dist is None:
        return False

    index = dist / "index.html"

    # Hashed assets under /assets/* served straight from disk.
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    # The SPA shell must never be cached: it references the content-hashed asset filenames, so a
    # stale cached index.html pins the whole app to an old build (old CSS/JS) even after a rebuild
    # — the bug that made fixes appear to "come back". Assets under /assets are content-hashed and
    # safe to cache forever; only the shell needs revalidation.
    NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}

    # SPA fallback: any non-/api GET returns index.html so client-side routing (/setup, /author,
    # ...) works on a hard refresh / deep link. Static files at the root (favicon, etc.) are
    # served if present, otherwise index.html.
    @app.get("/{full_path:path}")
    def spa_fallback(full_path: str, request: Request):
        if full_path.startswith("api/"):
            raise HTTPException(404, "not found")
        candidate = dist / full_path
        if full_path and candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(index), headers=NO_CACHE)

    return True
