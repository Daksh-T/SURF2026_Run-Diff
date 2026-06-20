"""Class codes + attempt logging.

  data/classes/<class_id>.json   instructor-created class record (passphrase, roster, target bundle)
  data/attempts/<class_id>.jsonl append-only per-attempt log (grade + hint events)

Follows store.py's _read/_write/slugify conventions; reuses them directly.
"""
from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

import store

CLASSES = store.DATA / "classes"
ATTEMPTS = store.DATA / "attempts"
for d in (CLASSES, ATTEMPTS):
    d.mkdir(parents=True, exist_ok=True)

# ~150 short common words for passphrases (word-word-word, e.g. "maple-river-stone")
WORDLIST = [
    "able", "acorn", "amber", "apple", "arch", "ash", "aspen", "bay", "bear", "bell",
    "berry", "birch", "blue", "boat", "bold", "bone", "book", "brave", "bread", "bridge",
    "brook", "brown", "cabin", "cactus", "calm", "camp", "candle", "canyon", "cedar", "chalk",
    "cliff", "cloud", "clover", "coal", "coast", "comet", "coral", "cove", "crane", "creek",
    "crest", "crow", "crown", "curve", "dawn", "deer", "delta", "desert", "dew", "dove",
    "dune", "dusk", "eagle", "echo", "elm", "ember", "fable", "falcon", "fern", "field",
    "finch", "fir", "flame", "flint", "fog", "forest", "fox", "frost", "gale", "garden",
    "gate", "glade", "glen", "gold", "grain", "grass", "gravel", "green", "grove", "gull",
    "harbor", "hawk", "haze", "heath", "hill", "holly", "horizon", "ice", "ink", "iris",
    "ivory", "ivy", "jade", "jay", "juniper", "kelp", "kite", "lake", "lamp", "lark",
    "leaf", "lemon", "light", "lilac", "lily", "lime", "lotus", "maple", "marsh", "meadow",
    "mesa", "mint", "mist", "moon", "moss", "mountain", "myrtle", "oak", "oasis", "ocean",
    "olive", "opal", "orchid", "otter", "owl", "palm", "path", "pearl", "pebble", "pine",
    "plain", "plum", "pond", "poppy", "quail", "quartz", "rain", "raven", "reed", "ridge",
    "river", "robin", "rock", "rose", "sage", "sand", "shade", "shore", "silver", "sky",
    "slate", "snow", "spark", "spring", "spruce", "star", "stone", "storm", "stream", "summit",
    "sun", "swan", "tide", "trail", "tree", "tulip", "valley", "violet", "walnut", "wave",
    "wheat", "willow", "wind", "wood", "wren", "zinc",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _all_codes() -> set[str]:
    """Every code currently in use across all classes — class passphrases AND per-student
    passcodes — so a freshly minted code never collides with either pool."""
    out: set[str] = set()
    for p in CLASSES.glob("*.json"):
        c = store._read(p)
        if c.get("passphrase"):
            out.add(c["passphrase"])
        for code in (c.get("passcodes") or {}).values():
            if code:
                out.add(code)
    return out


def _gen_code(words: int, taken: set[str]) -> str:
    """A unique `words`-word dash-joined code (e.g. "maple-river" or "maple-river-stone")
    not present in `taken`. `taken` is mutated as codes are handed out so a batch stays
    internally unique too."""
    for _ in range(400):
        phrase = "-".join(random.sample(WORDLIST, words))
        if phrase not in taken:
            taken.add(phrase)
            return phrase
    # extremely unlikely fallback: widen by one word
    phrase = "-".join(random.sample(WORDLIST, words + 1))
    taken.add(phrase)
    return phrase


def _gen_passphrase() -> str:
    # Both class passphrases and per-student passcodes are 3 words. They never collide because
    # _all_codes() reserves every code already in use across both pools, and join() resolves an
    # entered code by lookup (passcode first, then passphrase) rather than by word count.
    return _gen_code(3, _all_codes())


# --------------------------------------------------------------------------- #
# record normalization + scheduling/status
# --------------------------------------------------------------------------- #
def _normalize(c: dict) -> dict:
    """Bring an on-disk record up to the current shape (older records predate set_ids /
    status / scheduling). Pure in-memory — never rewrites the file."""
    if "set_ids" not in c:
        c["set_ids"] = [c["set_id"]] if c.get("set_id") else []
    # keep set_id as the primary (first) set for any back-compat reader
    if not c.get("set_id") and c["set_ids"]:
        c["set_id"] = c["set_ids"][0]
    c.setdefault("status", "active")        # "active" | "archived"
    c.setdefault("active_from", None)        # ISO; before this the code is not yet live
    c.setdefault("active_until", None)       # ISO; after this the code stops working
    c.setdefault("passcodes", {})            # name -> 2-word personal passcode (passcode mode)
    # live session control, separate from status/scheduling:
    #   "running" (normal) | "paused" (no submit, no hints) | "ended" (submit current Q only, no hints)
    c.setdefault("session_state", "running")
    return c


def set_session_state(class_id: str, state: str) -> dict:
    """Set the live session control for a class. Used by the instructor's Pause / End test /
    Resume controls; students learn it via class-status (proxied over the network when remote)."""
    if state not in ("running", "paused", "ended"):
        raise ValueError("state must be running, paused or ended")
    c = get_class(class_id)
    c["session_state"] = state
    return put_class(c)


def on_roster(c: dict, student: str) -> bool:
    """Whether `student` may still participate in class `c` *right now*. Open classes accept any
    name. Roster and passcode classes require a case-insensitive match against the class's
    current roster — so removing a name (or flipping an open class to roster mode) locks that
    student out on the very next check, not just at join time. The empty name is never on a
    gated roster."""
    if c.get("mode") not in ("roster", "passcode"):
        return True
    key = (student or "").strip().lower()
    if not key:
        return False
    return any((r or "").strip().lower() == key for r in c.get("roster", []))


def effective_state(c: dict) -> str:
    """How the class behaves *right now*: 'active', 'archived', 'scheduled' (not yet open) or
    'closed' (past its window). Insights/stats remain available in every state; only joining
    and taking the test is gated."""
    if c.get("status") == "archived":
        return "archived"
    now = _now()
    if c.get("active_from") and now < c["active_from"]:
        return "scheduled"
    if c.get("active_until") and now > c["active_until"]:
        return "closed"
    return "active"


_STATE_MESSAGE = {
    "archived": "this class has been archived and is no longer accepting attempts",
    "scheduled": "this class is not open yet",
    "closed": "this class is closed",
}


# --------------------------------------------------------------------------- #
# classes
# --------------------------------------------------------------------------- #
def _mint_passcodes(roster: list[str]) -> dict[str, str]:
    """One unique 3-word personal passcode per roster name (used by 'passcode' mode)."""
    taken = _all_codes()
    return {name: _gen_code(3, taken) for name in roster}


def new_class(title: str, set_ids: list[str], mode: str = "open",
               roster: list[str] | None = None, class_id: str | None = None) -> dict:
    cid = class_id or store.slugify(title)
    if (CLASSES / f"{cid}.json").exists():
        n = 2
        while (CLASSES / f"{cid}-{n}.json").exists():
            n += 1
        cid = f"{cid}-{n}"
    roster = list(roster or [])
    obj = {
        "id": cid, "title": title,
        "set_ids": list(set_ids),
        "set_id": set_ids[0] if set_ids else None,   # back-compat primary
        "passphrase": _gen_passphrase(),
        "mode": mode, "roster": roster,
        "passcodes": _mint_passcodes(roster) if mode == "passcode" else {},
        "status": "active", "active_from": None, "active_until": None,
        "created": _now(),
    }
    store._write(CLASSES / f"{cid}.json", obj)
    return obj


def get_class(class_id: str) -> dict:
    return _normalize(store._read(CLASSES / f"{class_id}.json"))


def put_class(obj: dict) -> dict:
    """Write a class record verbatim (used by assignment import — id/passphrase/etc. come
    from the exported record, not freshly generated). Overwrites any existing record with
    the same id."""
    store._write(CLASSES / f"{obj['id']}.json", obj)
    return obj


def update_class(class_id: str, fields: dict) -> dict:
    """Patch title/mode/roster/set_ids/status/scheduling in place. id and passphrase are
    immutable. Switching to passcode mode (or adding new roster names while in it) mints
    fresh personal passcodes for any name that doesn't already have one; existing names keep
    their codes so a handed-out passcode never silently changes."""
    c = get_class(class_id)
    for k, v in fields.items():
        if k in ("id", "passphrase"):
            continue
        if k == "set_ids":
            c["set_ids"] = list(v)
            c["set_id"] = v[0] if v else None
            continue
        c[k] = v
    # keep passcodes consistent with mode + roster
    if c.get("mode") == "passcode":
        taken = _all_codes()
        existing = c.get("passcodes") or {}
        new_codes = {}
        for name in c.get("roster", []):
            new_codes[name] = existing.get(name) or _gen_code(3, taken)
        c["passcodes"] = new_codes
    else:
        c["passcodes"] = {}
    store._write(CLASSES / f"{class_id}.json", c)
    return c


def remove_class(class_id: str) -> None:
    """Delete a class record. If an attempt log exists, rename it (never destroy) to
    {class_id}.deleted-<utc timestamp>.jsonl."""
    path = CLASSES / f"{class_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no class '{class_id}'")
    path.unlink()
    attempts_path = ATTEMPTS / f"{class_id}.jsonl"
    if attempts_path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        attempts_path.rename(ATTEMPTS / f"{class_id}.deleted-{stamp}.jsonl")


