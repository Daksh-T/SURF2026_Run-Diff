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

Hint laddering (redesign 2026-06-28 — error-class-adaptive ordering):
  The ladder is built from four PRIMITIVES, and which primitive sits at L1/L2/L3 depends on
  the error-class FAMILY the deterministic grader already detects (grader.classify_family):

    primitive   what it is                                              source
    ----------  -----------------------------------------------------  ------------------
    diff        the rendered deterministic result-set difference        grader (no model)
    socratic    ONE question that makes the student locate the error    model
    conceptual  one-sentence nudge naming the KIND of mistake, no SQL   model
    directive   names the clause/op AND the fix, in prose, no SQL       model
    db_error    the database error message (error family L1)            grader (no model)

    family       L1          L2          L3
    -----------  ----------  ----------  ----------
    membership   diff        socratic    conceptual
    ordering     diff*       socratic    conceptual    (* rendered as "wrong order")
    structure    socratic    conceptual  directive     (the locked default)
    error        db_error    conceptual  directive

  Membership/ordering lead with the concrete diff then DE-escalate concreteness while
  escalating fix-guidance (intentional non-monotonicity). Structure never shows the raw diff
  (locked decision) and ends on the directive. `default` routes to structure.

One model call per turn (deterministic rungs make none). The harness is model-pluggable via
populator/model.py; an offline templated mode lets the whole loop run and be tested without
any LLM. The retired L3 query-skeleton primitive is GONE: its job is now done by `diff` (for
membership) or `directive` (for structure).
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
# the per-family ordering policy (redesign §3). Each family keeps three rungs;
# only the content/order changes. `default` -> structure.
# --------------------------------------------------------------------------- #
DIFF, SOCRATIC, CONCEPTUAL, DIRECTIVE, DB_ERROR = (
    "diff", "socratic", "conceptual", "directive", "db_error")
MODEL_PRIMITIVES = (SOCRATIC, CONCEPTUAL, DIRECTIVE)   # the rungs that call the LLM

_FAMILY_RUNGS = {
    "membership": [DIFF, SOCRATIC, CONCEPTUAL],
    "ordering":   [DIFF, SOCRATIC, CONCEPTUAL],
    "structure":  [SOCRATIC, CONCEPTUAL, DIRECTIVE],
    "error":      [DB_ERROR, CONCEPTUAL, DIRECTIVE],
}


def family_for(gr: grader.GradeResult) -> str:
    """The error-class family of a wrong grade (drives the ladder). Falls back to structure
    (the locked default) when no diff is available."""
    return gr.family or "structure"


def primitive_at(gr: grader.GradeResult, level: int) -> str:
    """Which of the four primitives sits at `level` (1..MAX_LEVEL) for this grade's family."""
    rungs = _FAMILY_RUNGS.get(family_for(gr), _FAMILY_RUNGS["structure"])
    return rungs[max(1, min(level, MAX_LEVEL)) - 1]


# --------------------------------------------------------------------------- #
# prompt construction for the single constrained hint call (model primitives only)
# --------------------------------------------------------------------------- #
_PRIMITIVE_RULES = {
    SOCRATIC:
        "Give a SOCRATIC hint: ask ONE pointed question that leads the student to LOCATE the "
        "error themselves (e.g. 'which of your rows shouldn't be there, and what do they have "
        "in common?'). Ask a question only — do NOT state the fix, do NOT name a SQL clause or "
        "keyword, and do NOT write any SQL.",
    CONCEPTUAL:
        "Give a CONCEPTUAL hint: one sentence naming the KIND of mistake (e.g. a filtering "
        "boundary, a grouping, an ordering). Do NOT name a specific SQL clause or keyword, and "
        "do NOT write any SQL.",
    DIRECTIVE:
        "Give a DIRECTIVE hint: in prose, name the specific SQL clause or operation that is "
        "wrong AND the nature of the fix (e.g. 'your JOIN is dropping unmatched rows — you want "
        "a LEFT JOIN'). Be concrete about WHAT to change and WHY. Do NOT write any runnable SQL "
        "and do NOT hand over a complete or near-complete query.",
}

_SYSTEM = (
    "You are a Socratic SQL tutor. You help a student fix THEIR query without ever giving "
    "them the answer. You do not have the correct query and must not invent and reveal one. "
    "You are given: the problem, the schema, the SQL features it targets, the student's "
    "query, their result, and a precise diff describing how their result is wrong. "
    "Respond with ONLY the hint text — no preamble, no full solution."
)


