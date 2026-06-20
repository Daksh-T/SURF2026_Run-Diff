"""Instructor-side authoring flow (the product pivot).

In the real product an instructor does NOT hand us a schema or a per-problem check — they
write only a natural-language QUESTION and the GOLD SQL (exactly what they already do to
set an exam).  From just those two, a SINGLE model (qwen2.5-coder-32b) must:

  1. INFER a SQLite schema (DDL) the gold query runs on,
  2. DERIVE a generic non-triviality check from the gold SQL's structure (no hand check),
  3. AUTHOR a seeded data generator (reusing populate.py's validate/repair loop), gated on
     robustness (200-seed stress) AND diversity (data must vary per seed).

This script VALIDATES that assumption on our 10 existing problems: it throws away their
hand-written schema and check, keeps only (prompt, gold), runs the flow, and scores the
result two ways — (a) against the derived generic check, and (b), for rigor, against the
ORIGINAL hand-written check we still have — to see whether automatic schema inference is
good enough to replace the hand-curated pipeline.

Run on Colab (needs Ollama + qwen2.5-coder:32b):
  python3 instructor_flow.py --model qwen32b --k 8 --attempts 6 --stress 200
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import sqlglot
from sqlglot import exp

import model as M
import populate as P
from problems import bank
from problems.bank import Problem, _assert  # reuse the assertion helper

HERE = Path(__file__).resolve().parent
OUTDIR = HERE / "generators_instructor"
RUNS = HERE / "runs"


# --------------------------------------------------------------------------- #
# 1. schema inference
# --------------------------------------------------------------------------- #
_SCHEMA_SYS = """\
You design a minimal SQLite schema for a SQL teaching question.

You are given a natural-language QUESTION and the GOLD QUERY that answers it. Output the
CREATE TABLE statements (SQLite dialect) for exactly the tables and columns the gold query
references — no more. Rules:
- Use the EXACT table and column names that appear in the gold query (so the query runs
  verbatim). Add a sensible INTEGER PRIMARY KEY per table.
- Pick natural column types (INTEGER for ids/counts/amounts, TEXT for names/labels). Make
  columns the query filters/sorts on nullable unless they are a primary key.
