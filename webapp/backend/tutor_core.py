"""The web app's grading/hint core — a thin, gold-SQL-free layer over the shipped
deterministic grader (`tutor/grader.py`) and tutor harness (`tutor/harness.py`).

The whole security argument of the product lives in one fact this module makes literal:

    the gold SQL is touched EXACTLY ONCE, at publish/bake time (`bake_gold`).

After baking, every runtime operation a student can trigger — grading, the structured diff,
hint generation, and the leak guard — runs against the *baked per-seed gold results*, never
the gold query. So nothing downstream of `bake_gold` can leak the answer, because the answer
(as SQL) is not present downstream of `bake_gold`. This is the project's "the model never
sees the gold query" spine, extended to "the student bundle never contains it either."

A "baked" problem is JSON-serialisable:
    {
      "ordered": bool,                      # gold has a top-level ORDER BY
      "seeds": [ {"seed": int, "cols": [str], "rows": [[cell, ...], ...]}, ... ],
    }
Rows are lists in JSON; we re-tuple them on load so the grader's multiset/equality
comparison (which hashes rows) works unchanged.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "tutor", ROOT / "populator", ROOT / "eval" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import grader   # noqa: E402  tutor/  — deterministic primitives, reused unchanged
import harness  # noqa: E402  tutor/  — hint prompt + level rules + extract_sql, reused
from populate import load_populate  # noqa: E402  populator/

DEFAULT_SEEDS = grader.DEFAULT_SEEDS  # K=10 grading seeds


# --------------------------------------------------------------------------- #
# a redacted, gold-free problem view — everything the runtime is allowed to hold
# --------------------------------------------------------------------------- #
@dataclass
class RedactedProblem:
    """The runtime problem object. Deliberately has NO `gold_sql` field. Carries exactly the
    fields `harness.model_context` is allowed to show the hint model, plus the schema/title the
    grader and UI need. Constructing one cannot accidentally smuggle the gold query."""
    id: str
    title: str
    difficulty: str
    prompt: str
    schema: str
    target_clauses: list[str]


# --------------------------------------------------------------------------- #
# 1. BAKE  (publish-time, gold SQL present — the only place it is)
# --------------------------------------------------------------------------- #
def bake_gold(schema: str, gold_sql: str, generator_src: str,
              seeds: list[int] | None = None) -> dict:
    """Run the gold query on each seeded DB and freeze its result. Output is student-safe:
    it contains result rows, never the query that produced them."""
    seeds = seeds or DEFAULT_SEEDS
    populate = load_populate(generator_src)
    ordered = grader.order_sensitive(gold_sql)
    baked_seeds = []
    total_order = True   # does the gold ORDER BY impose a TOTAL order (stable under permutation)?
    for s in seeds:
        conn = grader.build_db(_SchemaOnly(schema), populate, s)
        cols, rows = grader.run_query(conn, gold_sql)
        if ordered:
            _permute_db(conn)
            _, rows_perm = grader.run_query(conn, gold_sql)
            if rows_perm != rows:
                total_order = False   # gold itself leaves ties unresolved on this seed
        conn.close()
        baked_seeds.append({"seed": s, "cols": list(cols),
                            "rows": [list(r) for r in rows]})
    return {"ordered": ordered, "total_order": total_order, "seeds": baked_seeds}


class _SchemaOnly:
    """grader.build_db only reads `.schema` off the problem; this lets bake/grade build DBs
    without constructing a full Problem (and without a gold_sql field existing at all)."""
    __slots__ = ("schema",)

    def __init__(self, schema: str):
        self.schema = schema


# --------------------------------------------------------------------------- #
# ordering-ambiguity defense: permute a built DB's storage order
# --------------------------------------------------------------------------- #
_permute_db = grader.permute_db   # canonical implementation lives in tutor/grader.py


# --------------------------------------------------------------------------- #
# 2. GRADE  (runtime, NO gold SQL — compares against baked results)
# --------------------------------------------------------------------------- #
def grade_baked(problem_id: str, schema: str, generator_src: str, baked: dict,
                student_sql: str, enforce_column_names: bool = False) -> grader.GradeResult:
    """Grade a student query against the baked gold results. Reuses the grader's exact per-seed
    comparison (`grader._compare`) and result types — so a query graded here is identical to one
    graded against the live gold query, minus the gold query.

    When `enforce_column_names` is set (an instructor per-question switch), the student's result
    headers must also match the gold's column names — the required names are the gold's own baked
    column headers, so the instructor never types them. Off by default and backward-compatible."""
    populate = load_populate(generator_src)
    ordered = baked["ordered"]
    # only enforce a strict order when the gold ORDER BY actually imposes a total one; if gold
    # itself leaves ties, comparing exact sequences would be arbitrary, so we don't.
    total_order = ordered and baked.get("total_order", True)
    prob = _SchemaOnly(schema)
    per: list[grader.SeedResult] = []
    first_fail = None
    for bs in baked["seeds"]:
        seed = bs["seed"]
        gold_cols = list(bs["cols"])
        gold_rows = [tuple(r) for r in bs["rows"]]
        # required names come straight from the gold's headers when enforcement is on
        required_columns = gold_cols if enforce_column_names else None
        conn = grader.build_db(prob, populate, seed)
        try:
            stu_cols, stu_rows = grader.run_query(conn, student_sql)
        except Exception as e:  # sqlite3.Error and friends
            sr = grader.SeedResult(seed, False,
                grader.Diff(seed, ordered, len(gold_cols), 0, len(gold_rows), 0,
                            gold_cols=gold_cols, sql_error=str(e)))
        else:
            sr = grader._compare(seed, gold_cols, gold_rows, stu_cols, stu_rows, ordered,
                                 required_columns=required_columns)
            # ordering-ambiguity guard: a query that matches only because of incidental storage
            # order must ALSO match after the rows are permuted. If it doesn't, its ORDER BY is
            # under-specified (e.g. a missing tie-break) — report it as an ordering error.
            if sr.ok and total_order:
                _permute_db(conn)
                try:
                    pc, pr = grader.run_query(conn, student_sql)
                except Exception:
                    pc, pr = stu_cols, stu_rows
                if pr != gold_rows:
                    sr = grader.SeedResult(seed, False,
                        grader.Diff(seed, ordered, len(gold_cols), len(pc),
                                    len(gold_rows), len(pr), ordering_only=True))
        conn.close()
        per.append(sr)
        if not sr.ok and first_fail is None:
            first_fail = sr
    return grader.GradeResult(problem_id, all(r.ok for r in per), len(per), per, first_fail)


# --------------------------------------------------------------------------- #
# 3. HINT  (runtime, NO gold SQL — model sees only the redacted view + diff)
# --------------------------------------------------------------------------- #
def make_hint(problem: RedactedProblem, gr: grader.GradeResult, level: int,
              student_sql: str, model: str | None) -> str:
    """One laddered hint. Delegates to the shipped `harness.generate_hint`, which builds the
    model-safe context (no gold) and the level rule. `model=None` -> offline templated hint."""
    return harness.generate_hint(problem, gr, level, student_sql, model=model)


# --------------------------------------------------------------------------- #
# 4. LEAK GUARD  (runtime, NO gold SQL — runs hint SQL vs baked gold results)
# --------------------------------------------------------------------------- #
def hint_leaks(problem_id: str, schema: str, generator_src: str, baked: dict,
               hint_text: str) -> bool:
    """True if any runnable SQL inside the hint reproduces the gold result on ALL seeds. Reuses
    the harness leak test, but grades via `grade_baked` so it too needs no gold query."""
    grade_fn = lambda sql: grade_baked(problem_id, schema, generator_src, baked, sql)  # noqa: E731
    return harness._hint_leaks(grade_fn, hint_text)


# --------------------------------------------------------------------------- #
# small helper: deterministic error-category chip (no LLM, no gold SQL)
# --------------------------------------------------------------------------- #
def error_category(gr: grader.GradeResult) -> str | None:
    """One-line category of the mismatch, derived ONLY from the first failing diff.
    None when correct. Purely a function of result-set facts already in `Diff`."""
    if gr.correct or not gr.first_fail or not gr.first_fail.diff:
        return None
    d = gr.first_fail.diff
    if d.sql_error:
        return "didn't run"
    if d.ordering_only:
        return "ordering"
    if d.gold_ncols != d.student_ncols:
        return "wrong columns"
    # right values/shape but a pinned column NAME is missing (enforce_column_names)
    if getattr(d, "required_columns_missing", None):
        return "wrong column names"
    has_missing, has_extra = bool(d.missing), bool(d.extra)
    if has_extra and not has_missing:
        return "extra rows — filter too loose?"
    if has_missing and not has_extra:
        return "missing rows — filter too strict?"
    if d.gold_nrows == d.student_nrows and has_missing and has_extra:
        return "values differ — check calculations"
    return "rows differ"


# --------------------------------------------------------------------------- #
# small helper: run the student's own query on seed #1 (no gold anywhere)
# --------------------------------------------------------------------------- #
def student_result(schema: str, generator_src: str, baked: dict,
                   student_sql: str, cap: int = 20) -> dict | None:
    """Run ONLY the student's SQL against the first baked seed's DB and return what they
    got back: {"cols", "rows" (capped), "n_rows"} or {"error": str} on SQL error. Touches
    no gold result — purely a convenience echo of the student's own output."""
    if not baked.get("seeds"):
        return None
    populate = load_populate(generator_src)
    seed = baked["seeds"][0]["seed"]
    conn = grader.build_db(_SchemaOnly(schema), populate, seed)
    try:
        cols, rows = grader.run_query(conn, student_sql)
    except Exception as e:  # sqlite3.Error and friends
        return {"error": str(e)}
    finally:
        conn.close()
    return {"cols": list(cols), "rows": [list(r) for r in rows[:cap]], "n_rows": len(rows)}


