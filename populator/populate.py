"""The synthetic-data populator (SURF Phase 2).

Per problem, an LLM authors a *seeded Python data generator* — not raw rows.  The
harness instantiates K in-memory SQLite databases (one per seed), runs the gold query
on each, and asserts the result is non-trivial and exercises the problem's target
clauses.  On any failure the error is fed back to the model (repair loop); on K-for-K
success the generator is frozen to `generators/<id>.py`.

Design notes tied to the project plan:
  * SQLite, in-memory, everywhere. K seeds == K datasets, built at runtime, never persisted.
  * The generator is deterministic given its seed (we verify this).
  * Edge cases (NULLs, ties, duplicates, randomized insert order) are injected *by
    construction* in the generator, and the per-problem `check` confirms they landed.
  * The populator model is allowed to see the gold query: it is NOT the tutor.  The
    security spine (model never sees gold) applies to Phase 4, not here.

Usage:
  uv run python populate.py --model groq --problems all --k 8 --attempts 5
  uv run python populate.py --model qwen7b --problems p02_orderby_ties --k 8
  uv run python populate.py --selftest            # validate the bank with reference generators
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from problems import bank

HERE = Path(__file__).resolve().parent
GEN_DIR = HERE / "generators"
RUN_DIR = HERE / "runs"
GEN_DIR.mkdir(exist_ok=True)
RUN_DIR.mkdir(exist_ok=True)

DEFAULT_SEEDS = list(range(1, 9))   # K=8 during authoring

# --------------------------------------------------------------------------- #
# prompt construction
# --------------------------------------------------------------------------- #
SYSTEM = """\
You write Python data generators for SQL teaching problems.

Your job: write ONE function

    def populate(conn, seed):
        ...

that inserts synthetic rows into the given sqlite3 connection `conn`, so that running
the GOLD QUERY below over that data produces a meaningful, non-trivial result which
exercises every target SQL clause.

Hard rules:
- Use only the Python standard library: `import random`, and `import sqlite3` is already
  available (do NOT reconnect — use the `conn` you are given). `from datetime import ...`
  is fine. Do not use Faker or any third-party package.
- Be DETERMINISTIC in `seed`: create `rng = random.Random(seed)` and draw ALL randomness
  from `rng`. The same seed must always produce the same rows.
- The TABLES ALREADY EXIST (schema below). Do not CREATE or DROP tables. Just INSERT.
- Respect primary keys / uniqueness: never insert a duplicate primary key.
- Insert rows in a randomized order (shuffle before inserting) so ordering is never
  accidentally pre-sorted.
- Satisfy ALL the DATA REQUIREMENTS listed — they guarantee the gold query is non-trivial.
- GUARANTEE each required edge case BY CONSTRUCTION, for EVERY seed. Do not rely on chance
  to satisfy a requirement: if a row "exactly on the boundary" or "a professor teaching two
  classes" or "a player with no matches" is required, explicitly hard-code at least one such
  row unconditionally, THEN add randomized rows around it. A requirement that only sometimes
  holds (depending on the seed) is a bug.
- Output ONLY one ```python code block containing the `populate` function (plus any
  imports/helpers it needs). No prose, no explanation, no example calls.

WORKED EXAMPLE (a DIFFERENT problem — shows the required STYLE; do not copy its tables):
Gold query: SELECT name FROM products WHERE price BETWEEN 10 AND 20 ORDER BY price DESC, name ASC
```python
import random
def populate(conn, seed):
    rng = random.Random(seed)
    rows = []
    # 1) GUARANTEE every edge case BY CONSTRUCTION, unconditionally (never rely on chance):
    rows.append(("OnLowBoundary", 10))    # exactly on the lower bound -> included
    rows.append(("OnHighBoundary", 20))   # exactly on the upper bound -> included
    rows.append(("JustBelow", 9))         # OUTSIDE the range -> proves the filter excludes
    rows.append(("JustAbove", 21))        # OUTSIDE the range -> proves the filter excludes
    rows.append(("TiePriceA", 15))        # two rows share a price -> exercises the ASC tie-break
    rows.append(("TiePriceB", 15))
    # 2) add randomized rows that VARY with the seed (so each seed is a different dataset):
    for i in range(rng.randint(6, 12)):
        rows.append((f"P{i}_{seed}", rng.randint(1, 40)))
    # 3) insert in randomized order so nothing is accidentally pre-sorted:
    rng.shuffle(rows)
    conn.executemany("INSERT INTO products(name, price) VALUES (?, ?)", rows)
