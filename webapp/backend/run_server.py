"""Entry point for the packaged backend sidecar. Starts uvicorn serving the FastAPI `app`.

Honors:
  HOST / PORT             bind address (default 127.0.0.1:8077)
  TUTOR_DATA_DIR          writable data root (set by Electron to userData/data when packaged)
  TUTOR_FRONTEND_DIST     built frontend dir (set by Electron to the bundled dist)

Run directly in dev (`uv run python run_server.py`) or as the PyInstaller binary.
"""
from __future__ import annotations

import os

import uvicorn

from app import app

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8077"))
    uvicorn.run(app, host=host, port=port, log_level="info")
