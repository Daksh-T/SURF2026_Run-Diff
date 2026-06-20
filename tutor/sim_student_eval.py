"""Phase 6 — simulated-student evaluation of the tutor, on REAL local models.

A simulated student (a role-prompted local model, denied the gold query — it receives only
what a real student sees: problem, schema, its own attempt history, correct/incorrect, and
the tutor's hint) starts from a genuinely-wrong query (a committed error category, the same
mutation battery as leakage_eval) and revises until correct or out of turns.

The tutor side is the SHIPPED hint path (harness.generate_hint + execution leak guard).

Objective outcome metrics per (problem, condition):
    reached_correct   did it ever grade correct (execution, binary)
    n_turns           submissions used (first wrong one included)
    max_hint_level    highest hint rung needed
    leaks             guard-caught hint leaks during the session (target 0 surfaced)

Hint-level calibration (the README's marginal-lift design): run the same student under
caps — no hints at all (cap 0), L1 only (cap 1), L1+L2 (cap 2), L1+L2+L3 (cap 3). Good
laddering = solve rate rises with the cap, gradually; L1 alone should NOT one-shot every
problem (that would mean L1 is too strong/leaky).

Run:  uv run python tutor/sim_student_eval.py [--tutor-model qwen7b] [--student-model qwen7b]
      [--max-turns 5] [--caps 0,1,2,3]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for p in (HERE, ROOT / "populator", ROOT / "eval" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import grader            # noqa: E402
import harness           # noqa: E402
import model as model_mod  # noqa: E402  populator/model.py
from problems import bank  # noqa: E402
from leakage_eval import wrong_query  # noqa: E402  same committed-error battery


# --------------------------------------------------------------------------- #
# the simulated student — sees exactly what a real student sees, never the gold
# --------------------------------------------------------------------------- #
# Two personas. "capable" just revises — but a competent code model then re-derives the
# answer with no hints at all (measured: 9/10 solved at cap 0), a ceiling that can't show
# hint-level lift. "committed" is the README's design — the student COMMITS to the sampled
# error category (holds the misconception that produced the wrong query) and only abandons
# it when the tutor's feedback specifically dislodges it. That models the student the tutor
# exists for.
_STUDENT_SYS = (
    "You are a beginning SQL student working on a practice problem. Your previous attempt "
    "was graded INCORRECT. Revise YOUR OWN query using only the problem statement, the "
    "schema, and the tutor's hint (if any). Do not start from scratch unless you must. "
    "Reply with ONLY the revised SQLite query — no explanation, no code fences."
)

# the misconception behind each committed error category (labels from leakage_eval._mutations)
_BELIEF = {
    "sort direction flipped": "the sort direction you used is the one the problem wants",
    "tie-break dropped": "no secondary sort key is needed — one ORDER BY column is enough",
    "WHERE dropped": "no row filtering is needed for this problem",
    "off-by-one LIMIT": "the row limit you used is the right number",
    "HAVING weakened": "your group-filter threshold/comparison is correct as written",
    "aggregate swapped": "the aggregate function you used is the right one for this problem",
}

_COMMITTED_SYS = (
    "You are a struggling SQL student. You wrote your query deliberately, because you "
    "believe: {belief}. You are CONFIDENT in that belief — it feels obviously right to you. "
    "An 'INCORRECT' verdict alone does not tell you what is wrong, and you must NOT change "
    "the part of the query tied to your belief unless a tutor hint specifically gives you a "
    "reason to question it. With no hint (or an unrelated hint), keep your approach and make "
    "at most one small change to something else you are less sure about. When a hint does "
    "point at something concrete, apply the smallest fix consistent with it. "
    "Reply with ONLY the revised SQLite query — no explanation, no code fences."
)


def student_revise(student_model: str, problem, attempts: list[str],
                   hints: list[tuple[int, str]], turn: int,
                   persona: str = "capable", error_label: str = "") -> str:
    history = "\n".join(f"Attempt {i+1} (graded INCORRECT):\n{a}" for i, a in enumerate(attempts))
    hint_txt = "\n".join(f"- (hint level {lv}) {h}" for lv, h in hints) or "(no hints given)"
    sys_txt = (_COMMITTED_SYS.format(belief=_BELIEF.get(error_label, "your approach is right"))
               if persona == "committed" else _STUDENT_SYS)
    prompt = (
        f"{sys_txt}\n\n## Problem\n{problem.prompt.strip()}\n\n## Schema\n"
        f"{problem.schema.strip()}\n\n## Your attempts so far\n{history}\n\n"
        f"## Tutor hints so far\n{hint_txt}\n\n"
        f"## Task\nWrite revision #{turn}. Output ONLY the SQL."
    )
    out = model_mod.call(student_model, prompt, max_retries=1)
    text = (out.get("text") or "").strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    if m:
        text = m.group(1).strip()
    return text


# --------------------------------------------------------------------------- #
# one session: student starts wrong, tutor hints up to a level cap
# --------------------------------------------------------------------------- #
def run_session(pid: str, start_label: str, start_sql: str, cap: int,
                tutor_model: str, student_model: str, max_turns: int,
                persona: str = "capable") -> dict:
    p = bank.get(pid)
    attempts, hints = [start_sql], []
    level = 0
    leaks = 0
    solved = False
    for turn in range(1, max_turns + 1):
        gr = grader.grade(pid, attempts[-1])
        if gr.correct:
            solved = True
            break
        if cap > 0:
            level = min(level + 1, cap)
            hint = harness.generate_hint(p, gr, level, attempts[-1], model=tutor_model)
            if harness.leaks_answer(pid, hint):
                leaks += 1
                hint = harness._offline_hint(harness.model_context(p), gr, max(1, level - 1))
            hints.append((level, hint))
        if turn == max_turns:
            break
        attempts.append(student_revise(student_model, p, attempts, hints, turn + 1,
                                       persona=persona, error_label=start_label))
    return {"pid": pid, "error": start_label, "cap": cap, "persona": persona,
            "reached_correct": solved, "n_turns": len(attempts),
            "max_hint_level": max((lv for lv, _ in hints), default=0),
            "leaks": leaks, "attempts": attempts,
            "hints": [{"level": lv, "text": h} for lv, h in hints]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tutor-model", default="qwen7b")
    ap.add_argument("--student-model", default="qwen7b")
    ap.add_argument("--max-turns", type=int, default=5)
    ap.add_argument("--caps", default="0,1,2,3")
    ap.add_argument("--persona", default="committed", choices=["committed", "capable"])
    ap.add_argument("--problems", default="all")
    ap.add_argument("--out", default=str(HERE / "experiments" / "runs"))
    args = ap.parse_args()

    if args.tutor_model.lower() in ("offline", "none"):   # deterministic templated hints
        args.tutor_model = None
    caps = [int(c) for c in args.caps.split(",")]
    pids = ([p.id for p in bank.PROBLEMS] if args.problems == "all"
            else args.problems.split(","))
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    jl = (outdir / f"sim_student_{args.persona}_{stamp}.jsonl").open("w")

    rows = []
    for pid in pids:
        wq = wrong_query(pid)
        if wq is None:
            print(f"skip {pid}: no wrong mutation", flush=True)
            continue
        label, sql = wq
        for cap in caps:
            t0 = time.time()
            r = run_session(pid, label, sql, cap, args.tutor_model,
                            args.student_model, args.max_turns, persona=args.persona)
            r["elapsed_s"] = round(time.time() - t0, 1)
            rows.append(r)
            jl.write(json.dumps(r) + "\n")
            jl.flush()
            print(f"{pid:20s} cap={cap}  solved={str(r['reached_correct']):5s} "
                  f"turns={r['n_turns']} maxL={r['max_hint_level']} leaks={r['leaks']} "
                  f"({r['elapsed_s']}s)", flush=True)
    jl.close()

    # summary per cap
    summary = {"tutor_model": args.tutor_model, "student_model": args.student_model,
               "persona": args.persona,
               "max_turns": args.max_turns, "stamp": stamp, "per_cap": {}}
    for cap in caps:
        rs = [r for r in rows if r["cap"] == cap]
        if not rs:
            continue
        solved = [r for r in rs if r["reached_correct"]]
        summary["per_cap"][cap] = {
            "n": len(rs), "solve_rate": round(len(solved) / len(rs), 3),
            "avg_turns_when_solved": round(sum(r["n_turns"] for r in solved) / len(solved), 2)
                                     if solved else None,
            "avg_max_hint_level": round(sum(r["max_hint_level"] for r in rs) / len(rs), 2),
            "total_leaks_caught": sum(r["leaks"] for r in rs),
        }
    spath = outdir / f"sim_student_{args.persona}_{stamp}.summary.json"
    spath.write_text(json.dumps(summary, indent=2))
    print("\n== SUMMARY ==")
    print(json.dumps(summary, indent=2))
    print(f"\nwrote {spath}")


if __name__ == "__main__":
    main()