```
Notice: rows OUTSIDE a filter range are added on purpose; ties for a secondary sort key are
hard-coded; a NULL (for a NULL-skipping aggregate) or an unmatched row (for a JOIN) would be
guaranteed the same way; and every dataset differs by seed.

WORKED EXAMPLE 2 (a DIFFERENT problem — a SELF-REFERENTIAL "more than the AVERAGE" threshold):
When the gold compares a value to an AVERAGE computed over the SAME data (e.g.
`LENGTH(content) > (SELECT AVG(LENGTH(content)) FROM posts)`, or `amount > (SELECT AVG(amount)
...)`), do NOT scatter random values and hope enough land above the mean. You CONTROL where the
average falls by making the data clearly BIMODAL: many SMALL rows and a distinct group of LARGE
rows. The mean then sits between the two groups, so every LARGE row is above it BY CONSTRUCTION.
If a COUNT/HAVING also applies, give the SAME key (author/customer) enough large rows to clear it,
and include one key that falls JUST SHORT so the HAVING is shown to exclude.
Gold query: SELECT author_id FROM posts
            WHERE LENGTH(content) > (SELECT AVG(LENGTH(content)) FROM posts)
            GROUP BY author_id HAVING COUNT(*) >= 3
```python
import random
def populate(conn, seed):
    rng = random.Random(seed)
    rows = []                                  # (author_id, content)
    SHORT, LONG = "x" * 20, "y" * 400          # two clearly separated lengths
    # many SHORT posts pull the average DOWN, so every LONG post is above it by construction:
    for _ in range(rng.randint(15, 25)):
        rows.append((rng.randint(50, 70), SHORT))
    # >=2 authors each get >=3 LONG posts -> they clear HAVING COUNT(*) >= 3 (non-trivial result):
    for author in (1, 2):
        for _ in range(3 + rng.randint(0, 2)):
            rows.append((author, LONG + "z" * rng.randint(0, 40)))   # vary length per seed
    # an author with only ONE long post -> EXCLUDED by the HAVING (proves the threshold bites):
    rows.append((3, LONG))
    rng.shuffle(rows)
    conn.executemany("INSERT INTO posts(author_id, content) VALUES (?, ?)", rows)