# --------------------------------------------------------------------------- #
# small helper: structured diff payload for the UI (deterministic, gold-free)
# --------------------------------------------------------------------------- #
def diff_payload(gr: grader.GradeResult, cap: int = 5) -> dict | None:
    """The diff the student UI renders. Pulled from the first failing seed; carries only
    result-set facts (row/col counts, sample missing/extra rows, ordering/SQL-error flags),
    plus the redesign's column headers + header annotations and the error-class family."""
    if gr.correct or not gr.first_fail or not gr.first_fail.diff:
        return None
    d = gr.first_fail.diff
    family = grader.classify_family(d)
    return {
        "seed": d.seed,
        "ordered": d.ordered,
        "sql_error": d.sql_error,
        "ordering_only": d.ordering_only,
        "family": family,
        "gold_ncols": d.gold_ncols, "student_ncols": d.student_ncols,
        "gold_nrows": d.gold_nrows, "student_nrows": d.student_nrows,
        # column headers so the renderer can align by name and color only common columns (§4.2)
        "gold_cols": list(d.gold_cols), "student_cols": list(d.student_cols),
        "header_notes": d.header_annotations(),
        "required_columns_missing": list(d.required_columns_missing),
        "missing": [list(r) for r in d.missing[:cap]],
        "extra": [list(r) for r in d.extra[:cap]],
        "missing_total": len(d.missing), "extra_total": len(d.extra),
        "text": d.to_text(cap=cap, family=family),
    }