def build_hint_prompt(ctx: dict, student_sql: str, diff_text: str, primitive: str) -> str:
    parts = [
        _SYSTEM,
        "\n## Problem\n" + ctx["prompt"].strip(),
        "\n## Schema\n" + ctx["schema"].strip(),
        "\n## SQL features this problem is meant to exercise\n- "
        + "\n- ".join(ctx["target_clauses"]),
        "\n## The student's query\n" + student_sql.strip(),
        "\n## How the student's result is wrong (deterministic diff)\n" + diff_text.strip(),
        "\n## Your task\n" + _PRIMITIVE_RULES[primitive],
    ]
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# hint generation — model-backed or offline templated, dispatched by primitive
# --------------------------------------------------------------------------- #
def _offline_primitive(primitive: str, ctx: dict, gr: grader.GradeResult) -> str:
    """Deterministic, LLM-free text for a model primitive (socratic/conceptual/directive).
    Used both as the offline mode and as the leak-guard fallback; never emits the answer."""
    diff = gr.first_fail.diff if gr.first_fail else None
    clauses = ctx["target_clauses"]
    fam = family_for(gr)
    if primitive == SOCRATIC:
        if fam == "error":
            return "Your query didn't run — what do the table and column names in it refer to?"
        if fam == "ordering":
            return "Your rows look right — so what's different about the ORDER they come out in?"
        if fam in ("membership",):
            return ("Compare your rows to what the question asks for: which rows shouldn't be "
                    "there, or are missing — and what do those rows have in common?")
        return ("What is the question asking for that your query isn't producing? Look at one "
                "row that's wrong and ask what your query did to it.")
    if primitive == CONCEPTUAL:
        if fam == "error":
            return ("Your query has a syntax or naming problem — it can't run yet, so fix that "
                    "before worrying about which rows it returns.")
        if fam == "ordering":
            return "You're selecting the right rows, but think about the order they come out in."
        if fam == "membership":
            return ("Your result includes or excludes the wrong rows — the issue is a boundary "
                    "or a filter, not a calculation.")
        return ("The shape of your result is off — think about how you're grouping or computing "
                "values, not just which rows you keep.")
    # DIRECTIVE
    if fam == "error":
        return ("Read the database error and fix it first: check that every table and column name "
                "exists and is spelled the way the schema declares it.")
    if clauses:
        return ("Revisit your " + ", ".join(clauses[:3]) + " — one of these is the wrong "
                "operation for what the prompt asks. Change that operation (not just a value) "
                "so it does what the question describes.")
    return ("Name the clause that produces the wrong part of your result and change the "
            "operation it performs to match what the prompt asks — not just a literal value.")


def render_diff_rung(gr: grader.GradeResult) -> str:
    """The deterministic `diff`/`db_error` rung text (no model). For the error family this is
    the database error; otherwise the family-aware rendered result-set diff."""
    diff = gr.first_fail.diff if gr.first_fail else None
    if diff is None:
        return ""
    if diff.sql_error:
        return f"Your query did not run. The database reported:\n{diff.sql_error}"
    return diff.to_text(family=family_for(gr))


def _offline_hint(ctx: dict, gr: grader.GradeResult, level: int) -> str:
    """Deterministic, LLM-free hint at `level` for this grade's family. Lets the harness run &
    be tested with no model, and never emits the answer. Back-compat: still takes a level."""
    primitive = primitive_at(gr, level)
    if primitive in (DIFF, DB_ERROR):
        return render_diff_rung(gr)
    return _offline_primitive(primitive, ctx, gr)


def generate_hint(problem, gr: grader.GradeResult, level: int, student_sql: str,
                  model: str | None = None) -> str:
    """Produce a single hint at `level`, choosing the primitive from the grade's family.
    Deterministic rungs (diff / db_error) never call a model. For the model rungs: if `model`
    is None use the offline template; otherwise call the pluggable model (gold never included)
    and fall back to the offline template on empty/failed output."""
    ctx = model_context(problem)
    primitive = primitive_at(gr, level)
    if primitive in (DIFF, DB_ERROR):
        return render_diff_rung(gr)
    if model is None:
        return _offline_primitive(primitive, ctx, gr)
    import model as model_mod  # populator/model.py
    prompt = build_hint_prompt(ctx, student_sql, gr.diff_text, primitive)
    # hints are interactive: one retry, not the authoring loop's long exponential backoff —
    # if the model is unreachable we fall back to the offline templated hint within seconds
    out = model_mod.call(model, prompt, max_retries=1)
    return (out.get("text") or "").strip() or _offline_primitive(primitive, ctx, gr)


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