- Do NOT add unrelated tables or columns. Do NOT insert any data.
- Output ONLY a single ```sql code block with the CREATE TABLE statements.
"""

_SCHEMA_USER = """\
QUESTION:
{prompt}

GOLD QUERY:
{gold}

Write the CREATE TABLE statement(s) now.
"""

_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def infer_schema(prompt: str, gold_sql: str, model_name: str) -> tuple[str, dict]:
    resp = M.call(model_name, _SCHEMA_SYS + "\n\n" + _SCHEMA_USER.format(
        prompt=prompt.strip(), gold=gold_sql.strip()))
    if resp["error"]:
        return "", {"error": resp["error"], "latency_s": resp["latency_s"]}
    blocks = _SQL_FENCE.findall(resp["text"] or "")
    ddl = (blocks[0] if blocks else resp["text"] or "").strip()
    # keep only CREATE ... ; statements, drop stray prose
    if "create" in ddl.lower():
        m = re.search(r"(create\s+table.*)", ddl, re.IGNORECASE | re.DOTALL)
        if m:
            ddl = m.group(1).strip()
    return ddl, {"error": None, "latency_s": resp["latency_s"],
                 "completion_tokens": resp["completion_tokens"]}


_SCHEMA_SET_SYS = """\
You design ONE minimal SQLite schema shared by a whole problem set. The instructor gives the
table(s) the questions run against and the list of GOLD QUERIES (the answers). Output CREATE
TABLE statement(s) that ALL the gold queries run against verbatim. Rules:
- Use the EXACT table and column names that appear across the gold queries.
- One schema for the whole set (the questions share the same table(s)).
- Natural column types; INTEGER PRIMARY KEY per table; make filtered/sorted columns nullable.
- No extra tables/columns; insert no data. Output ONLY a single ```sql code block.
"""


def infer_schema_for_set(table_hint: str, golds: list[str], model_name: str) -> tuple[str, dict]:
    """Infer ONE schema for a whole question set (the realistic case: an instructor writes many
    questions against the same table). Returns (ddl, meta)."""
    body = (f"TABLE(S) THE QUESTIONS RUN AGAINST: {table_hint}\n\nGOLD QUERIES (the answers):\n"
            + "\n".join(f"{i+1}. {g.strip()}" for i, g in enumerate(golds))
            + "\n\nWrite the CREATE TABLE statement(s) now.")
    resp = M.call(model_name, _SCHEMA_SET_SYS + "\n\n" + body)
    if resp["error"]:
        return "", {"error": resp["error"], "latency_s": resp["latency_s"]}
    blocks = _SQL_FENCE.findall(resp["text"] or "")
    ddl = (blocks[0] if blocks else resp["text"] or "").strip()
    if "create" in ddl.lower():
        m = re.search(r"(create\s+table.*)", ddl, re.IGNORECASE | re.DOTALL)
        if m:
            ddl = m.group(1).strip()
    return ddl, {"error": None, "latency_s": resp["latency_s"]}


def schema_runs(ddl: str, gold_sql: str) -> tuple[bool, str]:
    """Does the gold query execute against an EMPTY db built from the inferred schema?"""
    try:
        conn = sqlite3.connect(":memory:")
        conn.executescript(ddl)
    except sqlite3.Error as e:
        return False, f"DDL error: {e}"
    try:
        conn.execute(gold_sql).fetchall()
    except sqlite3.Error as e:
        return False, f"gold query does not run on inferred schema: {e}"
    finally:
        conn.close()
    return True, "ok"


def table_cols(ddl_or_conn) -> dict:
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl_or_conn)
    out = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        out[t] = {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
    conn.close()
    return out


# --------------------------------------------------------------------------- #
# 2. SQL parsing (sqlglot AST) — clause detection, query rewriting, nudges
#
# The gold query is parsed to a sqlglot AST instead of scanned with regexes.  The big win is
# that TOP-LEVEL clauses come for free: a subquery's WHERE/MIN()/LIMIT live nested inside the
# select expressions, NOT in the outer node's `.args`, so "is this aggregate/filter at the top
# level?" is just `ast.args.get(...)` / inspecting `ast.expressions` — no paren-depth counting,
# no eating the aggregate's own parens.  Query rewrites (strip the WHERE/HAVING/LIMIT and
# re-run) become `ast.copy().set(key, None).sql()` instead of fragile substring surgery.
# --------------------------------------------------------------------------- #
_NULL_SKIPPING_AGG = (exp.Sum, exp.Avg, exp.Min, exp.Max)  # Count does not skip NULLs


def parse_sql(gold_sql: str):
    """Parse the gold query to a sqlglot AST (the outer SELECT). Returns None on failure;
    callers degrade to a lenient check rather than crash."""
    try:
        return sqlglot.parse_one(gold_sql, read="sqlite")
    except Exception:
        return None


def _without(ast, *keys: str) -> str:
    """The query with the given top-level clause(s) removed, as SQL (for the rewrite checks)."""
    c = ast.copy()
    for k in keys:
        c.set(k, None)
    return c.sql(dialect="sqlite")


def _select_aggs(ast) -> list:
    """Aggregate calls in the TOP-LEVEL select list (a subquery's MIN() is not one of these)."""
    out = []
    for e in ast.expressions:
        node = e.this if isinstance(e, exp.Alias) else e
        if isinstance(node, exp.AggFunc):
            out.append(node)
    return out


def _agg_base_cols(ast) -> list[str]:
    """Base columns DIRECTLY aggregated by a NULL-skipping aggregate — i.e. `SUM/AVG/MIN/MAX(col)`
    over a *bare* column only.

    The NULL-skipping nudge teaches "the aggregate silently drops NULL rows", which is a clean,
    real teaching point ONLY when the aggregate's argument is the column itself. It is deliberately
    NOT raised when the argument is a derived expression:
      * `AVG(LENGTH(content))` — the value aggregated is a length, not `content`; a NULL `content`
        is an indirect, weak case AND forcing one can fight the query's real constraints.
      * `SUM(player_1_throw = 'r')` — a conditional COUNT, not an aggregate *of* the column; a NULL
        throw is not the intended lesson.
    Misfiring here is exactly what broke hw2_authors_nested (a forced-NULL gate the author loop
    could never satisfy) and cost hw2_throw_counts a wasted attempt, so we match only `AGG(Column)`.
    `find_all` still reaches aggregates nested in subqueries (e.g. a scalar `AVG(col)` threshold),
    but each must have a column as its *direct* argument to count."""
    cols = []
    for f in ast.find_all(*_NULL_SKIPPING_AGG):
        if isinstance(f.this, exp.Column):
            cols.append(f.this.name)
    return list(dict.fromkeys(cols))


def _between(ast):
    """First BETWEEN over a plain column with integer bounds -> (col, lo, hi), else None."""
    for b in ast.find_all(exp.Between):
        if isinstance(b.this, exp.Column):
            try:
                return b.this.name, int(b.args["low"].name), int(b.args["high"].name)
            except (KeyError, ValueError, AttributeError):
                continue
    return None


def _order_keys(ast) -> list:
    """Top-level ORDER BY keys as (base_column_or_None, sql_text), in order."""
    o = ast.args.get("order")
    if not o:
        return []
    return [(k.this.name if isinstance(k.this, exp.Column) else None, k.this.sql())
            for k in o.expressions]


def _equi_join(ast):
    """First INNER-join equality `a.x = b.y` as (table_a, col_a, table_b, col_b), resolving
    aliases via the FROM/JOIN tables. Inequality or self-joins (e.g. `s1.x < s2.x`) return None
    — they have no parent/child 'unmatched row' to nudge about."""
    amap = {t.alias_or_name: t.name for t in ast.find_all(exp.Table)}
    for j in ast.args.get("joins", []):
        if (j.args.get("kind") or "INNER").upper() not in ("INNER", ""):
            continue
        on = j.args.get("on")
        eq = on if isinstance(on, exp.EQ) else (on.find(exp.EQ) if on else None)
        if not eq or not isinstance(eq.this, exp.Column) or not isinstance(eq.expression, exp.Column):
            continue
        l, r = eq.this, eq.expression
        lt, rt = amap.get(l.table, l.table), amap.get(r.table, r.table)
        if lt and rt:
            return lt, l.name, rt, r.name
    return None


def _having_count_boundary(ast):
    """Find a `HAVING COUNT(*) >= k` (or `> k`) on a single GROUP BY key, top-level or nested in a
    subquery, and return (boundary_subquery_sql, boundary_size, cutoff) where the subquery yields
    one row per group as (key, _cnt) with the HAVING removed.  `boundary_size` is the group size a
    student with an off-by-one threshold would wrongly include (k-1 for `>=k`, k for `>k`); `cutoff`
    is the smallest group size the gold keeps.  Returns None if no such HAVING.  Sound: it only
    builds a checkable assert; it never rejects valid data on its own."""
    for sel in ast.find_all(exp.Select):
        having, grp = sel.args.get("having"), sel.args.get("group")
        if not having or not grp or len(grp.expressions) != 1:
            continue
        cond = having.this
        if not isinstance(cond, (exp.GTE, exp.GT)):
            continue
        cnt, lit = cond.this, cond.expression
        # COUNT(*) on the left, integer literal on the right
        if not isinstance(cnt, exp.Count) or not isinstance(cnt.this, exp.Star):
            continue
        try:
            k = int(lit.name)
        except (ValueError, AttributeError):
            continue
        boundary = k - 1 if isinstance(cond, exp.GTE) else k
        cutoff = boundary + 1
        if boundary < 1:
            continue  # nothing below the line to construct
        inner = sel.copy()
        inner.set("having", None)
        # the rewritten projections are (key, _cnt); an ORDER BY / LIMIT referencing dropped
        # aliases would not resolve — and neither matters for an existence check
        inner.set("order", None)
        inner.set("limit", None)
        key_sql = grp.expressions[0].sql(dialect="sqlite")
        inner.set("expressions", [sqlglot.parse_one(key_sql, read="sqlite"),
                                  sqlglot.parse_one("COUNT(*) AS _cnt", read="sqlite")])
        return inner.sql(dialect="sqlite"), boundary, cutoff
    return None


def _comparison_boundaries(ast) -> list[tuple[str, str]]:
    """Plain `col >= literal` / `> / <= / <` comparisons in WHERE clauses, as
    (column_name, literal_sql).  The exact-boundary row is the witness that separates `>` from
    `>=`; without it both grade identically and an off-by-one filter passes.  BETWEEN has its own
    nudge; equality and column-vs-column comparisons have no boundary to construct."""
    out, seen = [], set()
    for sel in ast.find_all(exp.Select):
        where = sel.args.get("where")
        if not where:
            continue
        for cmp_ in where.find_all(exp.GTE, exp.GT, exp.LTE, exp.LT):
            col, lit = cmp_.this, cmp_.expression
            if isinstance(col, exp.Literal):           # literal on the left: flip
                col, lit = lit, col
            if isinstance(col, exp.Column) and isinstance(lit, exp.Literal):
                key = (col.name, lit.sql(dialect="sqlite"))
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


def _set_op_overlap(ast):
    """For a top-level UNION (distinct) or EXCEPT, return (left_sql, right_sql, kind).
    The overlap witness — a value present on BOTH sides — is what makes the operator matter:
    without it UNION == UNION ALL and EXCEPT removes nothing."""
    node = ast
    if isinstance(node, exp.Select):
        return None
    if isinstance(node, exp.Union) and node.args.get("distinct", True) is False:
        return None                                    # UNION ALL: duplicates kept by design
    if isinstance(node, (exp.Union, exp.Except)):
        left, right = node.this.copy(), node.expression.copy()
        for side in (left, right):
            if hasattr(side, "set"):
                side.set("order", None)
        kind = "EXCEPT" if isinstance(node, exp.Except) else "UNION"  # Except subclasses Union
        return left.sql(dialect="sqlite"), right.sql(dialect="sqlite"), kind
    return None


def derive_clauses(gold_sql: str) -> list[str]:
    ast = parse_sql(gold_sql)
    if ast is None:
        return ["SELECT"]
    found = []
    if _between(ast):
        found.append("BETWEEN")
    if ast.args.get("group"):
        found.append("GROUP BY")
    if ast.args.get("having"):
        found.append("HAVING")
    if ast.args.get("joins"):
        found.append("JOIN")
    if ast.args.get("limit"):
        found.append("LIMIT")
    if ast.find(exp.Distinct):
        found.append("DISTINCT")
    if ast.args.get("order"):
        found.append("ORDER BY (top-level)")
    if list(ast.find_all(exp.AggFunc)):
        found.append("aggregate")
    if len(list(ast.find_all(exp.Select))) > 1:
        found.append("subquery")
    return found or ["SELECT"]


def make_generic_check(gold_sql: str):
    """A lenient, structure-derived non-triviality check (used by --check generic)."""
    ast = parse_sql(gold_sql)
    has_group = ast is not None and ast.args.get("group") is not None
    has_limit = ast is not None and ast.args.get("limit") is not None
    is_scalar_agg = ast is not None and not has_group and not has_limit and bool(_select_aggs(ast))

    def check(conn: sqlite3.Connection, rows: list[tuple]):
        if is_scalar_agg:
            _assert(len(rows) >= 1 and any(v is not None for v in rows[0]),
                    "scalar aggregate result is empty/NULL — not a usable example")
            return
        _assert(len(rows) >= 2,
                f"gold result has {len(rows)} row(s); want >= 2 for a non-trivial example")
        if has_group:
            _assert(len({r[0] for r in rows}) >= 2,
                    "GROUP BY produced only one group — not exercised")
    return check


def _tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]