```
Notice: the average is STEERED by the small/large split (never left to chance); the target keys
get ENOUGH large rows to clear the COUNT; and a just-short key proves the HAVING excludes. The
same idea covers a SECOND correlated predicate (e.g. title SHORTER than average): give the target
rows a short title and the filler rows a long title, so both averages split the same way.
(Diversity caveat: for THIS class of self-referential-threshold query, steering the average by a
fixed split is what makes authoring robust; randomising WHICH keys qualify per seed tends to break
the 200-seed robustness, so the shipped generator may be low-diversity — robust beats diverse.)
"""

USER_TMPL = """\
PROBLEM: {title}

SCHEMA (already created for you):
{schema}

GOLD QUERY (will be run against your data; make its result non-trivial):
{gold}

TARGET CLAUSES the data must exercise: {clauses}

DATA REQUIREMENTS (all mandatory):
{requirements}

Write `def populate(conn, seed):` now.
"""

REPAIR_TMPL = """\
Your previous `populate` function FAILED validation.

YOUR PREVIOUS CODE:
```python
{code}
```

FAILURE (on seed {seed}):
{error}

Fix the function so it passes. Keep the same signature `def populate(conn, seed):`.
Output ONLY the corrected ```python code block.
"""


def build_initial(problem) -> str:
    reqs = "\n".join(f"- {r}" for r in problem.requirements)
    return SYSTEM + "\n\n" + USER_TMPL.format(
        title=problem.title, schema=problem.schema.strip(),
        gold=problem.gold_sql.strip(), clauses=", ".join(problem.target_clauses),
        requirements=reqs)


def build_repair(problem, code, seed, error) -> str:
    return SYSTEM + "\n\n" + USER_TMPL.format(
        title=problem.title, schema=problem.schema.strip(),
        gold=problem.gold_sql.strip(), clauses=", ".join(problem.target_clauses),
        requirements="\n".join(f"- {r}" for r in problem.requirements),
    ) + "\n\n" + REPAIR_TMPL.format(code=code.strip(), seed=seed, error=error)


# --------------------------------------------------------------------------- #
# code extraction + execution
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_code(text: str) -> str:
    blocks = _FENCE.findall(text or "")
    if blocks:
        # prefer the block that defines populate
        for b in blocks:
            if "def populate" in b:
                return b.strip()
        return blocks[0].strip()
    # no fence: assume the whole text is code if it defines populate
    return (text or "").strip()


def load_populate(code: str):
    """Exec generator code in a fresh namespace and return its populate()."""
    ns: dict = {}
    exec(compile(code, "<generator>", "exec"), ns)
    if "populate" not in ns or not callable(ns["populate"]):
        raise ValueError("code does not define a callable `populate`")
    return ns["populate"]


def _instantiate(problem, populate, seed) -> tuple[sqlite3.Connection, list]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(problem.schema)
    populate(conn, seed)
    conn.commit()
    rows = conn.execute(problem.gold_sql).fetchall()
    return conn, rows


# --------------------------------------------------------------------------- #
# validation: one generator vs all seeds
# --------------------------------------------------------------------------- #
class GenFailure(Exception):
    def __init__(self, seed, stage, message):
        self.seed, self.stage, self.message = seed, stage, message
        super().__init__(f"[seed {seed}] {stage}: {message}")


def _annotate(tb: str, code: str) -> str:
    """Append the offending generator source lines to a traceback so the model can
    see exactly which line of ITS code raised."""
    lines = (code or "").splitlines()
    out = [tb]
    for m in re.finditer(r'File "<generator>", line (\d+)', tb):
        n = int(m.group(1))
        if 1 <= n <= len(lines):
            out.append(f"  >> your line {n}: {lines[n - 1].strip()}")
    return "\n".join(out)


def validate_generator(problem, code: str, seeds: list[int]) -> dict:
    """Run a generator (given as source code) across all seeds."""
    try:
        populate = load_populate(code)
    except Exception:
        return {"ok": False, "seed": None, "stage": "compile",
                "detail": _annotate(traceback.format_exc(limit=3), code)}
    return validate_callable(problem, populate, seeds, code=code)


def validate_callable(problem, populate, seeds: list[int], code: str | None = None) -> dict:
    """Run a populate(conn, seed) callable across all seeds. Returns dict(ok, detail, samples)."""
    samples = []
    for seed in seeds:
        # 1) instantiate + run gold
        try:
            conn, rows = _instantiate(problem, populate, seed)
        except Exception:
            return {"ok": False, "seed": seed, "stage": "populate/gold",
                    "detail": _annotate(traceback.format_exc(limit=4), code) if code
                              else traceback.format_exc(limit=4)}
        # 2) problem-specific non-triviality checks
        try:
            problem.check(conn, rows)
        except AssertionError as e:
            return {"ok": False, "seed": seed, "stage": "check", "detail": str(e)}
        except Exception:
            return {"ok": False, "seed": seed, "stage": "check-error",
                    "detail": traceback.format_exc(limit=4)}
        # 3) determinism: same seed -> identical gold result
        try:
            conn2, rows2 = _instantiate(problem, populate, seed)
            if rows2 != rows:
                return {"ok": False, "seed": seed, "stage": "determinism",
                        "detail": "re-running populate(conn, seed) with the same seed produced "
                                  "a DIFFERENT gold result; draw all randomness from "
                                  "random.Random(seed)."}
        except Exception:
            return {"ok": False, "seed": seed, "stage": "determinism",
                    "detail": traceback.format_exc(limit=4)}
        samples.append((seed, len(rows), rows[:3]))

    # 4) soft diversity: seeds should not all yield byte-identical gold output
    distinct = {repr(s[2]) for s in samples}
    diversity_warn = len(distinct) == 1 and len(seeds) > 1
    return {"ok": True, "samples": samples, "diversity_warn": diversity_warn}


# --------------------------------------------------------------------------- #
# per-problem authoring loop
# --------------------------------------------------------------------------- #
def author_problem(problem, model_name, seeds, max_attempts, log_fh, outdir=GEN_DIR) -> dict:
    import model as M

    history = []
    code = None
    result = None
    total_cost = 0.0
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            prompt = build_initial(problem)
        else:
            prompt = build_repair(problem, code, result["seed"], result["detail"])

        t0 = time.time()
        resp = M.call(model_name, prompt)
        cost = M.cost_usd(model_name, resp["prompt_tokens"], resp["completion_tokens"])
        total_cost += cost
        if resp["error"]:
            result = {"ok": False, "seed": None, "stage": "model",
                      "detail": f"model error: {resp['error']}"}
            code = code or ""
        else:
            code = extract_code(resp["text"])
            result = validate_generator(problem, code, seeds)

        rec = {
            "problem": problem.id, "model": model_name, "attempt": attempt,
            "ok": result["ok"], "stage": result.get("stage"),
            "detail": (result.get("detail") or "")[:800],
            "latency_s": round(resp["latency_s"], 2),
            "completion_tokens": resp["completion_tokens"],
            "cost_usd": round(cost, 6),
            "code_len": len(code or ""),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        log_fh.write(json.dumps(rec) + "\n")
        log_fh.flush()
        history.append(rec)

        status = "OK" if result["ok"] else f"FAIL@{result.get('stage')}"
        print(f"  [{problem.id}] attempt {attempt}/{max_attempts} "
              f"({resp['latency_s']:.1f}s) -> {status}"
              + ("" if result["ok"] else f": {result['detail'][:120].splitlines()[0] if result['detail'] else ''}"))

        if result["ok"]:
            freeze(problem, model_name, code, attempt, result, seeds, outdir)
            return {"problem": problem.id, "ok": True, "attempts": attempt,
                    "cost_usd": round(total_cost, 6),
                    "diversity_warn": result.get("diversity_warn", False)}

    return {"problem": problem.id, "ok": False, "attempts": max_attempts,
            "cost_usd": round(total_cost, 6),
            "last_stage": result.get("stage"), "last_detail": result.get("detail")}


def freeze(problem, model_name, code, attempts, result, seeds, outdir=GEN_DIR):
    header = (
        f'"""Frozen data generator for problem `{problem.id}` ({problem.title}).\n\n'
        f'Authored by model: {model_name}\n'
        f'Attempts to pass validation: {attempts}\n'
        f'Validated on seeds: {seeds}\n'
        f'Frozen: {datetime.now(timezone.utc).isoformat()}\n\n'
        f'Target clauses: {", ".join(problem.target_clauses)}\n'
        f'"""\n'
    )
    Path(outdir).mkdir(parents=True, exist_ok=True)
    (Path(outdir) / f"{problem.id}.py").write_text(header + "\n" + code + "\n")