def list_classes() -> list[dict]:
    out = []
    for p in sorted(CLASSES.glob("*.json")):
        out.append(_normalize(store._read(p)))
    return out


def _find_by_passphrase(passphrase: str) -> dict | None:
    phrase = passphrase.strip().lower()
    for p in CLASSES.glob("*.json"):
        c = _normalize(store._read(p))
        if c.get("passphrase") == phrase:
            return c
    return None


def _find_by_passcode(code: str) -> tuple[dict, str] | None:
    """Resolve a per-student personal passcode to (class, canonical student name)."""
    phrase = code.strip().lower()
    for p in CLASSES.glob("*.json"):
        c = _normalize(store._read(p))
        for name, pc in (c.get("passcodes") or {}).items():
            if pc == phrase:
                return c, name
    return None


def resolve_code(code: str) -> dict | None:
    """Resolve a class passphrase OR a personal passcode to its class record — without the
    name/roster checks that join() does. Used by the network 'fetch assignment by code' path,
    where the host hands back the assignment for whatever class the code belongs to."""
    code = (code or "").strip().lower()
    if not code:
        return None
    hit = _find_by_passcode(code)
    if hit is not None:
        return hit[0]
    return _find_by_passphrase(code)


def fetch_authorized(c: dict, code: str, name: str | None) -> bool:
    """Whether the (code, name) pair may pull class `c`'s assignment bundle — the same
    credentialing join() applies, enforced before any questions go over the wire so they don't
    leak to a non-participant. Open classes: any code-holder. Roster classes: the name must be
    on the current roster. Passcode classes: the code must be a *personal passcode* that still
    maps to a roster name (the shared class passphrase does not get in, mirroring join())."""
    mode = c.get("mode")
    if mode == "roster":
        return on_roster(c, name)
    if mode == "passcode":
        hit = _find_by_passcode((code or "").strip().lower())
        return hit is not None and hit[0]["id"] == c["id"]
    return True  # open


