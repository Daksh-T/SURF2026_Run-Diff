"""File-based store with a hard wall between two kinds of artifact.

  data/sets/<id>.json      INSTRUCTOR-PRIVATE source. Holds each problem's gold_sql,
                           schema, generator source. NEVER served to a student endpoint.
  data/bundles/<id>.json   STUDENT-SAFE published bundle. Holds baked per-seed gold
                           RESULTS, schema, generator source, prompt — but NO gold_sql.
                           Produced by publish.py::publish().

The directory split *is* the security boundary: the student API only ever opens `bundles/`,
and `bake`/`publish` is the one operation that reads `gold_sql`. There is no code path from a
student request to a `sets/` file.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# The data root. Packaged (Electron) builds set TUTOR_DATA_DIR to a writable per-user location
# (app.getPath("userData")/data) so the app never writes inside its own read-only bundle. In dev
# this is unset and we use the repo-relative default. config.py and classes.py both derive their
# paths from store.DATA, so this single switch moves all persisted state.
DATA = Path(os.environ["TUTOR_DATA_DIR"]) if os.environ.get("TUTOR_DATA_DIR") \
    else Path(__file__).resolve().parents[1] / "data"
SETS = DATA / "sets"
BUNDLES = DATA / "bundles"
for d in (SETS, BUNDLES):
    d.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "untitled"


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, indent=2))


# --------------------------------------------------------------------------- #
# instructor-private source sets
# --------------------------------------------------------------------------- #
def new_set(title: str, set_id: str | None = None) -> dict:
    sid = set_id or slugify(title)
    if (SETS / f"{sid}.json").exists():
        n = 2
        while (SETS / f"{sid}-{n}.json").exists():
            n += 1
        sid = f"{sid}-{n}"
    obj = {"id": sid, "title": title, "created": _now(), "updated": _now(),
           "problems": [], "published_at": None}
    _write(SETS / f"{sid}.json", obj)
    return obj


def get_set(set_id: str) -> dict:
    return _read(SETS / f"{set_id}.json")


def save_set(obj: dict) -> dict:
    obj["updated"] = _now()
    _write(SETS / f"{obj['id']}.json", obj)
    return obj


def rename_set(set_id: str, title: str) -> dict:
    """Change only a set's display title. The set id (and its slug-derived filename) is stable —
    classes attach by id, so a rename never breaks an assignment. If a published bundle exists,
    its title is synced too so students see the new name without a re-publish; publish state is
    kept in sync (a pure metadata rename must not flag the set 'edited since last publish')."""
    title = (title or "").strip()
    if not title:
        raise ValueError("title cannot be empty")
    s = get_set(set_id)
    s["title"] = title
    bundle_path = BUNDLES / f"{set_id}.json"
    if bundle_path.exists():
        b = _read(bundle_path)
        b["title"] = title
        _write(bundle_path, b)
        s["updated"] = _now()
        s["published_at"] = s["updated"]
        _write(SETS / f"{s['id']}.json", s)
        return s
    return save_set(s)


def list_sets() -> list[dict]:
    out = []
    for p in sorted(SETS.glob("*.json")):
        s = _read(p)
        out.append({"id": s["id"], "title": s["title"], "created": s["created"],
                    "updated": s["updated"], "n_problems": len(s["problems"]),
                    "published_at": s.get("published_at")})
    return out


def add_problem(set_id: str, problem: dict) -> dict:
    """Append an authored problem (carries gold_sql — instructor-private) to a source set."""
    s = get_set(set_id)
    if any(p["id"] == problem["id"] for p in s["problems"]):
        problem = {**problem, "id": f"{problem['id']}-{len(s['problems'])+1}"}
    s["problems"].append(problem)
    return save_set(s)


def remove_problem(set_id: str, problem_id: str) -> dict:
    """Drop a problem from a source set by id."""
    s = get_set(set_id)
    before = len(s["problems"])
    s["problems"] = [p for p in s["problems"] if p["id"] != problem_id]
    if len(s["problems"]) == before:
        raise ValueError(f"no problem '{problem_id}' in set '{set_id}'")
    return save_set(s)


def replace_problem(set_id: str, problem_id: str, new_problem: dict) -> dict:
    """Replace a problem in place — same position — but keep the original problem's id (a
    re-author produces a freshly-slugified id from the title, which may differ)."""
    s = get_set(set_id)
    for i, p in enumerate(s["problems"]):
        if p["id"] == problem_id:
            s["problems"][i] = {**new_problem, "id": problem_id}
            return save_set(s)
    raise ValueError(f"no problem '{problem_id}' in set '{set_id}'")


def reorder_problems(set_id: str, order: list[str]) -> dict:
    """Reorder problems to match `order` (a permutation of existing problem ids)."""
    s = get_set(set_id)
    by_id = {p["id"]: p for p in s["problems"]}
    if sorted(order) != sorted(by_id):
        raise ValueError("order must be a permutation of the set's problem ids")
    s["problems"] = [by_id[pid] for pid in order]
    return save_set(s)


def remove_set(set_id: str) -> None:
    """Delete a source set and its published bundle (if any). Caller is responsible for
    checking whether any class is attached to this set first — classes and attempt logs are
    never touched here."""
    set_path = SETS / f"{set_id}.json"
    if not set_path.exists():
        raise FileNotFoundError(f"no set '{set_id}'")
    set_path.unlink()
    bundle_path = BUNDLES / f"{set_id}.json"
    if bundle_path.exists():
        bundle_path.unlink()


def update_problem(set_id: str, problem_id: str, fields: dict) -> dict:
    """Patch only the given fields (title/prompt/difficulty) of a problem in place. Gold SQL,
    schema, and generator are not editable here — re-author instead."""
    s = get_set(set_id)
    for p in s["problems"]:
        if p["id"] == problem_id:
            p.update(fields)
            return save_set(s)
    raise ValueError(f"no problem '{problem_id}' in set '{set_id}'")


# --------------------------------------------------------------------------- #
# student-safe published bundles
# --------------------------------------------------------------------------- #
def save_bundle(bundle: dict) -> dict:
    _write(BUNDLES / f"{bundle['id']}.json", bundle)
    return bundle


def get_bundle(bundle_id: str) -> dict:
    return _read(BUNDLES / f"{bundle_id}.json")


def list_bundles() -> list[dict]:
    out = []
    for p in sorted(BUNDLES.glob("*.json")):
        b = _read(p)
        out.append({"id": b["id"], "title": b["title"],
                    "published_at": b["published_at"],
                    "n_problems": len(b["problems"])})
    return out


# --------------------------------------------------------------------------- #
# guardrail: assert a bundle is free of gold SQL before it is ever written/served
# --------------------------------------------------------------------------- #
def assert_student_safe(bundle: dict) -> None:
    """Defense in depth: a published bundle must not contain a `gold_sql` field anywhere.
    Raises if it does — so a refactor can never silently start shipping the answer."""
    def walk(o, path="bundle"):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "gold_sql":
                    raise ValueError(f"student-safe bundle contains gold_sql at {path}")
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
    walk(bundle)