def _has_col(conn, col):
    for t in _tables(conn):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({t})").fetchall()}
        if col in cols:
            return t
    return None


def _schema_tables(ddl: str) -> dict:
    """{table: set(columns)} from a CREATE TABLE DDL string, plus {table: pk_col}."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl)
    cols, pks = {}, {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        info = conn.execute(f"PRAGMA table_info({t})").fetchall()
        cols[t] = {r[1] for r in info}
        pk = [r[1] for r in info if r[5]]  # r[5] = pk flag
        pks[t] = pk[0] if pk else None
    conn.close()
    return cols, pks


def _table_of(cols: dict, col: str):
    for t, cs in cols.items():
        if col in cs:
            return t
    return None


def _run_asserts(conn, instructor_asserts):
    """Each confirmed-nudge assert (system-written SQL) must return >=1 row."""
    for a in (instructor_asserts or []):
        try:
            hit = conn.execute(a).fetchone() is not None
        except sqlite3.Error as e:
            raise AssertionError(f"instructor assertion did not run ({e}): {a}")
        _assert(hit, f"instructor-specified edge case is absent: {a}")


def nudges(gold_sql: str, schema_ddl: str) -> list[dict]:
    """Edge-case INTENT that can't be soundly inferred from SQL structure (round 3): a tie on a
    multi-key ORDER BY, NULL/boundary handling in a BETWEEN filter, NULL-skipping aggregates, and
    a JOIN that excludes rows.

    For each, the SYSTEM writes the verification SQL itself (it knows the columns from the gold
    query's AST + the inferred schema) and asks the instructor a plain-English YES/NO question.
    The instructor never writes SQL — they just confirm intent. A confirmed nudge's `assert_sql`
    is added to the authoring check, guaranteeing the data can actually expose that mistake (so a
    student who gets the clause wrong is graded wrong, not falsely 'correct')."""
    ast = parse_sql(gold_sql)
    if ast is None:
        return []
    cols, pks = _schema_tables(schema_ddl)
    out = []

    keys = _order_keys(ast)
    if len(keys) >= 2 and keys[0][0] and _table_of(cols, keys[0][0]):
        key, t = keys[0][0], _table_of(cols, keys[0][0])
        second = keys[1][1].split(".")[-1]
        out.append({
            "id": "tie", "column": key,
            "question": f"I noticed your answer sorts by {key} first, then by {second}. To test "
                        f"the tie-break properly I need to generate data where two rows have the "
                        f"exact same {key}. Is that okay?",
            "assert_sql": f"SELECT 1 FROM {t} GROUP BY {key} HAVING COUNT(*) > 1 LIMIT 1"})

    bet = _between(ast)
    if bet and _table_of(cols, bet[0]):
        col, lo, hi = bet
        t = _table_of(cols, col)
        out.append({
            "id": "between_boundary", "column": col,
            "question": f"Your filter keeps {col} between {lo} and {hi}. Should I include a row "
                        f"sitting exactly on the boundary ({lo} or {hi}) to test that the edges "
                        f"are handled correctly?",
            "assert_sql": f"SELECT 1 FROM {t} WHERE {col} IN ({lo}, {hi}) LIMIT 1"})
        out.append({
            "id": "filter_null", "column": col,
            "question": f"Should I also include some rows where {col} is missing (NULL), to check "
                        f"they're correctly left out of the result?",
            "assert_sql": f"SELECT 1 FROM {t} WHERE {col} IS NULL LIMIT 1"})

    # NULL-skipping aggregates: SUM/AVG/MIN/MAX silently skip NULLs — a teaching point only if the
    # data actually has some. Ask per aggregated base column.
    for col in _agg_base_cols(ast):
        t = _table_of(cols, col)
        if t:
            out.append({
                "id": f"agg_null_{col}", "column": col,
                "question": f"Your answer aggregates {col} (SUM/AVG/MIN/MAX skip NULLs). Should "
                            f"some {col} values be missing (NULL), to check they're correctly "
                            f"skipped?",
                "assert_sql": f"SELECT 1 FROM {t} WHERE {col} IS NULL LIMIT 1"})

    # comparison boundary (>=, >, <=, <) against a literal: the exact-boundary row is what
    # separates `>` from `>=` — without it an off-by-one filter grades correct.
    for col, lit in _comparison_boundaries(ast):
        t = _table_of(cols, col)
        if t and not (bet and bet[0] == col):          # BETWEEN already nudges this column
            out.append({
                "id": f"boundary_{col}", "column": col,
                "question": f"Your filter compares {col} against {lit}. Should I include a row "
                            f"where {col} is exactly {lit}, so a student who writes the wrong "
                            f"comparison (> instead of >=, or vice versa) is caught?",
                "assert_sql": f"SELECT 1 FROM {t} WHERE {col} = {lit} LIMIT 1"})

    # set-operation overlap: UNION (distinct) only differs from UNION ALL — and EXCEPT only
    # removes anything — when some value appears on BOTH sides.
    so = _set_op_overlap(ast)
    if so:
        left_sql, right_sql, kind = so
        why = ("removing duplicates is actually tested" if kind == "UNION"
               else "the subtraction actually removes something")
        out.append({
            "id": "setop_overlap", "column": None,
            "question": f"Your answer combines two lists with {kind}. Should some value appear "
                        f"in BOTH lists, so {why}?",
            "assert_sql": f"SELECT 1 FROM ({left_sql} INTERSECT {right_sql}) LIMIT 1"})

    # HAVING COUNT(*) >= k boundary: without a group sitting exactly one below the cutoff, a
    # student who writes the wrong threshold (>= k-1) is graded CORRECT. Force the boundary group.
    hb = _having_count_boundary(ast)
    if hb:
        sub_sql, boundary, cutoff = hb
        out.append({
            "id": "having_boundary", "column": None,
            "question": f"Your answer keeps only groups with a count of at least {cutoff}. To test "
                        f"that cutoff properly I want a group with exactly {boundary} matching "
                        f"row(s) — just below the line — so a student who uses the wrong threshold "
                        f"is caught. Is that okay?",
            "assert_sql": f"SELECT 1 FROM ({sub_sql}) WHERE _cnt = {boundary} LIMIT 1"})

    ej = _equi_join(ast)
    if ej:
        lt, lc, rt, rc = ej
        # parent = side whose join column is its PK (the 'one' side, e.g. players), so an unmatched
        # PARENT row is the natural exclusion; fall back to the joined (right) table as parent
        if pks.get(lt) == lc:
            pt, pc, ct, cc = lt, lc, rt, rc
        else:
            pt, pc, ct, cc = rt, rc, lt, lc
        out.append({
            "id": "join_exclusion", "column": None,
            "question": f"Your answer joins {ct} to {pt}. To test the join properly I want some "
                        f"{pt} rows that have NO matching {ct} (so the join correctly leaves them "
                        f"out). Is that okay?",
            "assert_sql": f"SELECT 1 FROM {pt} p WHERE NOT EXISTS "
                          f"(SELECT 1 FROM {ct} c WHERE c.{cc} = p.{pc}) LIMIT 1"})
    return out


def make_clause_check(gold_sql: str, instructor_asserts: list[str] | None = None):
    """A stronger, clause-aware stand-in for a hand check: it reads the gold SQL and, for
    each clause it finds, demands the edge case that makes that clause *matter* — derived
    mostly by query rewriting (strip LIMIT/HAVING and compare), so it needs no knowledge of
    the problem beyond the gold SQL itself.  `instructor_asserts` are optional one-line SQL
    snippets (answers to nudges()) that each must return >=1 row — the human-in-the-loop way
    to enforce intent the structure can't reveal (ties, JOIN exclusions)."""
    ast = parse_sql(gold_sql)
    if ast is None:  # unparseable: degrade to non-empty + instructor asserts (never crash)
        def lenient(conn, rows):
            _assert(len(rows) >= 1, "gold result is empty")
            _run_asserts(conn, instructor_asserts)
        return lenient

    has_where = ast.args.get("where") is not None
    has_group = ast.args.get("group") is not None
    has_having = ast.args.get("having") is not None
    has_limit = ast.args.get("limit") is not None
    # scalar aggregate = an aggregate in the TOP-LEVEL select (a subquery MIN() doesn't count)
    is_scalar_agg = not has_group and not has_limit and bool(_select_aggs(ast))
    bet = _between(ast)
    # query rewrites for the exclusion checks (clean AST surgery, not substring hacking)
    sql_no_where = _without(ast, "where") if has_where else None
    sql_no_having = _without(ast, "having") if has_having else None
    sql_no_limit = _without(ast, "limit", "offset") if has_limit else None
    # NOTE: NULL-skipping for an aggregate is problem-specific intent (some aggregates have no
    # NULLs, e.g. a wins total) — it's a nudge, not an automatic rule, so it isn't enforced here.

    def _run(conn, sql):
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error:
            return None

    def check(conn: sqlite3.Connection, rows: list[tuple]):
        if is_scalar_agg:
            _assert(len(rows) >= 1 and rows[0] and rows[0][0] not in (None, 0),
                    "scalar aggregate is empty/zero — not a usable example (e.g. a COUNT must "
                    "match at least one row)")
        else:
            # LIMIT/OFFSET ("11th largest", "top 12") legitimately returns few rows; a plain
            # list query should return at least a couple.
            need = 1 if has_limit else 2
            _assert(len(rows) >= need,
                    f"gold result has {len(rows)} row(s); want >= {need} for a usable example")
        if has_group:
            _assert(len({r[0] for r in rows}) >= 2,
                    "GROUP BY produced only one group — not exercised")
        if has_where and not bet:
            # a WHERE that excludes nothing means a student who omits it gets the same answer
            full = _run(conn, sql_no_where)
            if full is not None:
                if is_scalar_agg:
                    _assert(bool(full) and bool(rows) and full[0] != rows[0],
                            "the WHERE filter doesn't change the result — dropping it gives the "
                            "same answer, so a student who omits it can't be caught")
                else:
                    _assert(len(full) > len(rows),
                            "the WHERE filter excludes no rows — a student who omits it can't be caught")
        if has_limit:
            full = _run(conn, sql_no_limit)
            if full is not None:
                _assert(len(full) > len(rows),
                        "LIMIT never truncates — fewer candidate rows than the limit")
        if has_having:
            no_having = _run(conn, sql_no_having)
            if no_having is not None:
                _assert(len(no_having) > len(rows),
                        "HAVING excludes nobody — every group already satisfies it")
        if bet:
            col, lo, hi = bet
            t = _has_col(conn, col)
            if t:
                below = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} < ?", (lo,)).fetchone()[0]
                above = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {col} > ?", (hi,)).fetchone()[0]
                _assert(below >= 1 and above >= 1,
                        "no rows outside the BETWEEN range — the filter is not exercised")
        _run_asserts(conn, instructor_asserts)
    return check