def _assert_open(c: dict) -> None:
    state = effective_state(c)
    if state != "active":
        raise ValueError(_STATE_MESSAGE.get(state, "this class is not active"))


def join(passphrase: str, name: str) -> dict:
    code = passphrase.strip().lower()
    student = name.strip()

    # passcode mode: the student types their personal 2-word code and no name. Try that first
    # so a personal passcode never needs a separate "class code" field.
    hit = _find_by_passcode(code)
    if hit is not None:
        cls, student = hit
        _assert_open(cls)
        return {"class_id": cls["id"], "set_id": cls.get("set_id"),
                "set_ids": cls.get("set_ids", []), "student": student, "title": cls["title"]}

    cls = _find_by_passphrase(code)
    if cls is None:
        raise ValueError("unknown class code")
    _assert_open(cls)
    if cls["mode"] == "passcode":
        raise ValueError("this class uses personal passcodes — enter the one you were given")
    if not student:
        raise ValueError("Enter your name to join this classroom.")
    if cls["mode"] == "roster":
        if not on_roster(cls, student):
            raise ValueError("You are not on the roster for this classroom. "
                             "Check the spelling of your name, or ask your instructor to add you.")
        # canonicalize to the roster's spelling so attempt logs group under one name
        by_lower = {r.strip().lower(): r.strip() for r in cls.get("roster", [])}
        student = by_lower[student.lower()]
    return {"class_id": cls["id"], "set_id": cls.get("set_id"),
            "set_ids": cls.get("set_ids", []), "student": student, "title": cls["title"]}


