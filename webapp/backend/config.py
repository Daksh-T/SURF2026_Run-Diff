"""App-wide config: author password (sha256 of the plaintext, never the plaintext) and the
instructor's public sync URL (used by the assignment export/import + attempt-sync flow).

  data/config.json   {"author_password_sha256": str|null, "instructor_url": str|null}

Follows store.py's _read/_write conventions; reuses them directly.
"""
from __future__ import annotations

import store

CONFIG_PATH = store.DATA / "config.json"

DEFAULTS = {"author_password_sha256": None, "instructor_url": None}


def load() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    cfg = store._read(CONFIG_PATH)
    return {**DEFAULTS, **cfg}


def save(cfg: dict) -> dict:
    store._write(CONFIG_PATH, cfg)
    return cfg


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def set(key: str, value) -> dict:
    cfg = load()
    cfg[key] = value
    return save(cfg)
