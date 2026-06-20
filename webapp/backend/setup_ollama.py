"""First-run Ollama setup: detect Ollama, report model presence, drive the model pull.

These routes are NOT author-gated — students need them too. The hint model defaults to the
local qwen2.5-coder:7b (see populator/model.py::REGISTRY); we resolve the friendly name from
TUTOR_HINT_MODEL to its concrete Ollama tag and report/pull that tag.

Nothing here is required for the app to run: hints fall back to deterministic offline templates
when Ollama is absent. Setup is strongly recommended, not blocking.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")


# --------------------------------------------------------------------------- #
# resolve the friendly hint-model name -> concrete ollama tag
# --------------------------------------------------------------------------- #
def _model_tag() -> str:
    """Resolve TUTOR_HINT_MODEL (a friendly name like 'qwen7b') to its Ollama tag
    (e.g. 'qwen2.5-coder:7b') via populator/model.py::REGISTRY. Falls back gracefully:
    if the registry can't be imported, treat the env value itself as the tag."""
    friendly = os.environ.get("TUTOR_HINT_MODEL", "qwen7b")
    try:
        root = Path(__file__).resolve().parents[2]
        pop = str(root / "populator")
        if pop not in sys.path:
            sys.path.insert(0, pop)
        import model as _model  # populator/model.py

        entry = _model.REGISTRY.get(friendly)
        if entry and entry[0] == "ollama":
            return entry[1]
    except Exception:  # noqa: BLE001 — never let resolution failure break the route
        pass
    # if it already looks like a tag (has a ':') use it; else best-effort default
    if ":" in friendly:
        return friendly
    return "qwen2.5-coder:7b"


MODEL_TAG = _model_tag()


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _ollama_tags(timeout: float = 1.5) -> list[str] | None:
    """Return the list of installed model tags, or None if Ollama isn't reachable."""
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return [m.get("name", "") for m in data.get("models", [])]
    except Exception:  # noqa: BLE001 — connection refused / timeout / bad json all mean "not running"
        return None


def _ollama_installed() -> bool | None:
    """Best-effort: True if the binary or the macOS app is present, else None (undeterminable)."""
    if shutil.which("ollama"):
        return True
    if Path("/Applications/Ollama.app").exists():
        return True
    return None


def _tag_present(tags: list[str], wanted: str) -> bool:
    """Match the wanted tag against installed tags, tolerating the implicit ':latest' suffix
    Ollama adds (e.g. wanted 'qwen2.5-coder:7b' should match an installed 'qwen2.5-coder:7b')."""
    wanted_full = wanted if ":" in wanted else f"{wanted}:latest"
    for t in tags:
        t_full = t if ":" in t else f"{t}:latest"
        if t_full == wanted_full:
            return True
    return False


# --------------------------------------------------------------------------- #
# pull job tracking (mirrors app.py's _JOBS pattern, kept separate + thread-safe)
# --------------------------------------------------------------------------- #
_PULL_JOBS: dict[str, dict[str, Any]] = {}
_PULL_LOCK = threading.Lock()


def _set_job(job_id: str, **fields: Any) -> None:
    with _PULL_LOCK:
        job = _PULL_JOBS.setdefault(
            job_id,
            {"status": "starting", "completed": 0, "total": 0, "done": False, "error": None},
        )
        job.update(fields)


def _run_pull(job_id: str, tag: str) -> None:
    """POST to ollama /api/pull and consume the streaming NDJSON response, tracking progress."""
    body = json.dumps({"name": tag}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/pull",
        data=body,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=None) as resp:
            for raw in resp:  # NDJSON: one json object per line
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if evt.get("error"):
                    _set_job(job_id, status="error", error=str(evt["error"]), done=True)
                    return
                fields: dict[str, Any] = {}
                if "status" in evt:
                    fields["status"] = evt["status"]
                if "completed" in evt:
                    fields["completed"] = int(evt["completed"])
                if "total" in evt:
                    fields["total"] = int(evt["total"])
                if fields:
                    _set_job(job_id, **fields)
        _set_job(job_id, status="success", done=True)
    except urllib.error.URLError as e:
        _set_job(job_id, status="error", error=f"could not reach Ollama: {e}", done=True)
    except Exception as e:  # noqa: BLE001 — surface any pull crash to the UI
        _set_job(job_id, status="error", error=repr(e), done=True)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
router = APIRouter(prefix="/api/setup")


@router.get("/status")
def setup_status():
    tags = _ollama_tags()
    running = tags is not None
    return {
        "ollama_running": running,
        "model_present": bool(running and _tag_present(tags, MODEL_TAG)),
        "model_tag": MODEL_TAG,
        "ollama_installed": _ollama_installed(),
        "platform": sys.platform,
    }


@router.post("/pull")
def setup_pull():
    if _ollama_tags() is None:
        raise HTTPException(400, "Ollama is not running; start it before downloading the model")
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id)  # seed initial record
    threading.Thread(target=_run_pull, args=(job_id, MODEL_TAG), daemon=True).start()
    return {"job_id": job_id}


@router.get("/pull/{job_id}")
def setup_pull_status(job_id: str):
    with _PULL_LOCK:
        job = _PULL_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"no pull job '{job_id}'")
    return dict(job)
