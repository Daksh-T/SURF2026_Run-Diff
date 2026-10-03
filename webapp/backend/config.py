"""Persist the author password hash, class sync URL, and optional Groq API key locally.

Use atomic replacement and owner-only permissions when saving credentials.
"""
from __future__ import annotations

import store
import json
import os
import tempfile

CONFIG_PATH = store.DATA / "config.json"

DEFAULTS = {"author_password_sha256": None, "instructor_url": None}


def load() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    cfg = store._read(CONFIG_PATH)
    return {**DEFAULTS, **cfg}


def save(cfg: dict) -> dict:
    # Create the replacement with owner-only permissions before writing the API key.
    with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG_PATH.parent, delete=False) as output:
        temporary = output.name
        try:
            json.dump(cfg, output, indent=2)
            output.close()
            os.replace(temporary, CONFIG_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return cfg


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def set(key: str, value) -> dict:
    cfg = load()
    cfg[key] = value
    return save(cfg)
