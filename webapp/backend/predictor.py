"""Feature A — pre-publication difficulty prediction.

Before students see an authored problem, run the measured weak simulated student
(qwen2.5-coder:1.5b — the Phase-6 "committed" student's role, here used capable-style
since there's no committed error to seed from) against the shipped tutor path
(harness.TutorSession via qwen2.5-coder:7b). Instructors compare the predicted
solve_rate / avg_turns / avg_max_hint_level against real class performance later
(see app.py analytics endpoints).

Same gold-free spine as the rest of the app: TutorSession.submit() grades via the
deterministic grader and only ever shows the 1.5b student the redacted problem view
(harness.model_context) — never gold_sql.

State-mode (CREATE/INSERT/UPDATE/DELETE/DROP, graded by final DB state) problems are
predicted by `predict_state`, which mirrors this flow on the `state_core` primitives:
bake_gold_state / grade_baked_state / generate_hint_state / hint_leaks_state, driven by
a small `_StateSession` that copies TutorSession's level-escalation and leak-guard
policy exactly (state problems have no TutorSession — it is SELECT-bound).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "tutor", ROOT / "populator", ROOT / "eval" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import harness                              # noqa: E402  tutor/
import model as model_mod                   # noqa: E402  populator/model.py
from populate import load_populate          # noqa: E402  populator/
from sim_student_eval import student_revise  # noqa: E402  tutor/ — reuse the revise prompt

import state_core as sc                     # noqa: E402  webapp/backend/ — state-mode primitives
import tutor_core as tc                     # noqa: E402  webapp/backend/ — RedactedProblem

STUDENT_MODEL = "qwen1.5b"   # qwen2.5-coder:1.5b — the weak simulated student
TUTOR_MODEL = "qwen7b"       # qwen2.5-coder:7b — the shipped hint model
MAX_TURNS = 4                # revisions after the first (wrong) attempt


@dataclass
class _PredictProblem:
    """Minimal Problem-like object for TutorSession(problem=..., populate=...). No `check` is
    needed at predict-time (the data was already validated at authoring time)."""
    id: str
    title: str
    difficulty: str
    prompt: str
    schema: str
    gold_sql: str
    target_clauses: list[str]
    check: callable = field(default=lambda conn, rows: None)
    tables: list[str] = field(default_factory=list)


_FIRST_ATTEMPT_SYS = (
    "You are a beginning SQL student working on a practice problem. Write a SQLite query "
    "that answers the question below. This is attempt #{trial} — if you've tried this "
    "problem before, try a different approach this time. "
    "Reply with ONLY the SQL — no explanation, no code fences."
)


def _first_attempt(problem: _PredictProblem, trial: int) -> str:
    """A from-scratch attempt by the weak student, before any tutor interaction."""
    prompt = (
        f"{_FIRST_ATTEMPT_SYS.format(trial=trial)}\n\n## Problem\n{problem.prompt.strip()}"
        f"\n\n## Schema\n{problem.schema.strip()}\n\n## Task\nWrite the SQLite query."
    )
    out = model_mod.call(STUDENT_MODEL, prompt, max_retries=1)
    text = (out.get("text") or "").strip()
    sqls = harness.extract_sql(text)
    return sqls[0] if sqls else text


def _run_trial(problem: _PredictProblem, populate, trial: int) -> dict:
    session = harness.TutorSession(problem=problem, populate=populate, model=TUTOR_MODEL)
    sql = _first_attempt(problem, trial)
    turn = session.submit(sql)
    unaided = turn.correct  # did the from-scratch attempt (before any hint) already pass?
    attempts, hints = [sql], []
    for t in range(2, MAX_TURNS + 2):  # up to MAX_TURNS revisions after the first attempt
        if turn.correct:
            break
        if turn.hint is not None:
            hints.append((turn.level, turn.hint))
        if t > MAX_TURNS + 1:
            break
        revised = student_revise(STUDENT_MODEL, problem, attempts, hints, t, persona="capable")
        attempts.append(revised)
        turn = session.submit(revised)
    return {"unaided": unaided, "solved": session.solved,
            "n_turns": session.n_turns, "max_level": session.max_level}


def predict(problem_dict: dict, trials: int = 3) -> dict:
    """Run `trials` independent sessions of the weak student against the tutor path for an
    authored (not-yet-published) problem, and summarize how much help it needed."""
    problem = _PredictProblem(
        id=problem_dict["id"], title=problem_dict.get("title", problem_dict["id"]),
        difficulty=problem_dict.get("difficulty", "medium"),
        prompt=problem_dict["prompt"], schema=problem_dict["schema"],
        gold_sql=problem_dict["gold_sql"],
        target_clauses=list(problem_dict.get("target_clauses", [])),
    )
    populate = load_populate(problem_dict["generator_src"])

    results = [_run_trial(problem, populate, trial) for trial in range(1, trials + 1)]

    solved_unaided = sum(1 for r in results if r["unaided"])
    n_solved = sum(1 for r in results if r["solved"])
    return {
        "trials": trials,
        "solved_unaided": solved_unaided,
        "solve_rate": round(n_solved / trials, 3) if trials else None,
        "avg_turns": round(sum(r["n_turns"] for r in results) / trials, 2) if trials else None,
        "avg_max_hint_level": round(sum(r["max_level"] for r in results) / trials, 2) if trials else None,
        "model_student": "qwen2.5-coder:1.5b",
        "model_tutor": "qwen2.5-coder:7b",
    }


# --------------------------------------------------------------------------- #
# state-mode prediction (CREATE/INSERT/UPDATE/DELETE/DROP — graded by DB state)
# --------------------------------------------------------------------------- #
_STATE_FIRST_ATTEMPT_SYS = (
    "You are a beginning SQL student working on a practice problem. Write the SQLite "
    "statement(s) that do what the question below asks (CREATE/INSERT/UPDATE/DELETE/DROP "
    "as appropriate). This is attempt #{trial} — if you've tried this problem before, try "
    "a different approach this time. "
    "Reply with ONLY the SQL — no explanation, no code fences."
)

# state-flavored sibling of sim_student_eval._STUDENT_SYS — "query" -> "statement(s)"
_STATE_STUDENT_SYS = (
    "You are a beginning SQL student working on a practice problem. Your previous attempt "
    "was graded INCORRECT. Revise YOUR OWN SQL statement(s) using only the problem "
    "statement, the schema, and the tutor's hint (if any). Do not start from scratch "
    "unless you must. Reply with ONLY the revised SQLite statement(s) — no explanation, "
    "no code fences."
)


def _first_attempt_state(problem: tc.RedactedProblem, trial: int) -> str:
    """A from-scratch attempt by the weak student, before any tutor interaction."""
    prompt = (
        f"{_STATE_FIRST_ATTEMPT_SYS.format(trial=trial)}\n\n## Problem\n{problem.prompt.strip()}"
        f"\n\n## Schema\n{(problem.schema.strip() or '(no pre-existing tables)')}"
        f"\n\n## Task\nWrite the SQLite statement(s)."
    )
    out = model_mod.call(STUDENT_MODEL, prompt, max_retries=1)
    text = (out.get("text") or "").strip()
    sqls = harness.extract_sql(text)
    stmts = sc._extract_state_sql(text)
    candidate = sqls[0] if sqls else (stmts[0] if stmts else text)
    return candidate


def student_revise_state(student_model: str, problem: tc.RedactedProblem, attempts: list[str],
                         hints: list[tuple[int, str]], turn: int,
                         l3_evidence: str | None = None) -> str:
    """State-adapted sibling of `sim_student_eval.student_revise`: same persona/structure,
    "query" language swapped for "statement(s)", and (at L3) the deterministic state-diff
    evidence appended so the student can see it — gold-free, it's the same text the L3
    hint level would otherwise summarize via a model call."""
    history = "\n".join(f"Attempt {i+1} (graded INCORRECT):\n{a}" for i, a in enumerate(attempts))
    hint_txt = "\n".join(f"- (hint level {lv}) {h}" for lv, h in hints) or "(no hints given)"
    extra = f"\n\n## Evidence of how the database state is wrong\n{l3_evidence.strip()}" if l3_evidence else ""
    prompt = (
        f"{_STATE_STUDENT_SYS}\n\n## Problem\n{problem.prompt.strip()}\n\n## Schema\n"
        f"{(problem.schema.strip() or '(no pre-existing tables)')}\n\n## Your attempts so far\n"
        f"{history}\n\n## Tutor hints so far\n{hint_txt}{extra}\n\n"
        f"## Task\nWrite revision #{turn}. Output ONLY the SQL."
    )
    out = model_mod.call(student_model, prompt, max_retries=1)
    text = (out.get("text") or "").strip()
    sqls = harness.extract_sql(text)
    if sqls:
        return sqls[0]
    stmts = sc._extract_state_sql(text)
    return stmts[0] if stmts else text


class _StateSession:
    """State-mode sibling of `harness.TutorSession`. There is no TutorSession for state
    problems (it is SELECT-bound — it calls `grader.grade`/`grader.grade_problem`), so this
    mirrors its escalation policy and leak guard exactly, on the `state_core` primitives:

      * `submit(sql)` grades via `grade_baked_state`; on a wrong submission, escalates
        `self.level` by 1 (capped at `harness.MAX_LEVEL` == 3), generates a hint at that
        level, and leak-guards it via `hint_leaks_state` — on a flag, drops to the offline
        templated hint one level lower, exactly like `TutorSession.submit`.
      * L1/L2 hints are model-drawn via `generate_hint_state`. L3 has no model-drawn hint in
        state_core (only L1/L2 rules exist there) — per the task spec, "L3 = StateDiff.to_text
        evidence appended without a model call". So at L3 this session returns the
        deterministic diff text itself as the "hint" (gold-free, already-redacted, and by
        construction not leak-guardable-as-a-statement, so `leaked` is always False for L3).
      * `.solved` / `.n_turns` / `.max_level` mirror TutorSession's same-named properties.
    """

    def __init__(self, problem_id: str, schema: str, generator_src: str, baked: dict,
                redacted: tc.RedactedProblem, model: str | None, guard_leaks: bool = True):
        self.problem_id = problem_id
        self.schema = schema
        self.generator_src = generator_src
        self.baked = baked
        self.redacted = redacted
        self.model = model
        self.guard_leaks = guard_leaks
        self.level = 0
        self.turns: list[dict] = []

    def _grade(self, sql: str) -> sc.StateGradeResult:
        return sc.grade_baked_state(self.problem_id, self.schema, self.generator_src,
                                     self.baked, sql)

    def _leaks(self, hint: str) -> bool:
        return sc.hint_leaks_state(self.problem_id, self.schema, self.generator_src,
                                    self.baked, hint)

    def submit(self, student_sql: str) -> dict:
        gr = self._grade(student_sql)
        if gr.correct:
            turn = {"correct": True, "level": None, "hint": None}
            self.turns.append(turn)
            return turn

        self.level = min(self.level + 1, harness.MAX_LEVEL)

        if self.level >= 3:
            # L3: deterministic diff evidence, no model call (per spec) — gold-free and not a
            # runnable statement, so it cannot leak the gold by construction.
            diff_text = gr.first_fail.diff.to_text() if (gr.first_fail and gr.first_fail.diff) else ""
            turn = {"correct": False, "level": self.level, "hint": diff_text, "leaked": False}
            self.turns.append(turn)
            return turn

        hint = sc.generate_hint_state(self.redacted, gr, self.level, student_sql, self.model)

        leaked = False
        if self.guard_leaks and self._leaks(hint):
            leaked = True
            # safety fallback: regenerate offline at one level lower, same as TutorSession
            hint = sc._offline_hint_state(sc.error_category_state(gr), max(1, self.level - 1))

        turn = {"correct": False, "level": self.level, "hint": hint, "leaked": leaked}
        self.turns.append(turn)
        return turn

    @property
    def solved(self) -> bool:
        return bool(self.turns) and self.turns[-1]["correct"]

    @property
    def n_turns(self) -> int:
        return len(self.turns)

    @property
    def max_level(self) -> int:
        return max((t["level"] or 0) for t in self.turns) if self.turns else 0


def _run_trial_state(redacted: tc.RedactedProblem, problem_id: str, schema: str,
                     generator_src: str, baked: dict, trial: int) -> dict:
    session = _StateSession(problem_id, schema, generator_src, baked, redacted, model=TUTOR_MODEL)
    sql = _first_attempt_state(redacted, trial)
    turn = session.submit(sql)
    unaided = turn["correct"]
    attempts, hints = [sql], []
    for t in range(2, MAX_TURNS + 2):  # up to MAX_TURNS revisions after the first attempt
        if turn["correct"]:
            break
        if turn["hint"] is not None:
            hints.append((turn["level"], turn["hint"]))
        if t > MAX_TURNS + 1:
            break
        l3_evidence = turn["hint"] if turn["level"] and turn["level"] >= 3 else None
        revised = student_revise_state(STUDENT_MODEL, redacted, attempts, hints, t,
                                        l3_evidence=l3_evidence)
        attempts.append(revised)
        turn = session.submit(revised)
    return {"unaided": unaided, "solved": session.solved,
            "n_turns": session.n_turns, "max_level": session.max_level}


def predict_state(problem_dict: dict, trials: int = 3) -> dict:
    """State-mode sibling of `predict`: bakes the gold STATE once (instructor-side, gold-free
    downstream of that point — see `state_core.bake_gold_state`), then runs `trials`
    independent `_StateSession`s of the weak student against it. Same summary shape as
    `predict` so analytics/Insights "predicted vs actual" works unchanged for state problems."""
    redacted = tc.RedactedProblem(
        id=problem_dict["id"], title=problem_dict.get("title", problem_dict["id"]),
        difficulty=problem_dict.get("difficulty", "medium"),
        prompt=problem_dict["prompt"], schema=problem_dict["schema"],
        target_clauses=list(problem_dict.get("target_clauses", [])),
    )
    schema = problem_dict["schema"]
    generator_src = problem_dict["generator_src"]
    baked = sc.bake_gold_state(schema, problem_dict["gold_sql"], generator_src)

    results = [_run_trial_state(redacted, problem_dict["id"], schema, generator_src, baked, trial)
               for trial in range(1, trials + 1)]

    solved_unaided = sum(1 for r in results if r["unaided"])
    n_solved = sum(1 for r in results if r["solved"])
    return {
        "trials": trials,
        "solved_unaided": solved_unaided,
        "solve_rate": round(n_solved / trials, 3) if trials else None,
        "avg_turns": round(sum(r["n_turns"] for r in results) / trials, 2) if trials else None,
        "avg_max_hint_level": round(sum(r["max_level"] for r in results) / trials, 2) if trials else None,
        "model_student": "qwen2.5-coder:1.5b",
        "model_tutor": "qwen2.5-coder:7b",
    }
