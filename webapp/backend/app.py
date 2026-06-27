"""FastAPI surface for the SURF SQL tutor web app.

Two role-scoped route groups that map onto the security boundary in store.py:

  /api/student/*     reads ONLY published bundles (no gold SQL exists there). Grades,
                     diffs, and hints — all off baked gold results.
  /api/instructor/*  reads/writes private source sets (gold SQL present), runs the
                     authoring job (Groq), and publishes (bakes + strips gold).

The hint model defaults to the local qwen2.5-coder:7b (validated in model_probe/); the
authoring model defaults to Groq llama-3.3-70b (no student data, so cloud is fine).
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import socket
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import authoring
import classes
import config
import publish
import seal
import setup_ollama
import static
import store
import tutor_core as tc

HINT_MODEL = os.environ.get("TUTOR_HINT_MODEL", "qwen7b")       # local Ollama
AUTHOR_MODEL = os.environ.get("TUTOR_AUTHOR_MODEL", "groq")     # cloud, no student data

app = FastAPI(title="SURF SQL Tutor")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _bundle_problem(set_id: str, problem_id: str) -> dict:
    try:
        bundle = store.get_bundle(set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no published set '{set_id}'")
    for p in bundle["problems"]:
        if p["id"] == problem_id:
            return p
    raise HTTPException(404, f"no problem '{problem_id}' in set '{set_id}'")


def _redacted(p: dict) -> tc.RedactedProblem:
    return tc.RedactedProblem(
        id=p["id"], title=p["title"], difficulty=p.get("difficulty", "medium"),
        prompt=p["prompt"], schema=p["schema"], target_clauses=p.get("target_clauses", []))


def _forward_attempt(class_id: str, rec: dict) -> None:
    """If this class has a live instructor_url configured, forward the just-logged attempt
    in a daemon thread. Best-effort: never affects the student-facing response."""
    try:
        c = classes.get_class(class_id)
    except Exception:  # noqa: BLE001
        return
    url = c.get("instructor_url")
    if not url:
        return

    def _send():
        try:
            requests.post(
                f"{url.rstrip('/')}/api/sync/attempts",
                json={"class_id": class_id, "passphrase": c["passphrase"], "attempts": [rec]},
                timeout=3,
            )
        except Exception:  # noqa: BLE001 — forwarding must never raise
            pass

    threading.Thread(target=_send, daemon=True).start()


def _hosting_enabled() -> bool:
    """True only when the instructor has switched on 'Host on this network' under
    Author → Classes (which persists instructor_url in config). The LAN-facing student
    endpoints (fetch-assignment + attempt ingest) refuse to serve unless this is on, so
    turning hosting off truly stops other devices from connecting or pushing — the server
    process can keep running for the local instructor without acting as a class server."""
    return bool(config.load().get("instructor_url"))


def _require_hosting() -> None:
    if not _hosting_enabled():
        raise HTTPException(
            403,
            "this machine is not hosting a class server — the instructor must turn on "
            "'Host on this network' under Author → Classes",
        )


# =========================================================================== #
# AUTH — optional author password gating /api/instructor/*
# =========================================================================== #
def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def require_author(x_author_key: str | None = Header(default=None)) -> None:
    """Gate dependency for every /api/instructor/* route. If no password is set, the
    instructor surface is open (single-user local setup). Once a password is set, every
    request must carry X-Author-Key matching it."""
    expected = config.load().get("author_password_sha256")
    if not expected:
        return
    if not x_author_key or _sha256(x_author_key) != expected:
        raise HTTPException(401, "author password required")


# Loopback addresses the local app window (and the dev proxy) always originate from.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
# Opt-in escape hatch for the undocumented "headless backend + remote-browser admin" setup.
_ALLOW_REMOTE_ADMIN = os.environ.get("RUNDIFF_ALLOW_REMOTE_ADMIN", "").strip().lower() in (
    "1", "true", "yes", "on")


def require_local(request: Request) -> None:
    """Local-only gate for instructor/admin endpoints. The Author UI (and the dev proxy) only
    ever reach these over loopback — students and cross-machine sync use /api/student/* and
    /api/sync/* instead — so binding the backend to 0.0.0.0 for class hosting must NOT expose
    the admin surface to other devices on the LAN. Without this, a class server with no author
    password set would let any LAN peer drive authoring/publishing/class management.

    Set RUNDIFF_ALLOW_REMOTE_ADMIN=1 to deliberately allow LAN admin (the rare self-hosted,
    headless-backend case where the instructor drives the UI from another machine's browser)."""
    if _ALLOW_REMOTE_ADMIN:
        return
    client = request.client.host if request.client else None
    if client not in _LOOPBACK_HOSTS:
        raise HTTPException(
            403,
            "the instructor/admin API is local-only on this machine; set "
            "RUNDIFF_ALLOW_REMOTE_ADMIN=1 to allow access from other devices on the network",
        )


@app.get("/api/auth/status")
def auth_status():
    return {"password_set": bool(config.load().get("author_password_sha256"))}


class SetPasswordReq(BaseModel):
    password: str
    current: str | None = None


@app.post("/api/auth/set", dependencies=[Depends(require_local)])
def auth_set(req: SetPasswordReq):
    if not req.password:
        raise HTTPException(400, "password must not be empty")
    cfg = config.load()
    existing = cfg.get("author_password_sha256")
    if existing:
        if not req.current or _sha256(req.current) != existing:
            raise HTTPException(401, "current password is incorrect")
    config.set("author_password_sha256", _sha256(req.password))
    return {"ok": True}


class ClearPasswordReq(BaseModel):
    current: str


@app.post("/api/auth/clear", dependencies=[Depends(require_local)])
def auth_clear(req: ClearPasswordReq):
    """Remove the author password entirely (authoring goes open). Requires the current
    password so a locked-out visitor can't simply clear it."""
    cfg = config.load()
    existing = cfg.get("author_password_sha256")
    if existing and (not req.current or _sha256(req.current) != existing):
        raise HTTPException(401, "current password is incorrect")
    config.set("author_password_sha256", None)
    return {"ok": True}


class CheckPasswordReq(BaseModel):
    password: str


@app.post("/api/auth/check")
def auth_check(req: CheckPasswordReq):
    expected = config.load().get("author_password_sha256")
    return {"ok": (not expected) or _sha256(req.password) == expected}


instructor_router = APIRouter(
    prefix="/api/instructor", dependencies=[Depends(require_local), Depends(require_author)])


# =========================================================================== #
# STUDENT  — published bundles only
# =========================================================================== #
@app.get("/api/student/sets")
def student_sets(class_id: str | None = Query(default=None)):
    """Students only ever see sets they're assigned via a class. Without a class_id, the
    listing is empty — there is no "browse all sets" surface for students."""
    if class_id is None:
        return []
    try:
        c = classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    set_ids = c.get("set_ids") or ([c["set_id"]] if c.get("set_id") else [])
    # preserve the class's assigned order, not the on-disk bundle order
    by_id = {b["id"]: b for b in store.list_bundles()}
    return [by_id[sid] for sid in set_ids if sid in by_id]


@app.get("/api/student/sets/{set_id}")
def student_set(set_id: str, class_id: str | None = Query(default=None)):
    if class_id is not None:
        try:
            c = classes.get_class(class_id)
        except FileNotFoundError:
            raise HTTPException(403, "join a class to access this set")
        set_ids = c.get("set_ids") or ([c["set_id"]] if c.get("set_id") else [])
        if set_id not in set_ids:
            raise HTTPException(403, "join a class to access this set")
    try:
        b = store.get_bundle(set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no published set '{set_id}'")
    # thin, student-facing view: the prompt + the tables to read. No baked results, no generator.
    return {
        "id": b["id"], "title": b["title"], "published_at": b["published_at"],
        "problems": [{
            # no target_clauses: naming the clauses (e.g. "GROUP BY, SUM") above the editor
            # is a free hint that spoils the hint ladder. Stays in the bundle on disk /
            # instructor views; the hint path reads the bundle directly so hints are unaffected.
            "id": p["id"], "title": p["title"], "difficulty": p.get("difficulty", "medium"),
            "prompt": p["prompt"], "schema": p["schema"],
            # the grading mode, so the UI can switch the editor/diff view. Defaults to "select".
            "kind": p.get("kind", "select"),
        } for p in b["problems"]],
    }


class GradeReq(BaseModel):
    set_id: str
    problem_id: str
    sql: str
    class_id: str | None = None
    student: str | None = None


def _session_state(class_id: str | None) -> str:
    """Live session control for a class ('running'|'paused'|'ended'), or 'running' when there's
    no class (free practice) or the record is missing. For network students this reflects the
    last state cached locally by class-status polling."""
    if not class_id:
        return "running"
    try:
        return classes.get_class(class_id).get("session_state", "running")
    except FileNotFoundError:
        return "running"


_PAUSED_MSG = "The instructor paused the test — you'll be able to submit again when they resume."
_ENDED_HINT_MSG = "The test has ended — hints are closed. You can still submit the question you're on."
_REMOVED_MSG = ("You're no longer on the roster for this classroom. "
                "Ask your instructor to add you back if this is a mistake.")


def _assert_participant(class_id: str | None, student: str | None) -> None:
    """Hard server-side roster gate for in-session student actions. A student removed from a
    roster-mode class (or one whose class flipped open→roster) is refused here — not merely
    hidden in the UI — so they can't keep grading or pulling hints, and their attempts stop
    being logged. No-op for free practice (no class) and for a class this device doesn't hold
    on disk: a networked student grades against a possibly-stale local copy, and the host stays
    authoritative — it re-checks the roster when class-status is proxied and when attempts are
    ingested."""
    if not class_id or not student:
        return
    try:
        c = classes.get_class(class_id)
    except FileNotFoundError:
        return
    if not classes.on_roster(c, student):
        raise HTTPException(403, _REMOVED_MSG)


@app.post("/api/student/grade")
def student_grade(req: GradeReq):
    _assert_participant(req.class_id, req.student)
    if req.class_id and _session_state(req.class_id) == "paused":
        raise HTTPException(423, _PAUSED_MSG)
    p = _bundle_problem(req.set_id, req.problem_id)
    gr = tc.grade_problem(p, req.sql)
    n_passed = sum(1 for s in gr.per_seed if s.ok)
    category = tc.category_for(p, gr)
    if req.class_id and req.student:
        try:
            rec = classes.append_attempt(req.class_id, {
                "student": req.student, "problem_id": p["id"], "set_id": req.set_id,
                "kind": "grade", "correct": gr.correct, "category": category,
                "n_passed": n_passed, "n_seeds": gr.n_seeds, "hint_level": None,
                "sql": req.sql,
            })
            _forward_attempt(req.class_id, rec)
        except Exception:  # noqa: BLE001 — logging must never break grading
            pass
    return {
        "correct": gr.correct,
        "n_seeds": gr.n_seeds,
        "n_passed": n_passed,
        "diff": tc.diff_payload_for(p, gr),
        "category": category,
        # the student's own output on seed #1 — never the gold rows.
        "student_result": tc.student_result_for(p, req.sql),
    }


class HintReq(BaseModel):
    set_id: str
    problem_id: str
    sql: str
    level: int = 1
    class_id: str | None = None
    student: str | None = None


@app.post("/api/student/hint")
def student_hint(req: HintReq):
    # Only the language rungs (L1 conceptual, L2 name-the-clause) are model-generated. L3 is the
    # deterministic diff evidence, rendered client-side from the grade response — never a
    # model-drawn query skeleton (which leaks the answer's shape by construction). So the model
    # is never asked for L3 here. L3 still posts here (with no model call) purely so the request
    # is logged to the attempt stream and surfaces in Insights like L1/L2.
    if req.level not in (1, 2, 3):
        raise HTTPException(400, "level must be 1, 2 or 3")
    _assert_participant(req.class_id, req.student)
    if req.class_id:
        state = _session_state(req.class_id)
        if state == "paused":
            raise HTTPException(423, _PAUSED_MSG)
        if state == "ended":
            raise HTTPException(423, _ENDED_HINT_MSG)
    p = _bundle_problem(req.set_id, req.problem_id)
    gr = tc.grade_problem(p, req.sql)
    if gr.correct:
        return {"correct": True, "hint": None, "level": req.level, "leaked": False}

    if req.level == 3:
        # deterministic rung: log the request, return no model text (the client renders the
        # diff evidence it already holds).
        if req.class_id and req.student:
            try:
                rec = classes.append_attempt(req.class_id, {
                    "student": req.student, "problem_id": p["id"], "set_id": req.set_id,
                    "kind": "hint", "correct": None, "category": None,
                    "n_passed": sum(1 for s in gr.per_seed if s.ok),
                    "n_seeds": gr.n_seeds, "hint_level": 3, "sql": req.sql,
                })
                _forward_attempt(req.class_id, rec)
            except Exception:  # noqa: BLE001 — logging must never break hints
                pass
        return {"correct": False, "hint": None, "level": 3, "leaked": False}

    hint = tc.hint_for(p, _redacted(p), gr, req.level, req.sql, model=HINT_MODEL)
    # execution leak guard, off baked gold results (no gold SQL). If a hint smuggled a working
    # query, fall back to the deterministic offline hint, which provably cannot leak.
    leaked = tc.hint_leaks_for(p, hint)
    if leaked:
        hint = tc.hint_for(p, _redacted(p), gr, req.level, req.sql, model=None)
    if req.class_id and req.student:
        try:
            rec = classes.append_attempt(req.class_id, {
                "student": req.student, "problem_id": p["id"], "set_id": req.set_id,
                "kind": "hint", "correct": None, "category": None,
                "n_passed": sum(1 for s in gr.per_seed if s.ok),
                "n_seeds": gr.n_seeds, "hint_level": req.level, "sql": req.sql,
            })
            _forward_attempt(req.class_id, rec)
        except Exception:  # noqa: BLE001 — logging must never break hints
            pass
    return {"correct": False, "hint": hint, "level": req.level, "leaked": leaked}


# =========================================================================== #
# INSTRUCTOR — private source sets + authoring + publish
# =========================================================================== #
@instructor_router.get("/sets")
def instructor_sets():
    return store.list_sets()


class NewSet(BaseModel):
    title: str


@instructor_router.post("/sets")
def instructor_new_set(req: NewSet):
    return store.new_set(req.title)


@instructor_router.get("/sets/{set_id}")
def instructor_get_set(set_id: str):
    try:
        return store.get_set(set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")


class RenameSet(BaseModel):
    title: str


@instructor_router.patch("/sets/{set_id}")
def instructor_rename_set(set_id: str, req: RenameSet):
    """Rename a set after creation (e.g. an assignment whose title was set in the batch form).
    The id is immutable; only the display title changes, synced into the published bundle."""
    try:
        return store.rename_set(set_id, req.title)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")
    except ValueError as e:
        raise HTTPException(400, str(e))


@instructor_router.delete("/sets/{set_id}")
def instructor_remove_set(set_id: str):
    attached = [c for c in classes.list_classes()
                if set_id in (c.get("set_ids") or ([c["set_id"]] if c.get("set_id") else []))]
    if attached:
        names = ", ".join(f'"{c["title"]}"' for c in attached)
        raise HTTPException(
            409,
            f"This set has classes attached ({names}). Classes and their attempt logs "
            f"are kept; delete is blocked.",
        )
    try:
        store.remove_set(set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")
    return {"ok": True}


# ---- authoring as a background job ---------------------------------------- #
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()


class AuthorReq(BaseModel):
    prompt: str
    gold_sql: str
    title: str
    difficulty: str = "medium"
    confirmed_nudges: list[dict] = []
    ddl: str | None = None
    predict: bool = False


def _run_author(job_id: str, req: AuthorReq):
    try:
        result = authoring.author(req.prompt, req.gold_sql, AUTHOR_MODEL,
                                  req.title, req.difficulty,
                                  confirmed_nudges=req.confirmed_nudges, ddl=req.ddl,
                                  predict=req.predict)
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "done", "result": result}
    except Exception as e:  # noqa: BLE001 — surface any authoring crash to the UI
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "error", "result": {"status": "error",
                             "stage": "crash", "reason": repr(e)}}


@instructor_router.post("/author")
def instructor_author(req: AuthorReq):
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"state": "running", "result": None}
    threading.Thread(target=_run_author, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id, "state": "running"}


class AuthorBatchSection(BaseModel):
    table_hint: str
    items: list[dict]


class AuthorBatchReq(BaseModel):
    # back-compat single-section shape
    table_hint: str | None = None
    items: list[dict] | None = None
    # multi-section shape
    sections: list[AuthorBatchSection] | None = None

    def as_sections(self) -> list[dict]:
        if self.sections is not None:
            return [{"table_hint": s.table_hint, "items": s.items} for s in self.sections]
        return [{"table_hint": self.table_hint or "", "items": self.items or []}]


def _run_author_batch(job_id: str, req: AuthorBatchReq):
    def on_progress(i, n, title):
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "running",
                             "progress": {"done": i, "total": n, "current": title}}
    try:
        result = authoring.author_batch_sections(req.as_sections(), AUTHOR_MODEL,
                                                   on_progress=on_progress)
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "done", "result": result}
    except Exception as e:  # noqa: BLE001 — surface any authoring crash to the UI
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "error", "result": {"status": "error",
                             "stage": "crash", "reason": repr(e)}}


@instructor_router.post("/author-batch")
def instructor_author_batch(req: AuthorBatchReq):
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"state": "running", "result": None}
    threading.Thread(target=_run_author_batch, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id, "state": "running"}


@instructor_router.get("/jobs/{job_id}")
def instructor_job(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(404, f"no job '{job_id}'")
    return job


class AddProblem(BaseModel):
    problem: dict


@instructor_router.post("/sets/{set_id}/problems")
def instructor_add_problem(set_id: str, req: AddProblem):
    try:
        return store.add_problem(set_id, req.problem)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")


@instructor_router.delete("/sets/{set_id}/problems/{problem_id}")
def instructor_remove_problem(set_id: str, problem_id: str):
    try:
        return store.remove_problem(set_id, problem_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")
    except ValueError as e:
        raise HTTPException(404, str(e))


class ReorderReq(BaseModel):
    order: list[str]


@instructor_router.post("/sets/{set_id}/reorder")
def instructor_reorder(set_id: str, req: ReorderReq):
    try:
        return store.reorder_problems(set_id, req.order)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")
    except ValueError as e:
        raise HTTPException(400, str(e))


class UpdateProblemReq(BaseModel):
    title: str | None = None
    prompt: str | None = None
    difficulty: str | None = None


@instructor_router.patch("/sets/{set_id}/problems/{problem_id}")
def instructor_update_problem(set_id: str, problem_id: str, req: UpdateProblemReq):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        return store.update_problem(set_id, problem_id, fields)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")
    except ValueError as e:
        raise HTTPException(404, str(e))


class ReauthorReq(BaseModel):
    ddl: str


def _run_reauthor_problem(job_id: str, set_id: str, problem_id: str, req: ReauthorReq):
    try:
        s = store.get_set(set_id)
        p = next((p for p in s["problems"] if p["id"] == problem_id), None)
        if p is None:
            with _JOBS_LOCK:
                _JOBS[job_id] = {"state": "error", "result": {
                    "status": "error", "stage": "lookup",
                    "reason": f"no problem '{problem_id}' in set '{set_id}'"}}
            return
        result = authoring.author(p["prompt"], p["gold_sql"], AUTHOR_MODEL,
                                    p["title"], p.get("difficulty", "medium"), ddl=req.ddl)
        if result.get("status") == "ok":
            store.replace_problem(set_id, problem_id, result["problem"])
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "done", "result": result}
    except Exception as e:  # noqa: BLE001 — surface any authoring crash to the UI
        with _JOBS_LOCK:
            _JOBS[job_id] = {"state": "error", "result": {"status": "error",
                             "stage": "crash", "reason": repr(e)}}


@instructor_router.post("/sets/{set_id}/problems/{problem_id}/reauthor")
def instructor_reauthor_problem(set_id: str, problem_id: str, req: ReauthorReq):
    try:
        store.get_set(set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[job_id] = {"state": "running", "result": None}
    threading.Thread(target=_run_reauthor_problem, args=(job_id, set_id, problem_id, req),
                      daemon=True).start()
    return {"job_id": job_id, "state": "running"}


@instructor_router.get("/sets/{set_id}/export")
def instructor_export_set(set_id: str):
    """Instructor-private export — includes gold_sql, schema, generator source."""
    try:
        return store.get_set(set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")


class ImportSetReq(BaseModel):
    set: dict


@instructor_router.post("/sets/import")
def instructor_import_set(req: ImportSetReq):
    obj = req.set
    if not isinstance(obj.get("title"), str) or not isinstance(obj.get("problems"), list):
        raise HTTPException(400, "set must have a 'title' string and a 'problems' list")
    for p in obj["problems"]:
        if not all(k in p for k in ("id", "title", "prompt", "gold_sql", "schema")):
            raise HTTPException(400, "each problem needs id, title, prompt, gold_sql, schema")
    new = store.new_set(obj["title"])
    new["problems"] = obj["problems"]
    new["published_at"] = None
    return store.save_set(new)


@instructor_router.post("/sets/{set_id}/publish")
def instructor_publish(set_id: str):
    try:
        return publish.publish(set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no set '{set_id}'")
    except ValueError as e:
        raise HTTPException(400, str(e))


# =========================================================================== #
# CLASSES — instructor class codes + per-class attempt logs
# =========================================================================== #
class NewClass(BaseModel):
    title: str
    # accept either a single set_id (back-compat) or a list of set_ids
    set_id: str | None = None
    set_ids: list[str] | None = None
    mode: str = "open"
    roster: list[str] = []

    def resolved_set_ids(self) -> list[str]:
        if self.set_ids:
            return self.set_ids
        return [self.set_id] if self.set_id else []


@instructor_router.get("/config")
def instructor_get_config():
    """Instructor-wide settings. `instructor_url` is the publicly reachable address of THIS
    backend; when set, it gets baked into every exported assignment so students' apps push
    their attempts back live (network sync) instead of the file round-trip."""
    return {"instructor_url": config.load().get("instructor_url")}


class ConfigReq(BaseModel):
    instructor_url: str | None = None


@instructor_router.patch("/config")
def instructor_set_config(req: ConfigReq):
    url = (req.instructor_url or "").strip()
    config.set("instructor_url", url or None)
    return {"instructor_url": config.load().get("instructor_url")}


def _lan_ipv4s() -> list[str]:
    """Best-effort list of this machine's LAN IPv4 addresses (loopback excluded). The first
    entry is the interface used to reach the wider network — usually the right one to hand to
    students on the same Wi-Fi/LAN."""
    ips: list[str] = []
    # primary: the source IP the OS would use for an outbound connection (no packets sent)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    # secondary: any other addresses bound to the hostname
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


class SessionReq(BaseModel):
    state: str  # "running" | "paused" | "ended"


@instructor_router.patch("/classes/{class_id}/session")
def instructor_set_session(class_id: str, req: SessionReq):
    """Live session control: pause (freeze submit + hints), end (submit current question only,
    no hints, no switching), or resume. Students pick this up via class-status polling."""
    try:
        c = classes.set_session_state(class_id, req.state)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"class_id": class_id, "session_state": c["session_state"]}


@instructor_router.get("/host-info")
def instructor_host_info():
    """What students need to reach THIS machine: its LAN URL(s) + the port. The UI offers one
    of these as a one-click 'host on this network' value (saved as instructor_url)."""
    port = os.environ.get("PORT", "8077")
    ips = _lan_ipv4s()
    return {
        "port": port,
        "hostname": socket.gethostname(),
        "lan_urls": [f"http://{ip}:{port}" for ip in ips],
        "configured_url": config.load().get("instructor_url"),
    }


@instructor_router.post("/classes")
def instructor_new_class(req: NewClass):
    set_ids = req.resolved_set_ids()
    if not set_ids:
        raise HTTPException(400, "a class needs at least one published set")
    for sid in set_ids:
        try:
            store.get_bundle(sid)
        except FileNotFoundError:
            raise HTTPException(404, f"no published set '{sid}'")
    return classes.new_class(req.title, set_ids, mode=req.mode, roster=req.roster)


def _set_titles() -> dict[str, str]:
    return {b["id"]: b["title"] for b in store.list_bundles()}


@instructor_router.get("/classes")
def instructor_list_classes():
    titles = _set_titles()
    out = []
    for c in classes.list_classes():
        attempts = classes.read_attempts(c["id"])
        n_students = len({a["student"] for a in attempts})
        set_ids = c.get("set_ids") or ([c["set_id"]] if c.get("set_id") else [])
        out.append({
            **c, "n_attempts": len(attempts), "n_students": n_students,
            "n_sets": len(set_ids),
            "set_titles": [titles.get(sid, sid) for sid in set_ids],
            "state": classes.effective_state(c),
        })
    return out


@instructor_router.get("/classes/{class_id}")
def instructor_get_class(class_id: str):
    try:
        c = classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    attempts = classes.read_attempts(class_id)

    by_student: dict[str, dict] = {}
    by_problem: dict[str, dict] = {}
    for a in attempts:
        s = by_student.setdefault(a["student"], {"attempts": 0, "solved": set()})
        s["attempts"] += 1
        if a["kind"] == "grade" and a.get("correct"):
            s["solved"].add(a["problem_id"])

        pr = by_problem.setdefault(a["problem_id"], {"attempts": 0, "n_solved": 0, "solvers": set()})
        pr["attempts"] += 1
        if a["kind"] == "grade" and a.get("correct") and a["student"] not in pr["solvers"]:
            pr["solvers"].add(a["student"])
            pr["n_solved"] += 1

    students = [{"student": s, "attempts": v["attempts"], "n_solved": len(v["solved"])}
                for s, v in sorted(by_student.items())]
    problems = [{"problem_id": p, "attempts": v["attempts"], "n_solved": v["n_solved"]}
                for p, v in sorted(by_problem.items())]

    return {**c, "state": classes.effective_state(c), "n_attempts": len(attempts),
            "students": students, "problems": problems}


class UpdateClassReq(BaseModel):
    title: str | None = None
    mode: str | None = None
    roster: list[str] | None = None
    set_ids: list[str] | None = None
    status: str | None = None
    # scheduling: pass an ISO string to set, or the empty string to clear
    active_from: str | None = None
    active_until: str | None = None


@instructor_router.patch("/classes/{class_id}")
def instructor_update_class(class_id: str, req: UpdateClassReq):
    raw = req.model_dump(exclude_unset=True)
    fields = {}
    for k, v in raw.items():
        # scheduling/status fields accept "" to clear; everything else skips None
        if k in ("active_from", "active_until"):
            fields[k] = v or None
        elif v is not None:
            fields[k] = v
    if "set_ids" in fields:
        for sid in fields["set_ids"]:
            try:
                store.get_bundle(sid)
            except FileNotFoundError:
                raise HTTPException(404, f"no published set '{sid}'")
    try:
        return classes.update_class(class_id, fields)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")


@instructor_router.delete("/classes/{class_id}")
def instructor_remove_class(class_id: str):
    try:
        classes.remove_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    return {"ok": True}


@instructor_router.delete("/classes/{class_id}/student/{student}")
def instructor_delete_student(class_id: str, student: str):
    """Remove one student from a class by deleting every attempt logged under their name. They
    drop out of the student list and insights; if they're on the roster they remain there (edit
    the roster to remove the name itself)."""
    try:
        classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    removed = classes.delete_student(class_id, student)
    return {"ok": True, "removed": removed}


@instructor_router.delete("/classes/{class_id}/attempt/{uid}")
def instructor_delete_attempt(class_id: str, uid: str):
    """Remove a single grade/hint event from a student's history by its uid."""
    try:
        classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    if not classes.delete_attempt(class_id, uid):
        raise HTTPException(404, "no attempt with that id")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# ASSIGNMENT EXPORT/IMPORT — instructor exports one file, student loads it locally
# --------------------------------------------------------------------------- #
def _assignment_for_class(c: dict) -> dict:
    """Build the (unsealed) assignment object for a class: its metadata + every assigned
    student-safe bundle, plus the instructor's sync URL so the importing app can push attempts
    back. Shared by the file export and the network 'fetch by code' path. Raises 404 if a
    referenced set has not been published."""
    set_ids = c.get("set_ids") or ([c["set_id"]] if c.get("set_id") else [])
    bundles = []
    for sid in set_ids:
        try:
            b = store.get_bundle(sid)
        except FileNotFoundError:
            raise HTTPException(404, f"no published set '{sid}'")
        store.assert_student_safe(b)
        bundles.append(b)
    return {
        "format": "rundiff-assignment-v1",
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "class": {
            "id": c["id"], "title": c["title"],
            "set_id": set_ids[0] if set_ids else None, "set_ids": set_ids,
            "passphrase": c["passphrase"], "mode": c["mode"], "roster": c.get("roster", []),
            "passcodes": c.get("passcodes", {}),
            "status": c.get("status", "active"),
            "active_from": c.get("active_from"), "active_until": c.get("active_until"),
            "instructor_url": config.load().get("instructor_url"),
        },
        # `bundle` (singular) kept for back-compat with older student importers; `bundles`
        # carries every assigned set.
        "bundle": bundles[0] if bundles else None,
        "bundles": bundles,
    }


@instructor_router.get("/classes/{class_id}/export-assignment")
def instructor_export_assignment(class_id: str):
    try:
        c = classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    # Sealed by default: a curious student opening this file in a text editor sees only a
    # base64 blob, not the per-seed baked gold rows. See README.md "What an assignment file
    # can and cannot leak". The bundles are already student-safe (assert ran in the builder).
    return seal.seal(_assignment_for_class(c))


class ImportAssignmentReq(BaseModel):
    assignment: dict


def _import_assignment(a: dict) -> dict:
    """Validate + persist an assignment (sealed or v1): save its student-safe bundles and
    write the class record locally. Shared by the file-import endpoint and the network-connect
    path. Returns a join-shaped summary."""
    if a.get("format") == "rundiff-assignment-v2" and a.get("sealed"):
        try:
            a = seal.unseal(a)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if a.get("format") != "rundiff-assignment-v1":
        raise HTTPException(400, "unrecognized assignment format")
    c = a.get("class")
    bundles = a.get("bundles") or ([a["bundle"]] if a.get("bundle") else [])
    if not isinstance(c, dict) or not bundles:
        raise HTTPException(400, "assignment must have a 'class' object and at least one bundle")
    for k in ("id", "title", "passphrase", "mode", "roster"):
        if k not in c:
            raise HTTPException(400, f"assignment.class missing '{k}'")
    for bundle in bundles:
        try:
            store.assert_student_safe(bundle)
        except ValueError as e:
            raise HTTPException(400, str(e))
        store.save_bundle(bundle)
    set_ids = c.get("set_ids") or ([c["set_id"]] if c.get("set_id") else [b["id"] for b in bundles])
    classes.put_class({
        "id": c["id"], "title": c["title"],
        "set_ids": set_ids, "set_id": set_ids[0] if set_ids else None,
        "passphrase": c["passphrase"], "mode": c["mode"], "roster": list(c.get("roster", [])),
        "passcodes": c.get("passcodes", {}),
        "status": c.get("status", "active"),
        "active_from": c.get("active_from"), "active_until": c.get("active_until"),
        "created": classes._now(),
        "instructor_url": c.get("instructor_url"),
    })
    return {"class_id": c["id"], "set_id": set_ids[0] if set_ids else None,
            "set_ids": set_ids, "title": c["title"], "passphrase": c["passphrase"]}


@app.post("/api/student/import-assignment")
def student_import_assignment(req: ImportAssignmentReq):
    return _import_assignment(req.assignment)


# --------------------------------------------------------------------------- #
# ANALYTICS — per-class problem/student rollups + predicted-vs-actual hints
# --------------------------------------------------------------------------- #
def _class_set_ids(c: dict) -> list[str]:
    return c.get("set_ids") or ([c["set_id"]] if c.get("set_id") else [])


def _problem_to_set(set_ids: list[str]) -> dict[str, str]:
    """Map each problem id to the set it belongs to (for filtering older attempts that predate
    the per-attempt set_id field). First set wins on the rare cross-set id collision."""
    out: dict[str, str] = {}
    for sid in set_ids:
        try:
            for p in store.get_set(sid)["problems"]:
                out.setdefault(p["id"], sid)
        except FileNotFoundError:
            pass
    return out


def _scope_attempts(attempts: list[dict], scope_ids: list[str], p2s: dict[str, str]) -> list[dict]:
    """Keep only attempts belonging to the set(s) in view, judged by each attempt's own set_id
    (legacy attempts that predate the field fall back to the problem→set map).

    Essential for the "all sets" rollups (set_id=None). Without it, every attempt ever recorded
    for the class is counted — including attempts on a set that's since been unassigned. Because
    problem ids can repeat across sets (e.g. set2 reuses set1's questions), those stale attempts
    then get misattributed to a currently-assigned set's identically-id'd problems: assign set1,
    students answer, unassign set1, and set1's numbers bleed onto set2 even though nobody touched
    set2. Scoping by the attempt's real set keeps each set's stats its own."""
    scope = set(scope_ids)
    return [a for a in attempts
            if (a.get("set_id") or p2s.get(a.get("problem_id"))) in scope]


def _build_analytics(class_id: str, set_id: str | None = None) -> dict:
    """Aggregate `data/attempts/<class_id>.jsonl` into per-problem and per-student rollups.
    Student names are joined case-insensitively (the join flow lowercases sometimes); each
    group's display name is the first-seen casing. Titles + predictions come from the
    instructor source set(s) (gold-free here — only `title`/`prediction` are read). When
    `set_id` is given, only that set's attempts/problems are reported."""
    c = classes.get_class(class_id)
    attempts = classes.read_attempts(class_id)
    set_ids = _class_set_ids(c)
    scope_ids = [set_id] if set_id else set_ids
    p2s = _problem_to_set(set_ids)
    attempts = _scope_attempts(attempts, scope_ids, p2s)

    # title + prediction + difficulty lookup from the instructor source set(s), plus the
    # canonical problem ORDER (set order — what the chip row and grid display in). Every problem
    # in scope is listed even if nobody has attempted it yet, so stats reflect the whole set.
    titles: dict[str, str] = {}
    predictions: dict[str, dict | None] = {}
    difficulty: dict[str, str] = {}
    order: list[str] = []
    for sid in scope_ids:
        try:
            src = store.get_set(sid)
            for p in src["problems"]:
                if p["id"] not in titles:
                    order.append(p["id"])
                titles[p["id"]] = p.get("title", p["id"])
                predictions[p["id"]] = p.get("prediction")
                difficulty[p["id"]] = p.get("difficulty", "medium")
        except FileNotFoundError:
            pass

    # canonicalize student names case-insensitively; keep first-seen casing for display
    display_name: dict[str, str] = {}

    def _key(student: str) -> str:
        k = student.strip().lower()
        display_name.setdefault(k, student.strip())
        return k

    by_problem: dict[str, dict] = {}
    by_student: dict[str, dict] = {}

    for a in attempts:
        skey = _key(a["student"])
        pid = a["problem_id"]
        pr = by_problem.setdefault(pid, {
            "attempts": 0, "students": {}, "hint_requests": {},
        })
        st = by_student.setdefault(skey, {
            "attempts": 0, "solved": set(), "total_attempts": 0, "hints_used": 0,
        })

        pr["attempts"] += 1
        ps = pr["students"].setdefault(skey, {
            "n_grades": 0, "first_solve_at": None, "solved": False, "max_hint_level": 0,
            "hint_levels": {},
        })

        st["attempts"] += 1
        st["total_attempts"] += 1

        if a["kind"] == "grade":
            ps["n_grades"] += 1
            if a.get("correct"):
                if ps["first_solve_at"] is None:
                    ps["first_solve_at"] = ps["n_grades"]
                ps["solved"] = True
                st["solved"].add(pid)
        elif a["kind"] == "hint":
            level = a.get("hint_level")
            if level is not None:
                pr["hint_requests"][level] = pr["hint_requests"].get(level, 0) + 1
                ps["max_hint_level"] = max(ps["max_hint_level"], level)
                ps["hint_levels"][level] = ps["hint_levels"].get(level, 0) + 1
                st["hints_used"] += 1

    # every in-scope problem gets a row, attempted or not (zero-stat entry otherwise)
    for pid in order:
        by_problem.setdefault(pid, {"attempts": 0, "students": {}, "hint_requests": {}})

    problems = []
    for pid, pr in by_problem.items():
        students_attempted = len(pr["students"])
        solved = [ps for ps in pr["students"].values() if ps["solved"]]
        students_solved = len(solved)
        first_solves = [ps["first_solve_at"] for ps in solved if ps["first_solve_at"] is not None]
        max_levels = [ps["max_hint_level"] for ps in pr["students"].values()]
        student_rows = [{
            "student": display_name[skey],
            "n_grades": ps["n_grades"],
            "solved": ps["solved"],
            "first_solve_at": ps["first_solve_at"],
            "max_hint_level": ps["max_hint_level"],
            "hint_levels": {str(k): v for k, v in ps["hint_levels"].items()},
        } for skey, ps in pr["students"].items()]
        student_rows.sort(key=lambda r: r["student"].lower())
        problems.append({
            "problem_id": pid,
            "title": titles.get(pid, pid),
            "difficulty": difficulty.get(pid),
            "attempts": pr["attempts"],
            "students_attempted": students_attempted,
            "students_solved": students_solved,
            "solve_rate": round(students_solved / students_attempted, 3) if students_attempted else None,
            "avg_attempts_to_first_solve": round(sum(first_solves) / len(first_solves), 2) if first_solves else None,
            "hint_requests": pr["hint_requests"],
            "avg_max_hint_level_used": round(sum(max_levels) / len(max_levels), 2) if max_levels else None,
            "predicted": predictions.get(pid),
            "students": student_rows,
        })
    # display in set order; any legacy problem not in the current set(s) sinks to the end
    order_idx = {pid: i for i, pid in enumerate(order)}
    problems.sort(key=lambda p: order_idx.get(p["problem_id"], 1e9))

    students = [{
        "student": display_name[skey],
        "attempted": v["attempts"] > 0,
        "solved": len(v["solved"]),
        "total_attempts": v["total_attempts"],
        "hints_used": v["hints_used"],
    } for skey, v in sorted(by_student.items(), key=lambda kv: display_name[kv[0]].lower())]

    # roster names who haven't logged a single attempt still appear (as "not started") so the
    # overview reflects the whole class, not just whoever has shown up. Matched case-insensitively
    # against the students who have attempts.
    seen = {s["student"].strip().lower() for s in students}
    for name in c.get("roster", []):
        if name.strip().lower() not in seen:
            students.append({
                "student": name.strip(), "attempted": False,
                "solved": 0, "total_attempts": 0, "hints_used": 0,
            })
    students.sort(key=lambda s: s["student"].lower())

    n_students = len(by_student)
    n_attempts = len(attempts)
    grade_attempts = [a for a in attempts if a["kind"] == "grade"]
    n_solved_attempts = sum(1 for a in grade_attempts if a.get("correct"))
    overall_solve_rate = round(n_solved_attempts / len(grade_attempts), 3) if grade_attempts else None

    titles_by_set = _set_titles()
    return {
        "class_id": class_id,
        "title": c["title"],
        "set_id": set_id,
        "sets": [{"id": sid, "title": titles_by_set.get(sid, sid)} for sid in set_ids],
        "roster": list(c.get("roster", [])),
        "mode": c.get("mode"),
        "problems": problems,
        "students": students,
        "summary": {
            "n_students": n_students,
            "n_attempts": n_attempts,
            "overall_solve_rate": overall_solve_rate,
            "n_problems": len(order),
        },
    }


@instructor_router.get("/classes/{class_id}/analytics")
def instructor_class_analytics(class_id: str, set_id: str | None = Query(default=None)):
    try:
        return _build_analytics(class_id, set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")


def _build_student_detail(class_id: str, student: str, set_id: str | None = None) -> dict:
    """Full chronological story for one student in a class: every grade and hint event, in
    order, with the problem it belongs to, plus a per-problem rollup (attempts, solved, the
    attempt # they first passed on, and the hint levels they reached). Matched case-insensitively
    so it lines up with the analytics rollups. `student` is the display name from analytics."""
    c = classes.get_class(class_id)
    attempts = classes.read_attempts(class_id)
    set_ids = _class_set_ids(c)
    p2s = _problem_to_set(set_ids)
    scope_ids = [set_id] if set_id else set_ids
    attempts = _scope_attempts(attempts, scope_ids, p2s)

    titles: dict[str, str] = {}
    order: dict[str, int] = {}
    for sid in scope_ids:
        try:
            for i, p in enumerate(store.get_set(sid)["problems"]):
                titles.setdefault(p["id"], p.get("title", p["id"]))
                order.setdefault(p["id"], len(order))
        except FileNotFoundError:
            pass

    key = student.strip().lower()
    mine = [a for a in attempts if a.get("student", "").strip().lower() == key]
    mine.sort(key=lambda a: a.get("ts", ""))

    by_problem: dict[str, dict] = {}
    timeline = []
    for a in mine:
        pid = a["problem_id"]
        pb = by_problem.setdefault(pid, {
            "problem_id": pid, "title": titles.get(pid, pid),
            "n_grades": 0, "solved": False, "first_solve_at": None, "max_hint_level": 0,
            "hint_levels": {},
        })
        ev = {"ts": a["ts"], "uid": a.get("uid") or classes._uid(a), "kind": a["kind"],
              "problem_id": pid, "title": titles.get(pid, pid), "sql": a.get("sql")}
        if a["kind"] == "grade":
            pb["n_grades"] += 1
            ev["correct"] = a.get("correct")
            ev["category"] = a.get("category")
            ev["n_passed"] = a.get("n_passed")
            ev["n_seeds"] = a.get("n_seeds")
            ev["attempt_no"] = pb["n_grades"]
            if a.get("correct"):
                pb["solved"] = True
                if pb["first_solve_at"] is None:
                    pb["first_solve_at"] = pb["n_grades"]
        else:  # hint
            lvl = a.get("hint_level")
            ev["hint_level"] = lvl
            if lvl is not None:
                pb["max_hint_level"] = max(pb["max_hint_level"], lvl)
                pb["hint_levels"][str(lvl)] = pb["hint_levels"].get(str(lvl), 0) + 1
        timeline.append(ev)

    problems = sorted(by_problem.values(), key=lambda p: order.get(p["problem_id"], 1e9))
    return {
        "class_id": class_id, "title": c["title"], "student": student.strip(),
        "n_attempts": len(mine),
        "n_solved": sum(1 for p in problems if p["solved"]),
        "n_problems_touched": len(problems),
        "problems": problems,
        "timeline": timeline,
    }


@instructor_router.get("/classes/{class_id}/student/{student}")
def instructor_class_student(class_id: str, student: str, set_id: str | None = Query(default=None)):
    try:
        return _build_student_detail(class_id, student, set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")


@instructor_router.get("/classes/{class_id}/analytics.csv")
def instructor_class_analytics_csv(class_id: str, set_id: str | None = Query(default=None)):
    try:
        data = _build_analytics(class_id, set_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "problem_id", "title", "attempts", "students_attempted", "students_solved",
        "solve_rate", "avg_attempts_to_first_solve",
        "hint_requests_l1", "hint_requests_l2", "hint_requests_l3",
        "avg_max_hint_level_actual", "avg_max_hint_level_predicted",
    ])
    for p in data["problems"]:
        hr = p["hint_requests"]
        pred = p["predicted"] or {}
        w.writerow([
            p["problem_id"], p["title"], p["attempts"], p["students_attempted"],
            p["students_solved"], p["solve_rate"], p["avg_attempts_to_first_solve"],
            hr.get("1", hr.get(1, 0)), hr.get("2", hr.get(2, 0)), hr.get("3", hr.get(3, 0)),
            p["avg_max_hint_level_used"], pred.get("avg_max_hint_level"),
        ])

    headers = {"Content-Disposition": f'attachment; filename="{class_id}-analytics.csv"'}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv", headers=headers)


@instructor_router.get("/classes/{class_id}/live")
def instructor_class_live(class_id: str, since: str | None = Query(default=None),
                          set_id: str | None = Query(default=None)):
    """Live/async class-progress view, computed fresh from the attempts log each call.

    `since` (ISO timestamp, optional) scopes which attempts are considered at all — this is
    the "session" boundary. Students with no attempts after `since` simply don't appear.
    `set_id` (optional) restricts the grid to one of the class's assigned sets.
    """
    try:
        c = classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    attempts = classes.read_attempts(class_id)
    set_ids = _class_set_ids(c)
    scope_ids = [set_id] if set_id else set_ids
    p2s = _problem_to_set(set_ids)

    if since:
        attempts = [a for a in attempts if a.get("ts", "") > since]
    attempts = _scope_attempts(attempts, scope_ids, p2s)

    # problems: ordered from each in-scope published bundle (assignment order); titles fall
    # back to the source set, same as _build_analytics.
    titles: dict[str, str] = {}
    for sid in scope_ids:
        try:
            for p in store.get_set(sid)["problems"]:
                titles[p["id"]] = p.get("title", p["id"])
        except FileNotFoundError:
            pass

    problems = []
    for sid in scope_ids:
        try:
            for p in store.get_bundle(sid)["problems"]:
                problems.append({"problem_id": p["id"],
                                 "title": titles.get(p["id"], p.get("title", p["id"]))})
        except FileNotFoundError:
            pass

    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat(timespec="seconds")

    # canonicalize student names case-insensitively; keep first-seen casing for display
    display_name: dict[str, str] = {}

    def _key(student: str) -> str:
        k = student.strip().lower()
        display_name.setdefault(k, student.strip())
        return k

    by_student: dict[str, dict] = {}
    for a in attempts:
        skey = _key(a["student"])
        st = by_student.setdefault(skey, {"last_ts": a["ts"], "problems": {}})
        if a["ts"] > st["last_ts"]:
            st["last_ts"] = a["ts"]
        pid = a["problem_id"]
        pp = st["problems"].setdefault(pid, {
            "status": None, "n_grades": 0, "n_hints": 0, "max_hint_level": 0, "last_ts": a["ts"],
        })
        if a["ts"] > pp["last_ts"]:
            pp["last_ts"] = a["ts"]

        if a["kind"] == "grade":
            pp["n_grades"] += 1
            if a.get("correct"):
                pp["status"] = "solved"
            elif pp["status"] != "solved":
                pp["status"] = "trying"
        elif a["kind"] == "hint":
            pp["n_hints"] += 1
            level = a.get("hint_level")
            if level is not None:
                pp["max_hint_level"] = max(pp["max_hint_level"], level)
            if pp["status"] is None:
                pp["status"] = "hinted"

    students = []
    n_solved_cells = 0
    for skey, st in by_student.items():
        last_seen = st["last_ts"]
        try:
            active = (now_dt - datetime.fromisoformat(last_seen)).total_seconds() <= 120
        except ValueError:
            active = False
        prob_map = {}
        for pid, pp in st["problems"].items():
            if pp["status"] == "solved":
                n_solved_cells += 1
            prob_map[pid] = {
                "status": pp["status"],
                "n_grades": pp["n_grades"],
                "n_hints": pp["n_hints"],
                "max_hint_level": pp["max_hint_level"],
                "last_ts": pp["last_ts"],
            }
        students.append({
            "student": display_name[skey],
            "last_seen": last_seen,
            "active": active,
            "problems": prob_map,
        })
    students.sort(key=lambda s: s["student"].lower())

    recent = [{
        "ts": a["ts"], "student": a["student"], "problem_id": a["problem_id"],
        "kind": a["kind"], "correct": a.get("correct"), "hint_level": a.get("hint_level"),
        "category": a.get("category"),
    } for a in attempts[-20:][::-1]]

    titles_by_set = _set_titles()
    return {
        "class_id": class_id,
        "title": c["title"],
        "set_id": set_id,
        "sets": [{"id": sid, "title": titles_by_set.get(sid, sid)} for sid in set_ids],
        "problems": problems,
        "students": students,
        "recent": recent,
        "summary": {
            "n_students": len(students),
            "n_active": sum(1 for s in students if s["active"]),
            "n_solved_cells": n_solved_cells,
            "n_attempts": len(attempts),
        },
        "since": since,
        "now": now,
    }


@instructor_router.post("/classes/{class_id}/import-attempts")
def instructor_import_attempts(class_id: str, body: dict):
    try:
        classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    records = body.get("attempts") if "attempts" in body else body
    if not isinstance(records, list):
        raise HTTPException(400, "body must be an attempts export or {'attempts': [...]}")
    accepted, duplicates = classes.append_attempts_dedup(class_id, records)
    return {"accepted": accepted, "duplicates": duplicates}


# --------------------------------------------------------------------------- #
# ATTEMPT SYNC — live forwarding (instructor_url) + file-based fallback
# --------------------------------------------------------------------------- #
class SyncAttemptsReq(BaseModel):
    class_id: str
    passphrase: str
    attempts: list[dict]


@app.post("/api/sync/attempts")
def sync_attempts(req: SyncAttemptsReq):
    """Instructor-side ingest, called either live (one record at a time, forwarded from a
    student's grade/hint call) or in bulk (student's manual sync / file import). Not
    author-gated — students call this directly with the class passphrase as credential."""
    _require_hosting()
    try:
        c = classes.get_class(req.class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{req.class_id}'")
    if req.passphrase.strip().lower() != c.get("passphrase"):
        raise HTTPException(401, "passphrase does not match this class")
    # Authoritative roster gate for networked students: a removed/off-roster name's forwarded
    # attempts are dropped here so they never reach analytics, even if that student's device is
    # still working against a stale local copy of the roster.
    attempts = [a for a in req.attempts if classes.on_roster(c, a.get("student", ""))]
    rejected = len(req.attempts) - len(attempts)
    accepted, duplicates = classes.append_attempts_dedup(req.class_id, attempts)
    return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected}


class FetchAssignmentReq(BaseModel):
    code: str
    name: str | None = None


@app.post("/api/student/fetch-assignment")
def student_fetch_assignment(req: FetchAssignmentReq):
    """HOST side of network 'connect by code': a student app on the LAN posts a class passphrase
    or personal passcode (plus their name, for roster classes); we return the sealed assignment
    (class + student-safe bundles + this host's sync URL). The code IS the credential — no author
    gate, since students must reach it. Bundles carry no gold SQL, so this exposes nothing the
    file hand-out wouldn't.

    Gated on hosting being on: with 'Host on this network' off, the class server is dark and
    other devices can't connect or pull assignments, even though the local app keeps running."""
    _require_hosting()
    c = classes.resolve_code(req.code)
    if c is None:
        raise HTTPException(404, "no class matches that code")
    # don't hand out the questions before a scheduled test opens (or once it's closed/archived):
    # students shouldn't be able to read them early over the network.
    state = classes.effective_state(c)
    if state != "active":
        raise HTTPException(403, classes._STATE_MESSAGE.get(state, "this class is not open"))
    # roster gate the bundle itself, so the questions don't leak to a name that isn't (or no
    # longer is) on the roster — the network counterpart to join()'s check.
    if not classes.fetch_authorized(c, req.code, req.name):
        raise HTTPException(403, "You are not on the roster for this classroom. "
                                 "Check the spelling of your name, or ask your instructor to add you.")
    return seal.seal(_assignment_for_class(c))


class ConnectReq(BaseModel):
    url: str
    code: str
    name: str | None = None


@app.post("/api/student/connect")
def student_connect(req: ConnectReq):
    """STUDENT side of network 'connect by code': reach a host's /api/student/fetch-assignment
    over the LAN, import the returned assignment locally, then join — all proxied through this
    backend (not the browser), so there's no cross-origin call and we reuse the same outbound
    path as attempt sync. If the host advertised no sync URL, we fall back to the address the
    student typed so live sync still works."""
    base = req.url.strip().rstrip("/")
    if not base:
        raise HTTPException(400, "enter the class server address")
    if not base.startswith(("http://", "https://")):
        base = "http://" + base
    try:
        resp = requests.post(f"{base}/api/student/fetch-assignment",
                             json={"code": req.code, "name": req.name or ""}, timeout=10)
    except requests.RequestException as e:
        raise HTTPException(502, f"could not reach class server at {base}: {e}")
    if resp.status_code == 404:
        raise HTTPException(404, "no class on that server matches your code")
    if resp.status_code == 403:
        # roster rejection / not-open: pass the host's own message through verbatim so the
        # student sees why (e.g. "not on the roster") instead of a generic gateway error.
        detail = (resp.json().get("detail") if resp.headers.get("content-type", "").startswith("application/json")
                  else None) or "the class server refused your request"
        raise HTTPException(403, detail)
    try:
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(502, f"class server returned an error: {e}")

    imported = _import_assignment(resp.json())
    cid = imported["class_id"]
    c = classes.get_class(cid)
    if not c.get("instructor_url"):
        c["instructor_url"] = base
        classes.put_class(c)

    # join with the same code so the student gets their canonical roster name + set ids. A
    # ValueError here is a real rejection (most importantly: not on the roster) — surface it
    # instead of waving the student through, which is what previously let removed/off-roster
    # names in over the network.
    try:
        return classes.join(req.code, req.name or "")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/student/sync/{class_id}")
def student_sync(class_id: str):
    """Push all local attempts for this class to its configured instructor_url."""
    try:
        c = classes.get_class(class_id)
    except FileNotFoundError:
        raise HTTPException(404, f"no class '{class_id}'")
    instructor_url = c.get("instructor_url")
    if not instructor_url:
        raise HTTPException(400, "this class has no instructor_url configured")
    attempts = classes.read_attempts(class_id)
    try:
        resp = requests.post(
            f"{instructor_url.rstrip('/')}/api/sync/attempts",
            json={"class_id": class_id, "passphrase": c["passphrase"], "attempts": attempts},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise HTTPException(502, f"could not reach instructor: {e}")


@app.get("/api/student/attempts-export/{class_id}")
def student_attempts_export(class_id: str):
    return {
        "format": "rundiff-attempts-v1",
        "class_id": class_id,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempts": classes.read_attempts(class_id),
    }


@app.get("/api/student/class-status/{class_id}")
def student_class_status(class_id: str, local: bool = Query(default=False),
                         student: str | None = Query(default=None)):
    """Cheap liveness check the student UI polls during a session. Always 200 so a poll never
    throws — `exists:false` means the instructor deleted the class, and a non-active `state`
    means it was archived or fell outside its scheduled window. `session_state` carries live
    pause/end control. `removed:true` means this `student` is no longer on a roster/passcode
    class's roster (removed by the instructor, or the class flipped open→roster) and must be
    locked out — the live counterpart to the join-time roster check.

    For a networked student (class has an instructor_url) we proxy to the host so pause/end and
    roster membership are learned live, and cache the host's session_state locally so the local
    grade/hint guards match. The student name is forwarded so the host evaluates `removed`
    against its authoritative roster (the local imported copy can be stale). `local=1`
    short-circuits the proxy (the host answers about itself — no loop).

    `local=1` is only ever sent by another device's proxy forwarding in — i.e. a remote
    student asking us as their host. So when hosting is off we refuse it: pause/end (and the
    rest of live status) stop propagating over the LAN just like fetch-assignment and attempt
    ingest, and the remote student falls back to its last-known cached state. A device's own
    (non-proxied) polling has local=False and is never gated."""
    if local:
        _require_hosting()
    try:
        c = classes.get_class(class_id)
    except FileNotFoundError:
        return {"exists": False, "state": "deleted", "session_state": "running"}

    instructor_url = c.get("instructor_url")
    if instructor_url and not local:
        try:
            resp = requests.get(
                f"{instructor_url.rstrip('/')}/api/student/class-status/{class_id}",
                params={"local": 1, "student": student or ""}, timeout=4)
            if resp.ok:
                live = resp.json()
                ss = live.get("session_state")
                if ss and ss != c.get("session_state"):
                    c["session_state"] = ss
                    classes.put_class(c)   # cache so local enforcement matches the host
                return live
        except requests.RequestException:
            pass  # host unreachable: fall back to the last-known local state

    return {"exists": True, "state": classes.effective_state(c),
            "session_state": c.get("session_state", "running"),
            "removed": student is not None and not classes.on_roster(c, student)}


class JoinReq(BaseModel):
    passphrase: str
    name: str


@app.post("/api/student/join")
def student_join(req: JoinReq):
    try:
        return classes.join(req.passphrase, req.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/health")
def health():
    return {"ok": True, "hint_model": HINT_MODEL, "author_model": AUTHOR_MODEL}


app.include_router(instructor_router)

# first-run Ollama setup (NOT author-gated — students need it too)
app.include_router(setup_ollama.router)

# serve the built frontend (single-process app). MUST be last so the SPA catch-all route
# never shadows an /api/* route. No-op in dev when no dist exists.
static.mount_frontend(app)
