"""Instructor authoring wrapper — turns (question, gold SQL) into a publishable problem.

Reuses the validated pieces of `populator/instructor_flow.py` and `populator/populate.py`:
infer the schema from (prompt, gold), derive the target clauses, surface the plain-English
nudges, then author a seeded data generator gated on the same robustness loop the research
flow uses. Returns everything the instructor UI previews and everything `publish.py` needs —
including the gold_sql, which stays instructor-private until baking strips it.

Authoring is slow (several model calls + a stress loop), so the FastAPI layer runs this in a
background job; this module is the synchronous worker.
"""
from __future__ import annotations

import io
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "tutor", ROOT / "populator", ROOT / "eval" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import edge_coverage as ec  # noqa: E402  populator/  (general tie-break coverage check)
import grader            # noqa: E402
import instructor_flow as IF  # noqa: E402  populator/
import populate as P     # noqa: E402  populator/
import predictor          # noqa: E402  webapp/backend/ — Feature A difficulty prediction
import state_core as sc   # noqa: E402  webapp/backend/ — state-mode (DDL/DML) authoring
import store             # noqa: E402
from problems.bank import Problem, _assert  # noqa: E402

_TIE_REQUIREMENT = (
    "If the gold query orders by more than one key, you MUST construct (unconditionally, every "
    "seed) at least two result rows that tie on the primary sort key, so the secondary sort key "
    "is actually exercised — never rely on chance for this tie.")

# default authoring budget for the interactive UI (smaller than the research run)
K = 6
ATTEMPTS = 5
STRESS = 60


