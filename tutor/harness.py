"""Phase 4 — the tutor harness (no UI).

The orchestration around the deterministic grader: take a student's query, grade it
(Phase 3), and if it is wrong, produce a single laddered hint.  This is the agentic loop
only — there is no frontend here; `TutorSession` is a programmatic API a CLI or web layer
could drive.

Security spine (the project's core invariant): the model that writes the hint NEVER sees
the gold query.  This is enforced structurally — `model_context()` builds the only view
of a problem the model is allowed, and it omits `gold_sql` (and the check source).  The
model reasons solely over the problem prompt, schema, target clauses, the student's query,
the student's result, and the deterministic diff string from the grader.  Leaking *the*
answer is therefore impossible by construction; the only residual risk is the model
*deriving* a correct query inside a hint, which the leakage guard below catches by
execution (full Phase 5 will expand it).

Hint laddering:
  L1 — conceptual nudge (what kind of thing is wrong; no clause named)
  L2 — name the clause/operation to revisit
  L3 — a skeleton with blanks (structure, never the gold query)

One model call per turn: the diff is deterministic, so there is no second "grading" call.
The harness is model-pluggable via populator/model.py; an offline templated mode lets the
whole loop run and be tested without any LLM.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import grader  # local (same dir)

_POP = Path(__file__).resolve().parents[1] / "populator"
if str(_POP) not in sys.path:
    sys.path.insert(0, str(_POP))
from problems import bank  # noqa: E402

MAX_LEVEL = 3


# --------------------------------------------------------------------------- #
# the redacted, model-safe view of a problem  (NO gold_sql, NO check source)
# --------------------------------------------------------------------------- #
def model_context(problem) -> dict:
    """The ONLY problem fields the hint model is allowed to see."""
    return {
        "id": problem.id,
        "difficulty": problem.difficulty,
        "prompt": problem.prompt,
        "schema": problem.schema,
        "target_clauses": list(problem.target_clauses),
    }


# --------------------------------------------------------------------------- #
# prompt construction for the single constrained hint call
# --------------------------------------------------------------------------- #
_LEVEL_RULES = {
    1: "Give a LEVEL 1 hint: a one-sentence conceptual nudge about what KIND of thing is "
       "wrong. Do NOT name a SQL clause or keyword. Do NOT write any SQL.",
    2: "Give a LEVEL 2 hint: name the specific SQL clause or operation the student should "
       "revisit, and why, in 1-2 sentences. Do NOT write a full query or the answer.",
    3: "Give a LEVEL 3 hint: show ONLY the structural SHAPE of the query as a skeleton — which "
       "clauses appear and in what order — with EVERY meaningful choice left as a blank `___`. "
       "Blank out all column names, every aggregate/function name, all literals and values, and "
       "any sort direction (ASC/DESC). CRITICAL: the specific token the student got wrong (per "
       "the diff) MUST be a blank — never fill in the exact piece they are missing, or you have "
       "handed them the answer. A correct skeleton looks like `SELECT ___, ___(___) FROM ___ "
       "GROUP BY ___ ORDER BY ___ LIMIT ___` — clause keywords only, everything else blank. "
       "Never write the complete or near-complete query.",
}

_SYSTEM = (
    "You are a Socratic SQL tutor. You help a student fix THEIR query without ever giving "
    "them the answer. You do not have the correct query and must not invent and reveal one. "
    "You are given: the problem, the schema, the SQL features it targets, the student's "
    "query, their result, and a precise diff describing how their result is wrong. "
    "Respond with ONLY the hint text — no preamble, no full solution."
)


def build_hint_prompt(ctx: dict, student_sql: str, diff_text: str, level: int) -> str:
    parts = [
        _SYSTEM,
        "\n## Problem\n" + ctx["prompt"].strip(),
        "\n## Schema\n" + ctx["schema"].strip(),
        "\n## SQL features this problem is meant to exercise\n- "
        + "\n- ".join(ctx["target_clauses"]),
        "\n## The student's query\n" + student_sql.strip(),
        "\n## How the student's result is wrong (deterministic diff)\n" + diff_text.strip(),
        "\n## Your task\n" + _LEVEL_RULES[level],
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# hint generation — model-backed or offline templated
# --------------------------------------------------------------------------- #
def _offline_hint(ctx: dict, gr: grader.GradeResult, level: int) -> str:
    """Deterministic, LLM-free hint from the diff. Lets the harness run & be tested with
    no model, and never emits the answer."""
    diff = gr.first_fail.diff if gr.first_fail else None
    clauses = ctx["target_clauses"]
    if diff and diff.sql_error:
        if level == 1:
            return "Your query doesn't run yet — read the database error and check your column and table names."
        if level == 2:
            return f"There's a SQL error to fix first: {diff.sql_error}"
        return "Fix the error, then build up the query piece by piece: `SELECT ___ FROM ___ WHERE ___`."
    if diff and diff.ordering_only:
        if level == 1:
            return "You're selecting the right rows, but think about the order they come out in."
        if level == 2:
            return "Revisit your ORDER BY — the problem asks for a specific sort (and tie-breaking) you're not applying."
        return "Try: `... ORDER BY ___ <ASC|DESC>, ___ <ASC|DESC>` to match the required ordering."
    # row-content mismatch
    if level == 1:
        return ("Your result set doesn't match: you're including or excluding the wrong rows. "
                "Re-read which rows the question actually asks for.")
    if level == 2:
        return ("Look at how you filter and aggregate — revisit: "
                + ", ".join(clauses[:3]) + ". One of these isn't doing what the prompt requires.")
    return ("Sketch the shape first: `SELECT ___ FROM ___ "
            + ("GROUP BY ___ " if any("GROUP" in c.upper() for c in clauses) else "WHERE ___ ")
            + "` and fill the blanks from the prompt — don't copy any answer.")


def generate_hint(problem, gr: grader.GradeResult, level: int, student_sql: str,
                  model: str | None = None) -> str:
    """Produce a single hint at `level`. If `model` is None, use the offline templated
    hint; otherwise call the pluggable model (gold query never included)."""
    ctx = model_context(problem)
    if model is None:
        return _offline_hint(ctx, gr, level)
    import model as model_mod  # populator/model.py
    prompt = build_hint_prompt(ctx, student_sql, gr.diff_text, level)
    # hints are interactive: one retry, not the authoring loop's long exponential backoff —
    # if the model is unreachable we fall back to the offline templated hint within seconds
    out = model_mod.call(model, prompt, max_retries=1)
    return (out.get("text") or "").strip() or _offline_hint(ctx, gr, level)


# --------------------------------------------------------------------------- #
# leakage guard (minimal; full verifier is Phase 5)
# --------------------------------------------------------------------------- #
_SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.S | re.I)
_SELECT = re.compile(r"\bSELECT\b.+?(?:;|$)", re.S | re.I)


def extract_sql(text: str) -> list[str]:
    blocks = [m.group(1).strip() for m in _SQL_BLOCK.finditer(text)]
    if blocks:
        return blocks
    # fall back to bare SELECT ... statements (skeletons with ___ are ignored: not runnable)
    return [m.group(0).strip() for m in _SELECT.finditer(text) if "___" not in m.group(0)]


def _hint_leaks(grade_fn, hint_text: str) -> bool:
    """Core leak test: True if any runnable SQL in the hint, graded via `grade_fn`, reproduces
    the gold result on ALL seeds (the hint handed over a working query). `grade_fn(sql)` ->
    GradeResult. Skeletons with `___` blanks are not runnable, so they are not flagged here —
    that residual (a near-complete skeleton) is handled by the L3 prompt rule, not this guard."""
    for sql in extract_sql(hint_text):
        try:
            if grade_fn(sql).correct:
                return True
        except Exception:
            continue  # non-runnable / partial SQL is not a leak
    return False


def leaks_answer(pid: str, hint_text: str, seeds: list[int] | None = None) -> bool:
    """Leak check for a BANK problem (loads the problem + frozen generator by id)."""
    return _hint_leaks(lambda sql: grader.grade(pid, sql, seeds), hint_text)


def leaks_answer_for(problem, populate, hint_text: str,
                     seeds: list[int] | None = None) -> bool:
    """Leak check for an AD-HOC (instructor-authored) problem — one not in the bank. Same
    execution test, but grades against the supplied (problem, populate) instead of by id."""
    return _hint_leaks(lambda sql: grader.grade_problem(problem, populate, sql, seeds), hint_text)


# --------------------------------------------------------------------------- #
# the session
# --------------------------------------------------------------------------- #
@dataclass
class Turn:
    student_sql: str
    correct: bool
    level: int | None          # hint level given (None if correct)
    hint: str | None
    diff_text: str
    leaked: bool


class TutorSession:
    """Drives one student through one problem. Escalates the hint level on each wrong
    submission (capped at L3). Records turns for the Phase-6 evaluation metrics.

    Two ways to construct, so the SAME session drives both research and the product:
      * a BANK problem by id      -> `TutorSession("p01_between")`              (loads the frozen generator)
      * an AD-HOC problem object  -> `TutorSession(problem=prob, populate=gen)` (an instructor just
        authored it; it is not in the bank). `populate` is its `populate(conn, seed)` generator.
    Either way the model still NEVER sees the gold query (generate_hint redacts it)."""

    def __init__(self, problem=None, model: str | None = None,
                 seeds: list[int] | None = None, guard_leaks: bool = True,
                 populate=None, pid: str | None = None):
        if isinstance(problem, str):          # back-compat: TutorSession("p01_between")
            pid, problem = problem, None
        if problem is not None:               # ad-hoc instructor-authored problem
            if populate is None:
                raise ValueError("ad-hoc TutorSession(problem=...) requires populate=...")
            self.problem, self.populate, self.pid = problem, populate, problem.id
        else:                                 # bank problem by id
            self.problem, self.populate, self.pid = bank.get(pid), None, pid
        self.model = model
        self.seeds = seeds
        self.guard_leaks = guard_leaks
        self.level = 0
        self.turns: list[Turn] = []

    # grade / leak-check via the bank-id path or the ad-hoc (problem, populate) path
    def _grade(self, sql: str) -> grader.GradeResult:
        if self.populate is not None:
            return grader.grade_problem(self.problem, self.populate, sql, self.seeds)
        return grader.grade(self.pid, sql, self.seeds)

    def _leaks(self, hint: str) -> bool:
        if self.populate is not None:
            return leaks_answer_for(self.problem, self.populate, hint, self.seeds)
        return leaks_answer(self.pid, hint, self.seeds)

    def submit(self, student_sql: str) -> Turn:
        gr = self._grade(student_sql)
        if gr.correct:
            turn = Turn(student_sql, True, None, None, "", False)
            self.turns.append(turn)
            return turn

        self.level = min(self.level + 1, MAX_LEVEL)
        hint = generate_hint(self.problem, gr, self.level, student_sql, self.model)

        leaked = False
        if self.guard_leaks and self._leaks(hint):
            leaked = True
            # safety fallback: drop to a non-leaking templated hint one level lower
            hint = _offline_hint(model_context(self.problem), gr, max(1, self.level - 1))

        turn = Turn(student_sql, False, self.level, hint, gr.diff_text, leaked)
        self.turns.append(turn)
        return turn

    # --- Phase-6-facing metrics (objective, no rubric) ---
    @property
    def solved(self) -> bool:
        return bool(self.turns) and self.turns[-1].correct

    @property
    def n_turns(self) -> int:
        return len(self.turns)

    @property
    def max_level(self) -> int:
        return max((t.level or 0) for t in self.turns) if self.turns else 0

    @property
    def leak_count(self) -> int:
        return sum(1 for t in self.turns if t.leaked)