# --------------------------------------------------------------------------- #
# 3. run the flow over the bank and score it
# --------------------------------------------------------------------------- #
def stress(problem, populate, seeds) -> tuple[bool, str, int]:
    """Robustness + diversity of a generator under a Problem (schema+check+gold)."""
    res = P.validate_callable(problem, populate, seeds)
    if not res["ok"]:
        return False, f"{res['stage']} @ seed {res['seed']}", 0
    outs = set()
    for sd in seeds:
        conn = sqlite3.connect(":memory:")
        conn.executescript(problem.schema)
        populate(conn, sd)
        conn.commit()
        outs.add(repr(conn.execute(problem.gold_sql).fetchall()))
        conn.close()
    return True, "ok", len(outs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen32b")
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--attempts", type=int, default=6)
    ap.add_argument("--stress", type=int, default=200)
    ap.add_argument("--problems", default="all")
    ap.add_argument("--check", choices=["generic", "clause"], default="clause",
                    help="auto-derived check strength used during authoring")
    ap.add_argument("--set", choices=["bank", "extra"], default="bank",
                    help="bank = the 10 CS284 problems (have hand checks to score against); "
                         "extra = fresh made-up problems in new domains (generalization test, "
                         "no hand check)")
    ap.add_argument("--asserts", action="store_true",
                    help="auto-generate nudges from (gold + inferred schema) and confirm them "
                         "all (stands in for the instructor answering YES) during authoring")
    args = ap.parse_args()

    seeds = list(range(1, args.k + 1))
    stress_seeds = list(range(5000, 5000 + args.stress))
    has_handcheck = args.set == "bank"
    if args.set == "extra":
        import extra_problems
        source = extra_problems.PROBLEMS
    else:
        source = bank.PROBLEMS
    probs = source if args.problems == "all" else \
        [p for p in source if p.id in {x.strip() for x in args.problems.split(",")}]

    OUTDIR.mkdir(exist_ok=True)
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log = (RUNS / f"instructor_{stamp}.jsonl").open("w")
    print(f"Instructor-flow validation · model={args.model} · K={args.k} · "
          f"attempts={args.attempts} · stress={args.stress}\n")

    report = []
    for orig in probs:
        rec = {"id": orig.id}
        # ---- step 1: infer schema from (prompt, gold) only ----
        ddl, meta = infer_schema(orig.prompt, orig.gold_sql, args.model)
        runs, why = schema_runs(ddl, orig.gold_sql) if ddl else (False, meta.get("error", "no ddl"))
        rec["schema_infer_s"] = round(meta.get("latency_s", 0), 1)
        rec["schema_runs"] = runs
        # column overlap with the real hand-written schema
        try:
            real_cols = table_cols(orig.schema)
            inf_cols = table_cols(ddl) if runs else {}
            real_set = {(t, c) for t, cs in real_cols.items() for c in cs}
            inf_set = {(t, c) for t, cs in inf_cols.items() for c in cs}
            rec["col_match"] = (f"{len(real_set & inf_set)}/{len(real_set)} real cols present"
                                if real_set else "n/a")
        except Exception:
            rec["col_match"] = "n/a"

        if not runs:
            rec["status"] = f"SCHEMA-FAIL: {why}"
            print(f"  {orig.id:22s} SCHEMA-FAIL: {why}")
            report.append(rec); log.write(json.dumps({**rec, "ddl": ddl}) + "\n"); log.flush()
            continue

        # ---- step 2+3: author a generator against the INFERRED schema + generic check ----
        if args.check == "generic":
            check_obj = make_generic_check(orig.gold_sql)
        else:
            # auto-generate nudges from (gold + inferred schema) and confirm all (the validation
            # stands in for an instructor answering YES); their system-written asserts gate authoring
            auto = [n["assert_sql"] for n in nudges(orig.gold_sql, ddl)] if args.asserts else None
            check_obj = make_clause_check(orig.gold_sql, auto)
        synth = replace(orig, schema=ddl, check=check_obj,
                        target_clauses=derive_clauses(orig.gold_sql),
                        requirements=[
                            "Make the gold query return a non-trivial result (several rows, "
                            "or a meaningful aggregate).",
                            "Vary the data across seeds so different seeds give different "
                            "datasets — do NOT hard-code one fixed dataset.",
                            "Exercise every clause in the gold query BY CONSTRUCTION: include "
                            "rows that a WHERE/BETWEEN filter EXCLUDES (both below and above any "
                            "range); give any GROUP BY multiple groups; if an aggregate skips "
                            "NULLs (SUM/AVG/MIN/MAX of a column), put at least one NULL in that "
                            "column; if there is a LIMIT, create more candidate rows than the "
                            "limit so it truncates; if there is a HAVING, make it exclude at "
                            "least one group; if there is a JOIN, leave some rows unmatched.",
                        ])
        t0 = time.time()
        res = P.author_problem(synth, args.model, seeds, args.attempts, log, outdir=OUTDIR)
        rec["authored"] = res["ok"]
        rec["attempts"] = res.get("attempts")
        rec["author_s"] = round(time.time() - t0, 1)
        if not res["ok"]:
            rec["status"] = f"AUTHOR-FAIL @ {res.get('last_stage')}"
            print(f"  {orig.id:22s} schema✓ ({rec['col_match']}) · AUTHOR-FAIL @ {res.get('last_stage')}")
            report.append(rec); log.write(json.dumps(rec) + "\n"); log.flush()
            continue

        # ---- score: stress with generic check, AND with the ORIGINAL hand check ----
        populate = P.load_populate((OUTDIR / f"{orig.id}.py").read_text())
        g_ok, g_why, g_div = stress(synth, populate, stress_seeds)
        rec["stress_generic"] = f"PASS {g_div}/{len(stress_seeds)} distinct" if g_ok else f"FAIL {g_why}"
        if has_handcheck:
            eval_problem = replace(orig, schema=ddl)  # inferred schema + ORIGINAL hand check + gold
            h_ok, h_why, _ = stress(eval_problem, populate, stress_seeds)
            # a hand-check *error* (not an assertion failure) means the old hand check queries a
            # column the minimal inferred schema doesn't have — an eval artifact, not a real miss
            if h_ok:
                rec["stress_handcheck"] = "PASS"
            elif "check-error" in h_why:
                rec["stress_handcheck"] = "N/A (hand check needs a column the question never uses)"
            else:
                rec["stress_handcheck"] = f"FAIL {h_why}"
            rec["status"] = "OK" if (g_ok and h_ok) else ("PARTIAL" if g_ok else "WEAK")
        else:
            rec["stress_handcheck"] = "n/a (no hand check — generalization set)"
            rec["status"] = "OK" if g_ok else "WEAK"
        print(f"  {orig.id:22s} schema✓ ({rec['col_match']}) · authored in {rec['attempts']} · "
              f"generic:{rec['stress_generic']} · handcheck:{rec['stress_handcheck']}")
        report.append(rec); log.write(json.dumps(rec) + "\n"); log.flush()

    log.close()
    summ = (RUNS / f"instructor_{stamp}.summary.json")
    summ.write_text(json.dumps(report, indent=2))
    nschema = sum(1 for r in report if r.get("schema_runs"))
    nauth = sum(1 for r in report if r.get("authored"))
    print(f"\n=== INSTRUCTOR-FLOW SUMMARY (model {args.model}, set={args.set}) ===")
    print(f"  schema inferred & gold runs : {nschema}/{len(report)}")
    print(f"  generator authored (robust + diverse) : {nauth}/{len(report)}")
    if has_handcheck:
        nhand = sum(1 for r in report if r.get("stress_handcheck") == "PASS")
        nna = sum(1 for r in report if str(r.get("stress_handcheck")).startswith("N/A"))
        print(f"  ALSO passes original hand check (200 seeds): {nhand}/{len(report)}"
              + (f"  (+{nna} N/A eval-artifact)" if nna else ""))
    print(f"  summary -> {summ.name}")


if __name__ == "__main__":
    main()