def _sample_rows(schema: str, generator_src: str, gold_sql: str, seed: int = 1, cap: int = 8):
    """A small preview: the inferred tables with a few generated rows, plus the gold result —
    so the instructor can eyeball that the data is sane before publishing."""
    class _S:  # build_db only reads .schema
        schema = ""
    s = _S(); s.schema = schema
    populate = P.load_populate(generator_src)
    conn = grader.build_db(s, populate, seed)
    tables = {}
    for (tname,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        cur = conn.execute(f"SELECT * FROM {tname} LIMIT {cap}")
        cols = [d[0] for d in cur.description]
        tables[tname] = {"cols": cols, "rows": [list(r) for r in cur.fetchall()]}
    gcols, grows = grader.run_query(conn, gold_sql)
    conn.close()
    return {"tables": tables, "gold_preview": {"cols": list(gcols),
            "rows": [list(r) for r in grows[:cap]], "n_rows": len(grows)}}


def _make_confirmed_nudge_check(confirmed_nudges: list[dict]):
    """Each confirmed nudge's assert_sql (system-written, mirrors IF._run_asserts) must return
    >=1 row on the populated DB — otherwise the instructor-approved edge case never materialized."""
    def check(conn, rows):
        for n in confirmed_nudges:
            hit = conn.execute(n["assert_sql"]).fetchone() is not None
            _assert(hit, f"confirmed edge case is absent ({n['question']}): {n['assert_sql']}")
    return check


def author(prompt: str, gold_sql: str, model_name: str, title: str,
           difficulty: str = "medium", k: int = K, attempts: int = ATTEMPTS,
           stress: int = STRESS, confirmed_nudges: list[dict] | None = None,
           ddl: str | None = None, predict: bool = False) -> dict:
    """Synchronous authoring. Returns a dict with status='ok' and a problem record ready for
    `store.add_problem`, or status='error' with a reason.

    If `ddl` is given (assignment-mode batch authoring shares one inferred schema across
    problems), skip schema inference but still verify the gold query runs on it."""
    t0 = time.time()
    seeds = list(range(1, k + 1))

    # branch on the grading mode. detect_kind reads the gold ONCE here (still instructor-private);
    # SELECT keeps exactly today's flow, state takes the DDL/DML path below.
    if sc.detect_kind(gold_sql) == "state":
        return _author_state(prompt, gold_sql, model_name, title, difficulty,
                             k=k, attempts=attempts, stress=stress, ddl=ddl,
                             predict=predict, t0=t0)

    # 1. infer schema from (prompt, gold) only — unless a shared ddl was already inferred
    if ddl is None:
        ddl, meta = IF.infer_schema(prompt, gold_sql, model_name)
        if not ddl:
            return {"status": "error", "stage": "schema", "reason": meta.get("error", "no DDL")}
    runs, why = IF.schema_runs(ddl, gold_sql)
    if not runs:
        return {"status": "error", "stage": "schema",
                "reason": f"inferred schema does not run the gold query: {why}", "ddl": ddl}

    clauses = IF.derive_clauses(gold_sql)
    nudge_list = IF.nudges(gold_sql, ddl)   # plain-English yes/no questions for the instructor

    # 2+3. author a robust, diverse generator against the inferred schema + generic check
    # compose the generic non-triviality check with the general tie-break-coverage check, so an
    # authored problem with a multi-key ORDER BY can't ship data that leaves the tie-break untested
    confirmed_nudges = confirmed_nudges or []
    checks = [IF.make_generic_check(gold_sql), ec.make_tiebreak_check(gold_sql)]
    if confirmed_nudges:
        checks.append(_make_confirmed_nudge_check(confirmed_nudges))
    check = ec.compose_checks(*checks)
    tie_reqs = [_TIE_REQUIREMENT] if ec.tiebreak_columns(gold_sql) else []
    # one unconditional requirement per confirmed nudge, so the model builds the edge case
    # instead of relying on the check to catch its absence after the fact
    nudge_reqs = [
        f"REQUIRED edge case, build unconditionally on every seed: the instructor confirmed "
        f"\"{n['question']}\" — make this true of the generated data."
        for n in confirmed_nudges
    ]
    synth = Problem(
        id=store.slugify(title), title=title, difficulty=difficulty,
        schema=ddl, prompt=prompt, gold_sql=gold_sql,
        target_clauses=clauses, check=check,
        requirements=tie_reqs + nudge_reqs + [
            "Make the gold query return a non-trivial result (several rows, or a meaningful aggregate).",
            "Vary the data across seeds so different seeds give different datasets — do NOT hard-code one fixed dataset.",
            "Exercise every clause in the gold query BY CONSTRUCTION: include rows a WHERE/BETWEEN "
            "filter EXCLUDES (below and above any range); give any GROUP BY multiple groups; if an "
            "aggregate skips NULLs (SUM/AVG/MIN/MAX of a column), put at least one NULL there; if "
            "there is a LIMIT, create more candidates than the limit so it truncates; if there is a "
            "HAVING, make it exclude at least one group; if there is a JOIN, leave some rows unmatched.",
        ])

    with tempfile.TemporaryDirectory() as td:
        outdir = Path(td)
        log = io.StringIO()
        res = P.author_problem(synth, model_name, seeds, attempts, log, outdir=outdir)
        if not res.get("ok"):
            return {"status": "error", "stage": "author",
                    "reason": f"could not author a robust generator (failed at "
                              f"{res.get('last_stage')})", "ddl": ddl, "attempts": res.get("attempts")}
        generator_src = (outdir / f"{synth.id}.py").read_text()

    # 4. preview + a final stress confirmation
    stress_seeds = list(range(5000, 5000 + stress))
    populate = P.load_populate(generator_src)
    ok, swhy, ndistinct = IF.stress(synth, populate, stress_seeds)

    problem_record = {
        "id": synth.id, "title": title, "kind": "select", "difficulty": difficulty,
        "prompt": prompt, "gold_sql": gold_sql, "schema": ddl,
        "generator_src": generator_src, "target_clauses": clauses,
    }

    # 5. (optional) difficulty prediction — measured weak-student-vs-tutor run, ~1-3 min.
    # Attached to the problem record so it persists via store.add_problem; instructors later
    # compare it against real class performance (see app.py analytics).
    prediction = None
    if predict:
        prediction = predictor.predict(problem_record)
        problem_record["prediction"] = prediction

    return {
        "status": "ok",
        "kind": "select",
        "elapsed_s": round(time.time() - t0, 1),
        "problem": problem_record,
        "prediction": prediction,
        "nudges": [{"id": n["id"], "question": n["question"], "assert_sql": n["assert_sql"]}
                   for n in nudge_list],
        "enforced_nudges": [n["id"] for n in confirmed_nudges],
        "stress": {"ok": ok, "why": swhy, "distinct_datasets": ndistinct,
                   "n_seeds": len(stress_seeds)},
        "preview": _sample_rows(ddl, generator_src, gold_sql),
        "attempts": res.get("attempts"),
    }


# --------------------------------------------------------------------------- #
# STATE-MODE authoring (CREATE/INSERT/UPDATE/DELETE/DROP)
#
# The select path can't be reused: P.author_problem runs the gold as a SELECT, and IF's
# checks/nudges are result-set shaped. So state authoring has its own self-contained loop —
# the same model client + repair-retry rhythm, but gated on the deterministic state coverage
# gates in state_core (the DML edge-case invariant), not a SELECT non-triviality check.
# --------------------------------------------------------------------------- #
_TRIVIAL_GEN = "import random\n\n\ndef populate(conn, seed):\n    pass\n"

_STATE_GEN_SYS = """\
You write a Python data generator for a SQL teaching problem whose answer is a data-
MODIFICATION statement (CREATE/INSERT/UPDATE/DELETE/DROP), not a SELECT.

Write ONE function:

    def populate(conn, seed):
        ...

It inserts synthetic rows into the PRE-EXISTING tables (schema below) so that the gold
statement, when later run against your data, is meaningful and fully exercised.

Hard rules:
- Standard library only: `import random`. `sqlite3` is available via the given `conn` — do
  NOT reconnect. Do NOT CREATE or DROP tables; the schema already exists. Only INSERT rows.
- Deterministic in `seed`: `rng = random.Random(seed)`; draw ALL randomness from `rng`.
- Vary the data across seeds (different seeds -> different datasets). Several rows per table.
- Realistic values. Respect primary keys / uniqueness (never duplicate a PK).
- COVERAGE (build these BY CONSTRUCTION, every seed):
{coverage}
- Output ONLY one ```python code block with `def populate(conn, seed):`. No prose.
"""

_STATE_GEN_USER = """\
PROBLEM: {title}

QUESTION (the student must write the modification statement that does this):
{prompt}

PRE-EXISTING SCHEMA (already created; INSERT into these):
{schema}

Write `def populate(conn, seed):` now.
"""

_STATE_GEN_REPAIR = """\
Your previous `populate` FAILED a coverage gate.

YOUR PREVIOUS CODE:
```python
{code}
```

FAILURE (on seed {seed}): {why}

Fix the function so every seed satisfies the coverage rules. Keep the signature
`def populate(conn, seed):`. Output ONLY the corrected ```python code block.
"""


def _state_coverage_reqs(gold_sql: str) -> str:
    """Plain-English coverage requirements derived from the gold's statement types — the human
    side of state_core.make_state_gates, so the model builds the edge cases the gates check."""
    import sqlglot
    from sqlglot import exp
    reqs = []
    try:
        statements = [s for s in sqlglot.parse(gold_sql, read="sqlite") if s is not None]
    except Exception:
        statements = []
    for st in statements:
        if isinstance(st, exp.Update) and st.args.get("where") is not None:
            t = st.find(exp.Table)
            reqs.append(f"  * For the UPDATE on `{t.name if t else '?'}`: include rows that its "
                        f"WHERE matches (to be changed) AND rows it does NOT match (to stay "
                        f"unchanged) — at least one of each, every seed.")
        elif isinstance(st, exp.Delete) and st.args.get("where") is not None:
            t = st.find(exp.Table)
            reqs.append(f"  * For the DELETE on `{t.name if t else '?'}`: include rows its WHERE "
                        f"matches (to be deleted) AND rows it does NOT (to survive) — >=1 of each.")
        elif isinstance(st, exp.Insert):
            t = st.find(exp.Table)
            reqs.append(f"  * For the INSERT into `{t.name if t else '?'}`: pre-populate the table "
                        f"with some existing rows so the insert visibly grows it.")
        elif isinstance(st, exp.Drop):
            t = st.find(exp.Table)
            reqs.append(f"  * The DROP removes `{t.name if t else '?'}`: ensure another table also "
                        f"holds data so it's clear only the targeted table is dropped.")
    if not reqs:
        reqs.append("  * Populate the referenced tables with several varied rows so the statement "
                    "has real data to act on.")
    return "\n".join(reqs)


def _state_preview(schema: str, generator_src: str, gold_sql: str, seed: int = 1, cap: int = 6):
    """Pre-state (a few rows) + post-state after the gold, for the instructor to eyeball. Keeps
    the `tables` key (pre-state) for UI back-compat and adds `post_tables`."""
    populate = P.load_populate(generator_src)
    conn = grader.build_db(sc.tc._SchemaOnly(schema), populate, seed)
    pre = sc.snapshot_state(conn)
    conn.executescript(gold_sql)
    conn.commit()
    post = sc.snapshot_state(conn)
    conn.close()

    def _shape(snap):
        out = {}
        for name, t in snap["tables"].items():
            out[name] = {"cols": [c[0] for c in t["columns"]],
                         "rows": [list(r) for r in t["rows"][:cap]], "n_rows": t["n_rows"]}
        return out
    return {"tables": _shape(pre), "post_tables": _shape(post)}


def _author_state(prompt: str, gold_sql: str, model_name: str, title: str,
                  difficulty: str, k: int, attempts: int, stress: int,
                  ddl: str | None, predict: bool, t0: float) -> dict:
    """State-mode authoring. Mirrors author()'s contract (status ok/error + problem record),
    but bakes per-seed STATE, gates on the deterministic DML coverage gates, and never runs the
    gold as a SELECT."""
    import model as M

    seeds = list(range(1, k + 1))
    pid = store.slugify(title)

    # 1. PRE-schema. If a shared ddl is given, use it. Else if the gold only CREATEs (references
    # no pre-existing tables), the pre-schema is empty. Else infer the pre-state schema and verify
    # it with a STATE-aware check (the gold must run AND change the state).
    if ddl is not None:
        schema = ddl
        runs, why = sc.schema_runs_state(schema, gold_sql)
        if not runs:
            return {"status": "error", "kind": "state", "stage": "schema",
                    "reason": f"shared schema does not run the gold statement: {why}", "ddl": schema}
    elif sc.gold_only_creates(gold_sql):
        schema = ""
    else:
        schema, meta = IF.infer_schema(prompt, gold_sql, model_name)
        if not schema:
            return {"status": "error", "kind": "state", "stage": "schema",
                    "reason": meta.get("error", "no DDL")}
        runs, why = sc.schema_runs_state(schema, gold_sql)
        if not runs:
            return {"status": "error", "kind": "state", "stage": "schema",
                    "reason": f"inferred schema does not run the gold statement: {why}",
                    "ddl": schema}

    gate = sc.make_state_gates(gold_sql)

    def _pre_factory_for(generator_src, seed):
        populate = P.load_populate(generator_src)
        return lambda: grader.build_db(sc.tc._SchemaOnly(schema), populate, seed)

    def _validate(generator_src) -> tuple[bool, int | None, str]:
        """Run the coverage gates over the authoring seeds. Returns (ok, failing_seed, why)."""
        try:
            P.load_populate(generator_src)
        except Exception as e:  # noqa: BLE001
            return False, None, f"generator does not compile: {e}"
        for s in seeds:
            try:
                ok, why = gate(_pre_factory_for(generator_src, s))
            except Exception as e:  # noqa: BLE001 — fail-open at the call site, report the seed
                return False, s, f"gate raised: {e}"
            if not ok:
                return False, s, why
        return True, None, "ok"

    # 2. generator. Empty pre-schema -> trivial generator, no model call. Else author + gate-loop.
    if schema == "":
        generator_src = _TRIVIAL_GEN
        used_attempts = 0
    else:
        coverage = _state_coverage_reqs(gold_sql)
        base = _STATE_GEN_SYS.format(coverage=coverage) + "\n\n" + _STATE_GEN_USER.format(
            title=title, prompt=prompt.strip(), schema=schema.strip())
        generator_src = None
        used_attempts = 0
        last_why = "no attempt produced a passing generator"
        for attempt in range(1, attempts + 1):
            used_attempts = attempt
            if attempt == 1 or generator_src is None:
                resp = M.call(model_name, base)
            else:
                resp = M.call(model_name, base + "\n\n" + _STATE_GEN_REPAIR.format(
                    code=(generator_src or "").strip(), seed=fail_seed or seeds[0], why=last_why))
            if resp.get("error"):
                last_why = f"model error: {resp['error']}"
                continue
            cand = P.extract_code(resp["text"])
            ok, fail_seed, why = _validate(cand)
            generator_src = cand
            if ok:
                break
            last_why = why
        else:
            return {"status": "error", "kind": "state", "stage": "author",
                    "reason": f"could not author a generator passing the coverage gates: {last_why}",
                    "ddl": schema, "attempts": used_attempts}

    # 3. stress over a disjoint seed band with the same gates
    stress_seeds = list(range(5000, 5000 + stress))
    s_ok, s_why, s_fail = True, "ok", None
    for s in stress_seeds:
        try:
            ok, why = gate(_pre_factory_for(generator_src, s))
        except Exception as e:  # noqa: BLE001
            ok, why = False, f"gate raised: {e}"
        if not ok:
            s_ok, s_why, s_fail = False, why, s
            break

    problem_record = {
        "id": pid, "title": title, "kind": "state", "difficulty": difficulty,
        "prompt": prompt, "gold_sql": gold_sql, "schema": schema,
        "generator_src": generator_src, "target_clauses": [],
    }

    # 4. (optional) difficulty prediction — the state-mode sibling of the SELECT predictor.
    prediction = None
    if predict:
        prediction = predictor.predict_state(problem_record)
        problem_record["prediction"] = prediction

    return {
        "status": "ok",
        "kind": "state",
        "elapsed_s": round(time.time() - t0, 1),
        "problem": problem_record,
        "prediction": prediction,
        "nudges": [],            # v1: the deterministic gates above stand in for nudges
        "enforced_nudges": [],
        "stress": {"ok": s_ok, "why": s_why, "fail_seed": s_fail, "n_seeds": len(stress_seeds)},
        "preview": _state_preview(schema, generator_src, gold_sql),
        "attempts": used_attempts,
    }


def author_batch(table_hint: str, items: list[dict], model_name: str,
                  on_progress=None) -> dict:
    """Assignment-mode batch authoring: one shared table, several questions.

    Infers ONE schema for the whole set from `table_hint` + every item's gold_sql, then runs
    `author()` per item against that shared ddl. Keeps going past individual item failures —
    each entry in `items` of the result is an `author()` result (status 'ok' or 'error')."""
    result = author_batch_sections([{"table_hint": table_hint, "items": items}],
                                    model_name, on_progress=on_progress)
    if result["status"] != "ok":
        return result
    section = result["sections"][0]
    return {"status": "ok", "ddl": section["ddl"], "items": section["items"]}


def author_batch_sections(sections: list[dict], model_name: str,
                            on_progress=None) -> dict:
    """Multi-section assignment authoring: each section has its own table hint (own schema)
    and its own questions.

    `sections` = [{"table_hint": str, "items": [...]}]. Per section, infers the schema once
    from its table_hint + its items' gold_sql, then authors each item against it. The
    progress callback counts items across ALL sections (done/total/current title), so a
    single progress bar can track the whole job. Result: {status:'ok', sections:[{ddl,
    items:[...]}]} — one entry per input section, items in the same order as given. On a
    schema-inference failure for any section, returns status='error' immediately (mirrors
    the old single-section behavior)."""
    total = sum(len(s["items"]) for s in sections)
    done = 0
    out_sections = []
    for s in sections:
        table_hint = s["table_hint"]
        items = s["items"]
        ddl, meta = IF.infer_schema_for_set(table_hint, [it["gold_sql"] for it in items], model_name)
        if not ddl:
            return {"status": "error", "stage": "schema", "reason": meta.get("error", "no DDL")}

        results = []
        for it in items:
            if on_progress:
                on_progress(done, total, it["title"])
            res = author(it["prompt"], it["gold_sql"], model_name, it["title"],
                          it.get("difficulty", "medium"), confirmed_nudges=None, ddl=ddl)
            results.append(res)
            done += 1

        out_sections.append({"ddl": ddl, "items": results})

    return {"status": "ok", "sections": out_sections}
