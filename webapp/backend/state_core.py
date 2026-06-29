"""State-mode grading core — the DDL/DML sibling of `tutor_core.py`.

Where `tutor_core` grades a SELECT by comparing its *result set* against baked gold rows,
this module grades a CREATE/INSERT/UPDATE/DELETE/DROP statement (single or multi) by
comparing the *post-execution database state* against the gold's baked per-seed state.

The security spine is identical and inviolable: the gold SQL is read EXACTLY ONCE, at
bake (`bake_gold_state`). After baking, every runtime operation — grading, the structured
diff, hints, the leak guard — runs against the *baked per-seed gold state*, never the gold
statement. The baked bundle therefore never contains the gold statement, only its frozen
state, and `store.assert_student_safe` still passes by construction.

One extra honesty rule lives here, mirroring select-mode's "never show the student a gold
row they don't already have": the state diff sent to a student may carry counts, table
names, column names, and samples of the STUDENT's OWN extra rows — but NEVER samples of
gold-state rows the student is missing (those are counts only). `StateDiff.to_text` and
`diff_payload_state` both enforce this, so the hint model never sees a gold row either.

A "baked" state problem is JSON-serialisable:
    {
      "kind": "state",
      "seeds": [ {"seed": int, "state": <snapshot>}, ... ],
    }
where <snapshot> is the normalized DB state produced by `snapshot_state` (below).
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "tutor", ROOT / "populator", ROOT / "eval" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import sqlglot                          # noqa: E402  (a populator dependency)
from sqlglot import exp                 # noqa: E402

import grader                           # noqa: E402  tutor/  — build_db reused unchanged
import harness                          # noqa: E402  tutor/  — model call + leak helpers reused
from populate import load_populate      # noqa: E402  populator/

import tutor_core as tc                 # noqa: E402  RedactedProblem + _SchemaOnly

DEFAULT_SEEDS = tc.DEFAULT_SEEDS


# --------------------------------------------------------------------------- #
# 0. kind detection — is this gold a SELECT or a state-mutating statement?
# --------------------------------------------------------------------------- #
_STATE_NODES = (exp.Create, exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter)
_FIRST_KW = re.compile(r"^\s*([a-zA-Z]+)")
_STATE_KEYWORDS = {"create", "insert", "update", "delete", "drop", "alter", "replace"}


def detect_kind(gold_sql: str) -> str:
    """"select" iff every statement in the gold is a pure query; "state" if ANY statement
    creates/inserts/updates/deletes/drops/alters. Parses with sqlglot (multi-statement via
    `sqlglot.parse`); on total parse failure falls back to a per-statement first-keyword
    regex. Defaults to "select" so a misparse never silently turns a query into a state
    problem (fail-open toward the existing, well-tested path)."""
    try:
        parsed = sqlglot.parse(gold_sql, read="sqlite")
        statements = [s for s in parsed if s is not None]
        if statements:
            for st in statements:
                if isinstance(st, _STATE_NODES) or st.find(*_STATE_NODES):
                    return "state"
            return "select"
    except Exception:
        pass
    # regex fallback: split on ; and look at each statement's first keyword
    for chunk in gold_sql.split(";"):
        m = _FIRST_KW.match(chunk)
        if m and m.group(1).lower() in _STATE_KEYWORDS:
            return "state"
    return "select"


def _statement_kinds(gold_sql: str) -> list[str]:
    """Lowercase statement kinds ("create", "update", ...) for each statement in the gold.
    sqlglot first, first-keyword regex fallback — same fail-open posture as detect_kind."""
    kinds: list[str] = []
    try:
        for st in sqlglot.parse(gold_sql, read="sqlite"):
            if st is not None:
                kinds.append(type(st).__name__.lower())
        if kinds:
            return kinds
    except Exception:
        pass
    for chunk in gold_sql.split(";"):
        m = _FIRST_KW.match(chunk)
        if m:
            kinds.append(m.group(1).lower())
    return kinds


# --------------------------------------------------------------------------- #
# 1. SNAPSHOT — normalized, comparable DB state
# --------------------------------------------------------------------------- #
def _affinity(decl_type: str) -> str:
    """SQLite's five type-affinity rules (https://sqlite.org/datatype3.html §3.1), applied to
    a declared column type so `INT`/`INTEGER`/`BIGINT` all compare equal, `VARCHAR(40)`/`TEXT`
    both become TEXT, etc. Empty/unknown declared type -> BLOB (rule 4)."""
    t = (decl_type or "").upper()
    if "INT" in t:
        return "INTEGER"
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return "TEXT"
    if t == "" or "BLOB" in t:
        return "BLOB"
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return "REAL"
    return "NUMERIC"


def _canon_cell(v):
    """Round floats to 9 decimals so a float computed two ways still compares equal; leave
    everything else (int/str/bytes/None) as-is."""
    if isinstance(v, float):
        return round(v, 9)
    return v


def snapshot_state(conn: sqlite3.Connection) -> dict:
    """A normalized, JSON-serialisable picture of the whole DB state, built so that two states
    compare equal under the semantics the spec locks:

      * tables equal iff their column SETS match (declaration order does NOT matter) and their
        canonical rows match;
      * a row is canonicalized by projecting its columns in ALPHABETICAL column-name order
        (so reordering columns in a CREATE doesn't change row identity), rounding floats;
      * rows within a table are sorted by their JSON text (a multiset compared as a sorted list).

    Internal `sqlite_%` tables are skipped. The per-column tuple is
    [name_lower, affinity, notnull(0/1), pk(0/1), dflt(str|None)] in DECLARATION order (kept for
    display/diffing; equality uses the set of these tuples)."""
    out: dict = {"tables": {}}
    table_names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]
    for tname in table_names:
        info = conn.execute(f'PRAGMA table_info("{tname}")').fetchall()
        # info row: (cid, name, type, notnull, dflt_value, pk)
        columns = []
        col_names = []
        for cid, name, decl, notnull, dflt, pk in info:
            nm = (name or "").lower()
            col_names.append(name)
            columns.append([nm, _affinity(decl), int(notnull or 0),
                            1 if pk else 0, None if dflt is None else str(dflt)])
        # canonicalize rows: project columns in ALPHABETICAL name order, round floats
        alpha_idx = sorted(range(len(col_names)), key=lambda i: (col_names[i] or "").lower())
        col_list = ", ".join('"' + c + '"' for c in col_names)
        raw_rows = conn.execute(
            f'SELECT {col_list} FROM "{tname}"'
        ).fetchall() if col_names else []
        canon_rows = []
        for r in raw_rows:
            canon_rows.append([_canon_cell(r[i]) for i in alpha_idx])
        canon_rows.sort(key=lambda row: json.dumps(row, default=str, sort_keys=True))
        out["tables"][tname.lower()] = {
            "columns": columns,
            "rows": canon_rows,
            "n_rows": len(canon_rows),
        }
    return out


def _col_set(table_snap: dict) -> set:
    """The set of column tuples for set-equality comparison (declaration order ignored)."""
    return {tuple(c) for c in table_snap["columns"]}


def _tables_equal(a: dict, b: dict) -> bool:
    return _col_set(a) == _col_set(b) and a["rows"] == b["rows"]


# --------------------------------------------------------------------------- #
# 2. BAKE  (publish-time, gold SQL present — the ONLY place it is)
# --------------------------------------------------------------------------- #
def bake_gold_state(schema: str, gold_sql: str, generator_src: str,
                    seeds: list[int] | None = None) -> dict:
    """Build each seeded PRE database (schema may be "" — then an empty DB with the generator
    still applied), run the gold statement(s) via `executescript`, and freeze the resulting
    state. Output is student-safe: it holds baked state, never the statement that produced it.

    Gold errors RAISE here — publish must fail loudly rather than ship a broken problem."""
    seeds = seeds or DEFAULT_SEEDS
    populate = load_populate(generator_src)
    baked_seeds = []
    for s in seeds:
        conn = grader.build_db(tc._SchemaOnly(schema), populate, s)
        conn.executescript(gold_sql)   # gold errors propagate -> publish fails loudly
        conn.commit()
        baked_seeds.append({"seed": s, "state": snapshot_state(conn)})
        conn.close()
    return {"kind": "state", "seeds": baked_seeds}


# --------------------------------------------------------------------------- #
# 3. the structured state diff (deterministic, gold-free at runtime, redacted)
# --------------------------------------------------------------------------- #
@dataclass
class StateDiff:
    """How the student's post-state differs from the gold's, on one seed. Carries no gold
    statement and — by the honesty rule — no gold-only ROWS: missing rows are reported as
    COUNTS only (`n_missing`), while a sample is given ONLY for the student's OWN extra rows
    (`extra_sample`), which the student already has. Mirrors select-mode's redaction."""
    seed: int
    sql_error: str | None = None
    tables_missing: list = field(default_factory=list)   # in gold, not in student
    tables_extra: list = field(default_factory=list)     # in student, not in gold
    column_diffs: dict = field(default_factory=dict)     # {table: {"missing":[...], "extra":[...]}}
    row_diffs: dict = field(default_factory=dict)        # {table: {n_student,n_gold,n_missing,n_extra,extra_sample}}
    no_effect: bool = False                              # student post == student pre, gold's differs

    def to_text(self, cap: int = 5) -> str:
        """Human summary used in hint prompts and the L3 evidence. Respects the redaction rule:
        it NEVER prints a missing (gold-only) row — only its count — and only ever samples the
        student's own extra rows."""
        if self.sql_error:
            return f"Your statement did not run: {self.sql_error}"
        if self.no_effect:
            return ("Your statement left the database unchanged, but the correct answer changes "
                    "it. Re-read what the question asks you to add, modify, or remove.")
        L = ["Comparing the database state after your statement:"]
        if self.tables_missing:
            L.append(f"- table(s) the correct answer has but yours does not: "
                     f"{', '.join(self.tables_missing)}")
        if self.tables_extra:
            L.append(f"- table(s) present after your statement that should not be: "
                     f"{', '.join(self.tables_extra)}")
        for t, cd in self.column_diffs.items():
            bits = []
            if cd.get("missing"):
                bits.append(f"missing column(s) {', '.join(cd['missing'])}")
            if cd.get("extra"):
                bits.append(f"unexpected column(s) {', '.join(cd['extra'])}")
            if bits:
                L.append(f"- table `{t}`: {'; '.join(bits)}")
        for t, rd in self.row_diffs.items():
            seg = (f"- table `{t}`: has {rd['n_student']} row(s), the correct answer has "
                   f"{rd['n_gold']}")
            if rd.get("n_missing"):
                seg += f"; {rd['n_missing']} expected row(s) are absent"   # COUNT only — no sample
            if rd.get("n_extra"):
                sample = rd.get("extra_sample") or []
                shown = ", ".join(repr(tuple(r)) for r in sample[:cap])
                more = f" (+{rd['n_extra'] - len(sample[:cap])} more)" if rd["n_extra"] > len(sample[:cap]) else ""
                seg += f"; {rd['n_extra']} of your row(s) should not be there: {shown}{more}"
            L.append(seg)
        if len(L) == 1:
            L.append("- the resulting state differs.")
        return "\n".join(L)


@dataclass
class StateSeedResult:
    seed: int
    ok: bool
    diff: StateDiff | None = None


@dataclass
class StateGradeResult:
    """Mirrors `grader.GradeResult` closely enough for app.py: `.correct`, `.n_seeds`,
    `.per_seed` (each with `.ok`), `.first_fail` (carrying a `StateDiff`)."""
    problem_id: str
    correct: bool
    n_seeds: int
    per_seed: list
    first_fail: StateSeedResult | None


def _compare_states(seed: int, gold: dict, student: dict, pre: dict) -> StateSeedResult:
    """Compare a student's post-state to the gold post-state on one seed, building a redacted
    StateDiff. `pre` is the student's PRE state (to detect a no-op)."""
    gtabs, stabs = gold["tables"], student["tables"]
    gnames, snames = set(gtabs), set(stabs)
    tables_missing = sorted(gnames - snames)
    tables_extra = sorted(snames - gnames)

    column_diffs: dict = {}
    row_diffs: dict = {}
    for t in sorted(gnames & snames):
        gt, st = gtabs[t], stabs[t]
        gcols = {c[0] for c in gt["columns"]}
        scols = {c[0] for c in st["columns"]}
        miss_c = sorted(gcols - scols)
        extra_c = sorted(scols - gcols)
        if miss_c or extra_c or _col_set(gt) != _col_set(st):
            cd = {}
            if miss_c:
                cd["missing"] = miss_c
            if extra_c:
                cd["extra"] = extra_c
            # column SETS differ but names match -> a type/constraint difference; surface it
            if not cd and _col_set(gt) != _col_set(st):
                cd["missing"] = []   # placeholder so the table is flagged as differing
            column_diffs[t] = cd
        # row comparison (canonical multisets compared as sorted lists)
        if gt["rows"] != st["rows"]:
            from collections import Counter
            gc = Counter(json.dumps(r, default=str, sort_keys=True) for r in gt["rows"])
            sc = Counter(json.dumps(r, default=str, sort_keys=True) for r in st["rows"])
            n_missing = sum((gc - sc).values())   # gold-only rows: COUNT ONLY (redaction)
            extra_keys = list((sc - gc).elements())
            # rebuild the student's own extra rows for the sample (their data, allowed)
            srows_by_key: dict = {}
            for r in st["rows"]:
                srows_by_key.setdefault(json.dumps(r, default=str, sort_keys=True), []).append(r)
            extra_sample = []
            taken: dict = {}
            for k in extra_keys:
                idx = taken.get(k, 0)
                bucket = srows_by_key.get(k, [])
                if idx < len(bucket):
                    extra_sample.append(bucket[idx])
                    taken[k] = idx + 1
                if len(extra_sample) >= 5:
                    break
            row_diffs[t] = {
                "n_student": st["n_rows"], "n_gold": gt["n_rows"],
                "n_missing": n_missing, "n_extra": len(extra_keys),
                "extra_sample": extra_sample,   # STUDENT-only rows only
            }

    ok = (not tables_missing and not tables_extra and not column_diffs and not row_diffs)
    if ok:
        return StateSeedResult(seed, True)
    # no-effect detection: student didn't change anything, but gold does
    no_effect = (_states_equal(student, pre) and not _states_equal(gold, pre))
    return StateSeedResult(seed, False, StateDiff(
        seed=seed, tables_missing=tables_missing, tables_extra=tables_extra,
        column_diffs=column_diffs, row_diffs=row_diffs, no_effect=no_effect))


def _states_equal(a: dict, b: dict) -> bool:
    if set(a["tables"]) != set(b["tables"]):
        return False
    return all(_tables_equal(a["tables"][t], b["tables"][t]) for t in a["tables"])


# --------------------------------------------------------------------------- #
# 4. GRADE  (runtime, NO gold SQL — compares against baked gold state)
# --------------------------------------------------------------------------- #
def grade_baked_state(problem_id: str, schema: str, generator_src: str, baked: dict,
                      student_sql: str) -> StateGradeResult:
    """Grade a student statement against the baked gold STATE. Per seed: build a FRESH pre DB,
    snapshot it, `executescript` the student's SQL (errors caught, not raised), snapshot again,
    compare to the baked gold state. Correct iff every seed matches. No gold statement present."""
    populate = load_populate(generator_src)
    per: list = []
    first_fail = None
    for bs in baked["seeds"]:
        seed = bs["seed"]
        gold_state = bs["state"]
        conn = grader.build_db(tc._SchemaOnly(schema), populate, seed)
        pre_state = snapshot_state(conn)
        try:
            conn.executescript(student_sql)
            conn.commit()
        except Exception as e:   # sqlite3.Error and friends — a bad statement is "didn't run"
            conn.close()
            sr = StateSeedResult(seed, False, StateDiff(seed=seed, sql_error=str(e)))
        else:
            post_state = snapshot_state(conn)
            conn.close()
            sr = _compare_states(seed, gold_state, post_state, pre_state)
        per.append(sr)
        if not sr.ok and first_fail is None:
            first_fail = sr
    return StateGradeResult(problem_id, all(r.ok for r in per), len(per), per, first_fail)


# --------------------------------------------------------------------------- #
# 5. deterministic error-category chip (no LLM, no gold SQL)
# --------------------------------------------------------------------------- #
def error_category_state(gr: StateGradeResult) -> str | None:
    """One-line category of the mismatch, from the first failing diff. None when correct."""
    if gr.correct or not gr.first_fail or not gr.first_fail.diff:
        return None
    d = gr.first_fail.diff
    if d.sql_error:
        return "didn't run"
    if d.tables_missing:
        return "table missing"
    if d.tables_extra:
        return "unexpected table — should it be gone?"
    if d.column_diffs:
        return "wrong columns"
    if d.no_effect:
        return "no effect — your statement changed nothing"
    return "wrong rows"


# --------------------------------------------------------------------------- #
# 6. structured diff payload for the UI (deterministic, gold-free, redacted)
# --------------------------------------------------------------------------- #
def diff_payload_state(gr: StateGradeResult, cap: int = 5) -> dict | None:
    """The state diff the student UI renders. Carries counts, table/column names, and samples
    of the STUDENT's own extra rows only — never a gold-only row sample."""
    if gr.correct or not gr.first_fail or not gr.first_fail.diff:
        return None
    d = gr.first_fail.diff
    row_diffs = {}
    for t, rd in d.row_diffs.items():
        row_diffs[t] = {
            "n_student": rd["n_student"], "n_gold": rd["n_gold"],
            "n_missing": rd["n_missing"], "n_extra": rd["n_extra"],
            "extra_sample": [list(r) for r in (rd.get("extra_sample") or [])[:cap]],
        }
    return {
        "kind": "state",
        "family": family_for_state(gr),
        "seed": d.seed,
        "sql_error": d.sql_error,
        "tables_missing": list(d.tables_missing),
        "tables_extra": list(d.tables_extra),
        "column_diffs": d.column_diffs,
        "row_diffs": row_diffs,
        "no_effect": d.no_effect,
        "text": d.to_text(cap=cap),
    }


# --------------------------------------------------------------------------- #
# 7. run the student's OWN statement on seed #1 and echo their post-state
# --------------------------------------------------------------------------- #
def student_result_state(schema: str, generator_src: str, baked: dict,
                         student_sql: str, cap: int = 8) -> dict:
    """Run ONLY the student's SQL on seed #1's pre DB and return THEIR own resulting tables.
    Their database — no gold anywhere. Shape:
      {"kind":"state","tables":{name:{"cols":[...],"rows":[..cap..],"n_rows"}}} or
      {"kind":"state","error": str}."""
    if not baked.get("seeds"):
        return {"kind": "state", "tables": {}}
    populate = load_populate(generator_src)
    seed = baked["seeds"][0]["seed"]
    conn = grader.build_db(tc._SchemaOnly(schema), populate, seed)
    try:
        conn.executescript(student_sql)
        conn.commit()
    except Exception as e:   # sqlite3.Error and friends
        conn.close()
        return {"kind": "state", "error": str(e)}
    snap = snapshot_state(conn)
    conn.close()
    tables = {}
    for name, t in snap["tables"].items():
        tables[name] = {
            "cols": [c[0] for c in t["columns"]],
            "rows": [list(r) for r in t["rows"][:cap]],
            "n_rows": t["n_rows"],
        }
    return {"kind": "state", "tables": tables}


# --------------------------------------------------------------------------- #
# 8. HINT  (runtime, NO gold SQL — model sees only the redacted view + state diff)
#
# State family ladder (redesign 2026-06-28). The SELECT membership/structure split does not map
# cleanly onto state diffs, because the honesty rule forbids showing gold-only rows (counts
# only). So a "diff-first" rung is strong for SCHEMA errors (table/column names are safe to
# show) but partially blinded for ROW errors (we can only show counts + the student's OWN extra
# rows). The state families and their orderings:
#
#   family       trigger                          L1         L2          L3
#   -----------  -------------------------------  ---------  ----------  ----------
#   error        sql_error                        db_error   conceptual  directive
#   no_effect    statement changed nothing        diff*      socratic    directive   (* "no change")
#   schema       tables/columns differ            diff       socratic    conceptual
#   rows         only row contents differ         diff       socratic    directive
#
# Schema diffs are concrete and leak-free → diff-first mirrors SELECT membership. Row diffs are
# the one place state is weaker than SELECT (the diff can't reveal the missing gold rows) so we
# lead with the partial diff and END on a directive. no_effect's diff is just "you changed
# nothing"; the diff rung renders that, then socratic → directive. As in select-mode, the diff /
# db_error rungs are DETERMINISTIC (client renders them from the diff payload, no model call).
# --------------------------------------------------------------------------- #
_STATE_FAMILY_RUNGS = {
    "error":     ["db_error", "conceptual", "directive"],
    "no_effect": ["diff", "socratic", "directive"],
    "schema":    ["diff", "socratic", "conceptual"],
    "rows":      ["diff", "socratic", "directive"],
}


def family_for_state(gr: StateGradeResult) -> str | None:
    """Error-class family of a wrong state grade (None when correct)."""
    if gr.correct or not gr.first_fail or not gr.first_fail.diff:
        return None
    d = gr.first_fail.diff
    if d.sql_error:
        return "error"
    if d.no_effect:
        return "no_effect"
    if d.tables_missing or d.tables_extra or d.column_diffs:
        return "schema"
    return "rows"


def rung_plan_state(gr: StateGradeResult) -> list[str] | None:
    if gr.correct:
        return None
    return list(_STATE_FAMILY_RUNGS.get(family_for_state(gr) or "rows",
                                        _STATE_FAMILY_RUNGS["rows"]))


def primitive_at_state(gr: StateGradeResult, level: int) -> str:
    plan = rung_plan_state(gr) or _STATE_FAMILY_RUNGS["rows"]
    return plan[max(1, min(level, len(plan))) - 1]


_STATE_PRIMITIVE_RULES = {
    "socratic":
        "Give a SOCRATIC hint: ask ONE pointed question that leads the student to LOCATE what "
        "their statement did wrong to the data (e.g. 'which rows did your WHERE actually match?'). "
        "Ask a question only — do NOT state the fix, do NOT name a SQL clause, do NOT write SQL.",
    "conceptual":
        "Give a CONCEPTUAL hint: one sentence naming the KIND of thing that's wrong with how "
        "their statement changed (or failed to change) the data. Do NOT name a SQL clause or "
        "keyword, and do NOT write any SQL.",
    "directive":
        "Give a DIRECTIVE hint: in prose, name the specific statement or clause that is wrong "
        "(e.g. the WHERE on an UPDATE/DELETE, the column list of a CREATE, the table a DROP "
        "targets) AND the nature of the fix. Be concrete about WHAT to change and WHY. Do NOT "
        "write any runnable SQL and do NOT hand over a complete or near-complete statement.",
}

_STATE_SYSTEM = (
    "You are a Socratic SQL tutor helping a student fix THEIR data-modification statement "
    "(CREATE/INSERT/UPDATE/DELETE/DROP) without ever giving them the answer. You do not have "
    "the correct statement and must not invent and reveal one. You are given: the problem, the "
    "schema, the student's statement, and a precise diff describing how the resulting database "
    "STATE is wrong. Respond with ONLY the hint text — no preamble, no full solution."
)


def _build_state_hint_prompt(ctx: dict, student_sql: str, diff_text: str, primitive: str) -> str:
    parts = [
        _STATE_SYSTEM,
        "\n## Problem\n" + ctx["prompt"].strip(),
        "\n## Schema\n" + (ctx["schema"].strip() or "(no pre-existing tables)"),
        "\n## The student's statement\n" + student_sql.strip(),
        "\n## How the resulting database state is wrong (deterministic diff)\n" + diff_text.strip(),
        "\n## Your task\n" + _STATE_PRIMITIVE_RULES[primitive],
    ]
    return "\n".join(parts)


def render_state_diff_rung(gr: StateGradeResult) -> str:
    """The deterministic `diff`/`db_error` rung text — the already-redacted state diff (which
    also covers the no-effect and sql-error messaging). No model, cannot leak."""
    d = gr.first_fail.diff if gr.first_fail else None
    return d.to_text() if d else ""


def _offline_primitive_state(primitive: str, family: str | None) -> str:
    """Deterministic, LLM-free text for a state model primitive (socratic/conceptual/directive),
    keyed by family. Used as offline mode and as the leak-guard fallback; cannot leak."""
    if primitive == "socratic":
        if family == "error":
            return "Your statement didn't run — what do the table and column names in it refer to?"
        if family == "schema":
            return ("Look at the tables and columns the question describes — which one is "
                    "missing, extra, or shaped differently from what you produced?")
        if family == "no_effect":
            return "Your statement ran but changed nothing — which rows did you expect it to touch?"
        return ("Look at the rows that ended up wrong — which ones did your statement change (or "
                "leave alone) that it shouldn't have, and what do they have in common?")
    if primitive == "conceptual":
        if family == "error":
            return ("Your statement has a syntax or naming problem — it can't run yet, so fix that "
                    "before worrying about which rows it affects.")
        if family == "schema":
            return "The problem is the shape of the database — a table or column isn't as required."
        if family == "no_effect":
            return "Your statement didn't actually change the data the way the question needs."
        return ("You're changing too many, too few, or the wrong rows — the issue is which rows "
                "you target, not the table's shape.")
    # directive
    if family == "error":
        return ("Read the database error and fix it first: check that every table and column name "
                "exists and is spelled right, and that your value list matches the columns.")
    if family == "schema":
        return ("Revisit your CREATE/DROP/ALTER — a table or a column's name, type, or constraint "
                "(NOT NULL, PRIMARY KEY) doesn't match what the prompt requires. Fix that piece.")
    if family == "no_effect":
        return ("Revisit the WHERE (or the target) of your UPDATE/DELETE — it currently matches "
                "nothing, so the data is untouched. Make it select the rows the prompt describes.")
    return ("Revisit the WHERE clause (or the values) on your statement — it's writing or "
            "removing the wrong rows. Change that condition to match the rows the prompt targets.")


def generate_hint_state(problem: tc.RedactedProblem, gr: StateGradeResult, level: int,
                        student_sql: str, model: str | None) -> str:
    """One laddered state hint. Mirrors `harness.generate_hint`: deterministic diff/db_error
    rungs never call a model; model rungs use the pluggable model via `populator/model.py` (one
    retry, offline fallback). The model never sees gold state (the diff text is redacted)."""
    primitive = primitive_at_state(gr, level)
    family = family_for_state(gr)
    if primitive in ("diff", "db_error"):
        return render_state_diff_rung(gr)
    if model is None:
        return _offline_primitive_state(primitive, family)
    import model as model_mod   # populator/model.py — the same client harness.generate_hint uses
    diff_text = gr.first_fail.diff.to_text() if (gr.first_fail and gr.first_fail.diff) else ""
    ctx = {"prompt": problem.prompt, "schema": problem.schema}
    prompt = _build_state_hint_prompt(ctx, student_sql, diff_text, primitive)
    try:
        out = model_mod.call(model, prompt, max_retries=1)
        text = (out.get("text") or "").strip()
    except Exception:   # unreachable model -> offline within seconds
        text = ""
    return text or _offline_primitive_state(primitive, family)


# --------------------------------------------------------------------------- #
# 9. LEAK GUARD  (runtime, NO gold SQL — runs hint SQL vs baked gold state)
# --------------------------------------------------------------------------- #
def hint_leaks_state(problem_id: str, schema: str, generator_src: str, baked: dict,
                     hint_text: str) -> bool:
    """True if any runnable SQL inside the hint reproduces the gold STATE on ALL seeds. Reuses
    `harness._hint_leaks`, which only needs `.correct` on the grade result — satisfied by
    `StateGradeResult`. We also extract bare statement-mutating SQL the harness's SELECT-only
    extractor would miss."""
    grade_fn = lambda sql: grade_baked_state(problem_id, schema, generator_src, baked, sql)  # noqa: E731
    if harness._hint_leaks(grade_fn, hint_text):
        return True
    # harness.extract_sql only finds fenced blocks or bare SELECTs; a leaked DML/DDL statement
    # in prose would be missed, so scan for bare state statements too.
    for stmt in _extract_state_sql(hint_text):
        try:
            if grade_fn(stmt).correct:
                return True
        except Exception:
            continue
    return False


_STATE_STMT = re.compile(
    r"\b(?:CREATE|INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE)\b.+?(?:;|$)", re.S | re.I)


def _extract_state_sql(text: str) -> list[str]:
    """Bare CREATE/INSERT/UPDATE/DELETE/DROP/ALTER statements in hint prose (skeletons with
    `___` blanks are not runnable, so they are ignored)."""
    fenced = harness._SQL_BLOCK.findall(text or "")
    candidates = list(fenced) if fenced else [text or ""]
    out = []
    for c in candidates:
        for m in _STATE_STMT.finditer(c):
            stmt = m.group(0).strip()
            if "___" not in stmt:
                out.append(stmt)
    return out


# --------------------------------------------------------------------------- #
# 10. authoring helpers — state-aware schema check + deterministic coverage gates
# --------------------------------------------------------------------------- #
def schema_runs_state(ddl: str, gold_sql: str) -> tuple[bool, str]:
    """State-aware sibling of IF.schema_runs (which runs the gold as a query). Builds an empty
    DB from `ddl` and `executescript`s the gold; no error is the baseline requirement.

    The post != pre check only applies to statements that can change an EMPTY database
    (CREATE/DROP/ALTER/INSERT). An UPDATE or DELETE legitimately no-ops with zero rows, so
    demanding a state change here would reject every data-dependent gold — that requirement
    is enforced where it belongs, by the per-seed generator gates (which guarantee affected
    AND unaffected rows exist)."""
    try:
        conn = sqlite3.connect(":memory:")
        if ddl:
            conn.executescript(ddl)
    except sqlite3.Error as e:
        return False, f"DDL error: {e}"
    try:
        pre = snapshot_state(conn)
        conn.executescript(gold_sql)
        conn.commit()
        post = snapshot_state(conn)
    except sqlite3.Error as e:
        return False, f"gold statement does not run on inferred schema: {e}"
    data_independent = any(
        t in ("create", "drop", "alter", "insert") for t in _statement_kinds(gold_sql))
    changed = not _states_equal(pre, post)
    conn.close()
    if data_independent and not changed:
        return False, "gold statement does not change the state on the inferred schema"
    return True, "ok"


def gold_only_creates(gold_sql: str) -> bool:
    """True iff the gold references no pre-existing tables — i.e. every statement is a CREATE
    (and any INSERT/UPDATE/DELETE/DROP only touches a table the same gold CREATEd). Then the
    PRE-schema can be "" (empty DB). Fail-open: on parse trouble, return False (infer a schema)."""
    try:
        statements = [s for s in sqlglot.parse(gold_sql, read="sqlite") if s is not None]
    except Exception:
        return False
    if not statements:
        return False
    created = set()
    for st in statements:
        if isinstance(st, exp.Create):
            tbl = st.find(exp.Table)
            if tbl is not None:
                created.add(tbl.name.lower())
            continue
        # any other statement type must target only already-created tables
        targets = {t.name.lower() for t in st.find_all(exp.Table)}
        if not targets <= created:
            return False
        if isinstance(st, (exp.Drop, exp.Alter)):
            # a DROP/ALTER of a just-created table still references only created tables; allow it
            continue
        if not isinstance(st, (exp.Insert, exp.Update, exp.Delete)):
            return False
    return True


# --- the deterministic coverage gates (the DML "edge-case coverage" invariant) ------------- #
def make_state_gates(gold_sql: str):
    """Return a per-seed validation callable `gate(pre_conn_factory) -> (ok, why)` derived from
    the gold's statement types. Fail-open: anything outside this catalog is skipped, never
    crashes. Every gold run must change the state; plus per-statement-type gates:

      * UPDATE ... WHERE : target table has >=1 changed AND >=1 unchanged row
      * DELETE ... WHERE : >=1 deleted AND >=1 surviving row in target
      * INSERT           : target row count strictly increases
      * CREATE TABLE x   : x absent before, present after
      * DROP TABLE x     : x present before, absent after, >=1 OTHER table survives if any existed

    `pre_conn_factory()` must return a FRESH populated pre-DB connection (so each gate can run
    the gold on its own copy and inspect before/after)."""
    try:
        statements = [s for s in sqlglot.parse(gold_sql, read="sqlite") if s is not None]
    except Exception:
        statements = []

    def _tnames(conn):
        return {r[0].lower() for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}

    def _count(conn, t):
        try:
            return conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error:
            return None

    def gate(pre_conn_factory) -> tuple[bool, str]:
        # universal gate: the gold must change the state
        conn = pre_conn_factory()
        pre = snapshot_state(conn)
        try:
            conn.executescript(gold_sql)
            conn.commit()
        except sqlite3.Error as e:
            conn.close()
            return False, f"gold statement failed on generated data: {e}"
        post = snapshot_state(conn)
        if _states_equal(pre, post):
            conn.close()
            return False, "gold statement does not change the state on this seed"
        conn.close()

        for st in statements:
            # ---- UPDATE ... WHERE: changed + unchanged rows in target ----
            if isinstance(st, exp.Update) and st.args.get("where") is not None:
                tbl = st.find(exp.Table)
                if tbl is None:
                    continue
                t = tbl.name
                where_sql = st.args["where"].sql(dialect="sqlite")  # includes "WHERE ..."
                c = pre_conn_factory()
                try:
                    matched = c.execute(f'SELECT COUNT(*) FROM "{t}" {where_sql}').fetchone()[0]
                    total = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except sqlite3.Error:
                    c.close()
                    continue   # fail-open: can't evaluate -> skip this gate
                c.close()
                if matched < 1:
                    return False, f"UPDATE matches no rows in {t} (need >=1 changed)"
                if total - matched < 1:
                    return False, f"UPDATE matches every row in {t} (need >=1 unchanged)"

            # ---- DELETE ... WHERE: deleted + surviving rows in target ----
            elif isinstance(st, exp.Delete) and st.args.get("where") is not None:
                tbl = st.find(exp.Table)
                if tbl is None:
                    continue
                t = tbl.name
                where_sql = st.args["where"].sql(dialect="sqlite")
                c = pre_conn_factory()
                try:
                    matched = c.execute(f'SELECT COUNT(*) FROM "{t}" {where_sql}').fetchone()[0]
                    total = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except sqlite3.Error:
                    c.close()
                    continue
                c.close()
                if matched < 1:
                    return False, f"DELETE matches no rows in {t} (need >=1 deleted)"
                if total - matched < 1:
                    return False, f"DELETE matches every row in {t} (need >=1 surviving)"

            # ---- INSERT: target row count strictly increases ----
            elif isinstance(st, exp.Insert):
                tbl = st.find(exp.Table)
                if tbl is None:
                    continue
                t = tbl.name
                c_pre = pre_conn_factory()
                before = _count(c_pre, t)
                c_pre.close()
                c_post = pre_conn_factory()
                try:
                    c_post.executescript(gold_sql)
                    c_post.commit()
                    after = _count(c_post, t)
                except sqlite3.Error:
                    c_post.close()
                    continue
                c_post.close()
                if before is None or after is None or after <= before:
                    return False, f"INSERT does not increase row count of {t}"

            # ---- CREATE TABLE x: absent before, present after ----
            elif isinstance(st, exp.Create):
                tbl = st.find(exp.Table)
                if tbl is None:
                    continue
                x = tbl.name.lower()
                c = pre_conn_factory()
                before_tabs = _tnames(c)
                try:
                    c.executescript(gold_sql)
                    c.commit()
                    after_tabs = _tnames(c)
                except sqlite3.Error:
                    c.close()
                    continue
                c.close()
                if x in before_tabs:
                    return False, f"CREATE target {x} already exists before the statement"
                if x not in after_tabs:
                    return False, f"CREATE target {x} is absent after the statement"

            # ---- DROP TABLE x: present before, absent after, another survives ----
            elif isinstance(st, exp.Drop):
                tbl = st.find(exp.Table)
                if tbl is None:
                    continue
                x = tbl.name.lower()
                c = pre_conn_factory()
                before_tabs = _tnames(c)
                try:
                    c.executescript(gold_sql)
                    c.commit()
                    after_tabs = _tnames(c)
                except sqlite3.Error:
                    c.close()
                    continue
                c.close()
                if x not in before_tabs:
                    return False, f"DROP target {x} is not present before the statement"
                if x in after_tabs:
                    return False, f"DROP target {x} still present after the statement"
                if before_tabs - {x} and not (after_tabs):
                    return False, "DROP removed the only surviving table (need another to survive)"
        return True, "ok"

    return gate