# --------------------------------------------------------------------------- #
# self-test: validate the bank with hand-written reference generators
# --------------------------------------------------------------------------- #
def selftest(seeds):
    """Confirm every gold query is valid SQLite and every check is satisfiable,
    independent of any LLM, using the reference generators in reference_gens.py."""
    import reference_gens as RG
    ok = True
    for problem in bank.PROBLEMS:
        gen = getattr(RG, f"gen_{problem.id}", None)
        if gen is None:
            print(f"  [{problem.id}] NO reference generator — skipped")
            continue
        res = validate_callable(problem, gen, seeds)
        if res["ok"]:
            n = res["samples"][0][1]
            print(f"  [{problem.id}] OK  (gold rows on seed {seeds[0]}: {n})")
        else:
            ok = False
            print(f"  [{problem.id}] FAIL @ {res['stage']} (seed {res['seed']}): "
                  f"{(res['detail'] or '').splitlines()[-1][:160]}")
    print("\nSELFTEST", "PASSED" if ok else "FAILED")
    return ok


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="groq")
    ap.add_argument("--problems", default="all",
                    help="'all' or comma-separated problem ids")
    ap.add_argument("--k", type=int, default=len(DEFAULT_SEEDS),
                    help="number of seeds (K) to validate against")
    ap.add_argument("--attempts", type=int, default=5)
    ap.add_argument("--outdir", default=str(GEN_DIR),
                    help="where to freeze generators (default: generators/)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    seeds = list(range(1, args.k + 1))

    if args.selftest:
        raise SystemExit(0 if selftest(seeds) else 1)

    if args.problems == "all":
        probs = bank.PROBLEMS
    else:
        probs = [bank.get(pid.strip()) for pid in args.problems.split(",")]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = RUN_DIR / f"{args.model}_{stamp}.jsonl"
    print(f"model={args.model}  K={args.k}  attempts={args.attempts}  problems={len(probs)}")
    print(f"log -> {log_path}\n")

    summary = []
    with open(log_path, "w") as fh:
        for p in probs:
            print(f"== {p.id} ({p.difficulty}) ==")
            summary.append(author_problem(p, args.model, seeds, args.attempts, fh, args.outdir))

    n_ok = sum(1 for s in summary if s["ok"])
    tot_cost = sum(s["cost_usd"] for s in summary)
    print("\n=== SUMMARY ===")
    for s in summary:
        mark = "PASS" if s["ok"] else "FAIL"
        extra = f"in {s['attempts']} attempt(s)" if s["ok"] else f"@{s.get('last_stage')}"
        print(f"  {mark}  {s['problem']:22s} {extra}")
    print(f"\n{n_ok}/{len(summary)} problems solved · ${tot_cost:.4f} · log {log_path.name}")
    # write a machine-readable summary alongside the jsonl
    (RUN_DIR / f"{args.model}_{stamp}.summary.json").write_text(
        json.dumps({"model": args.model, "k": args.k, "attempts": args.attempts,
                    "n_ok": n_ok, "n_total": len(summary), "cost_usd": tot_cost,
                    "results": summary}, indent=2))


if __name__ == "__main__":
    main()