# --------------------------------------------------------------------------- #
# attempts (append-only jsonl)
# --------------------------------------------------------------------------- #
def _uid(rec: dict) -> str:
    """Stable id for dedup across local + forwarded copies of the same attempt. Built from
    the record's own fields; any missing field contributes an empty string."""
    parts = [str(rec.get(k, "") or "") for k in
             ("ts", "student", "problem_id", "kind", "hint_level", "sql")]
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return digest[:12]


def append_attempt(class_id: str, rec: dict) -> dict:
    rec = {"ts": _now(), **rec}
    rec["uid"] = _uid(rec)
    path = ATTEMPTS / f"{class_id}.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def read_attempts(class_id: str) -> list[dict]:
    path = ATTEMPTS / f"{class_id}.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _rewrite_attempts(class_id: str, records: list[dict]) -> None:
    """Overwrite a class's attempt log with `records` (used by the deletion helpers). The log
    is normally append-only; deletion is an explicit instructor action, so we rewrite in full."""
    path = ATTEMPTS / f"{class_id}.jsonl"
    with path.open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def delete_student(class_id: str, student: str) -> int:
    """Remove every attempt logged under `student` (matched case-insensitively, the same way
    analytics group names) from this class's log. Returns the number of attempts removed."""
    key = student.strip().lower()
    attempts = read_attempts(class_id)
    kept = [a for a in attempts if a.get("student", "").strip().lower() != key]
    removed = len(attempts) - len(kept)
    if removed:
        _rewrite_attempts(class_id, kept)
    return removed


def delete_attempt(class_id: str, uid: str) -> bool:
    """Remove a single attempt by its uid (legacy records without one are matched by the uid
    computed from their own fields). Returns True if an attempt was removed."""
    attempts = read_attempts(class_id)
    kept = [a for a in attempts if (a.get("uid") or _uid(a)) != uid]
    removed = len(attempts) - len(kept)
    if removed:
        _rewrite_attempts(class_id, kept)
    return removed > 0


def append_attempts_dedup(class_id: str, records: list[dict]) -> tuple[int, int]:
    """Append only records whose uid isn't already present in this class's log. Records
    arriving without a uid get one computed from their own fields (ts is preserved as-is,
    never overwritten). Returns (accepted, duplicates)."""
    existing = {a["uid"] for a in read_attempts(class_id) if "uid" in a}
    path = ATTEMPTS / f"{class_id}.jsonl"
    accepted = 0
    duplicates = 0
    with path.open("a") as f:
        for rec in records:
            rec = dict(rec)
            uid = rec.get("uid") or _uid(rec)
            rec["uid"] = uid
            if uid in existing:
                duplicates += 1
                continue
            existing.add(uid)
            f.write(json.dumps(rec) + "\n")
            accepted += 1
    return accepted, duplicates