# =========================================================================== #
# KIND-AWARE DISPATCH
#
# A bundle problem dict `p` may now carry a "kind" ("select" | "state"). These thin
# dispatchers branch on `p.get("kind", "select")` so existing SELECT problems take exactly
# the path above (byte-identical responses) while state problems route to `state_core`. The
# app layer calls only these, so it stays kind-agnostic. `state_core` imports this module, so
# we import it lazily inside the dispatchers to avoid an import cycle at module load.
# =========================================================================== #
def grade_problem(p: dict, sql: str):
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.grade_baked_state(p["id"], p["schema"], p["generator_src"], p["baked"], sql)
    return grade_baked(p["id"], p["schema"], p["generator_src"], p["baked"], sql,
                       enforce_column_names=bool(p.get("enforce_column_names", False)))


def student_result_for(p: dict, sql: str) -> dict | None:
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.student_result_state(p["schema"], p["generator_src"], p["baked"], sql)
    return student_result(p["schema"], p["generator_src"], p["baked"], sql)


def category_for(p: dict, gr) -> str | None:
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.error_category_state(gr)
    return error_category(gr)


def diff_payload_for(p: dict, gr) -> dict | None:
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.diff_payload_state(gr)
    return diff_payload(gr)


def hint_for(p: dict, redacted: "RedactedProblem", gr, level: int,
             sql: str, model: str | None) -> str:
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.generate_hint_state(redacted, gr, level, sql, model)
    return make_hint(redacted, gr, level, sql, model)


# --- family-adaptive ladder plumbing (redesign §3) — kind-aware ------------- #
def family_for(p: dict, gr) -> str | None:
    """Error-class family of the first failing grade (None when correct). Drives the ladder
    ordering and lets the UI label rungs ahead of time."""
    if gr.correct:
        return None
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.family_for_state(gr)
    return harness.family_for(gr)


def rung_plan(p: dict, gr) -> list[str] | None:
    """The primitive sitting at each of L1..L3 for this grade's family (None when correct).
    The UI renders rung labels from this and knows which rungs are the deterministic diff."""
    if gr.correct:
        return None
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.rung_plan_state(gr)
    return [harness.primitive_at(gr, lv) for lv in (1, 2, 3)]


def primitive_at(p: dict, gr, level: int) -> str:
    """Primitive for a single rung. The hint endpoint uses this to decide whether a rung is the
    deterministic diff/db_error (no model call, client renders) or a model rung."""
    plan = rung_plan(p, gr) or []
    idx = max(1, min(level, len(plan) or 1)) - 1
    return plan[idx] if plan else "conceptual"


# the deterministic (no-model, client-rendered) primitives, shared by app.py's hint endpoint
DETERMINISTIC_PRIMITIVES = ("diff", "db_error")


def hint_leaks_for(p: dict, hint_text: str) -> bool:
    if p.get("kind", "select") == "state":
        import state_core as sc
        return sc.hint_leaks_state(p["id"], p["schema"], p["generator_src"], p["baked"], hint_text)
    return hint_leaks(p["id"], p["schema"], p["generator_src"], p["baked"], hint_text)
