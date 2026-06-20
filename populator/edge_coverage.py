"""General edge-coverage checks for gold queries — the data must EXERCISE every clause that
can silently make grading lenient, not just include it.

The motivating bug: a gold `ORDER BY population, city_name` whose seeded data never produces a
population tie. With no tie, the `city_name` tie-break is never tested, so a student query that
omits it grades correct on every seed. The grader is right; the data is too easy. The fix is a
non-triviality assertion the generator must satisfy.

This module derives such assertions GENERICALLY from the gold SQL's AST (via sqlglot), so it
applies to any problem without hand-coding per-problem data:

  * tie-break:  for ORDER BY k1, k2, …, kn the result must contain a tie on the prefix
                k1..k_{n-1} so the last key actually decides an ordering.

(LIMIT-truncates / HAVING-excludes / JOIN-drops are already enforced by the bank's per-problem
hand checks; tie-break was the gap. The helpers here are written so the other classes can be
added the same way if a future problem needs them.)
"""
from __future__ import annotations

import sqlglot
from sqlglot import exp


def order_key_indices(gold_sql: str) -> list[tuple[int, bool]] | None:
    """Map each top-level ORDER BY key to the gold result COLUMN INDEX it sorts on, in order.
    Returns [(col_index, descending?), …], or None if the query won't parse or a key cannot be
    resolved to an output column (in which case we make no assertion rather than a wrong one).

    Resolution handles the common teaching-SQL shapes: a key that is an output alias, a key that
    is a selected column name, or a 1-based ordinal (`ORDER BY 2`).
    """
    try:
        ast = sqlglot.parse_one(gold_sql, read="sqlite")
    except Exception:
        return None
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    order = ast.args.get("order")
    if select is None or order is None:
        return None

    # output column names/aliases, in SELECT order
    out_names: list[str | None] = []
    for proj in select.expressions:
        if isinstance(proj, exp.Alias):
            out_names.append(proj.alias)
        elif isinstance(proj, exp.Column):
            out_names.append(proj.name)
        else:
            out_names.append(None)
    lower = [n.lower() if n else None for n in out_names]

    keys: list[tuple[int, bool]] = []
    for o in order.expressions:                 # exp.Ordered
        desc = bool(o.args.get("desc"))
        e = o.this
        idx = None
        if isinstance(e, exp.Literal) and e.is_int:          # ORDER BY 2
            n = int(e.name) - 1
            if 0 <= n < len(out_names):
                idx = n
        else:
            name = e.name if isinstance(e, (exp.Column, exp.Identifier)) else None
            if name and name.lower() in lower:
                idx = lower.index(name.lower())
        if idx is None:
            return None                          # unresolved key — stay safe, assert nothing
        keys.append((idx, desc))
    return keys


def tiebreak_columns(gold_sql: str) -> list[int] | None:
    """The prefix column indices that must tie for the final ORDER BY key to be exercised.
    Returns None if there is no secondary sort key to worry about."""
    keys = order_key_indices(gold_sql)
    if not keys or len(keys) < 2:
        return None
    return [idx for idx, _ in keys[:-1]]


def assert_tiebreak_exercised(rows: list[tuple], prefix_cols: list[int]) -> None:
    """Raise AssertionError if no two result rows tie on every `prefix_cols` column. When they
    do tie, the next ORDER BY key is what breaks the tie — so it is genuinely under test."""
    seen: set[tuple] = set()
    for r in rows:
        key = tuple(r[c] for c in prefix_cols)
        if key in seen:
            return
        seen.add(key)
    cols = ", ".join(f"col{c}" for c in prefix_cols)
    raise AssertionError(
        f"no two gold rows tie on ({cols}); the ORDER BY tie-break is never exercised, so a "
        f"student query that omits the secondary sort key would wrongly pass. Generate at least "
        f"two rows that share the same ({cols}) within the result.")


def make_tiebreak_check(gold_sql: str):
    """A `check(conn, rows)`-shaped callable enforcing tie-break coverage, or None if the gold
    query has no secondary sort key. Composable with a problem's existing hand check."""
    prefix = tiebreak_columns(gold_sql)
    if prefix is None:
        return None

    def _check(conn, rows):
        assert_tiebreak_exercised(rows, prefix)
    return _check


# --------------------------------------------------------------------------- #
# aggregate-value ties: CONSTRUCT the witness instead of prompting for it
# --------------------------------------------------------------------------- #
# Models reliably build raw-row ties but fail at engineering two groups whose AGGREGATE
# comes out equal (p03: 18/18 authoring failures). But the witness is mechanically
# derivable: clone one group's source rows under a fresh group-key value and every
# aggregate over the group (SUM/COUNT/AVG/MIN/MAX) copies exactly — an equal-aggregate
# tie BY CONSTRUCTION. The plan (table, group column, aggregate, direction) is read off
# the gold SQL's AST, so this generalizes to any single-table aggregate-ordered problem;
# the snippet is emitted as self-contained source appended to a frozen generator.

def aggregate_tie_plan(gold_sql: str) -> dict | None:
    """If the gold's PRIMARY ORDER BY key is an aggregate (directly or via alias) over a
    single-table GROUP BY on one column, return what an augmentation needs:
    {table, group_col, agg_sql, desc}. Else None (no safe construction)."""
    try:
        ast = sqlglot.parse_one(gold_sql, read="sqlite")
    except Exception:
        return None
    select = ast if isinstance(ast, exp.Select) else ast.find(exp.Select)
    order = ast.args.get("order")
    group = select.args.get("group") if select else None
    if select is None or order is None or group is None:
        return None
    if select.args.get("joins") or select.find(exp.Join):
        return None                                   # multi-table: cloning is not safe generically
    frm = select.args.get("from") or select.args.get("from_")   # sqlglot <30 / >=30
    if frm is None or not isinstance(frm.this, exp.Table):
        return None
    table = frm.this.name
    if len(group.expressions) != 1 or not isinstance(group.expressions[0], exp.Column):
        return None
    group_col = group.expressions[0].name

    first = order.expressions[0]                      # exp.Ordered
    desc = bool(first.args.get("desc"))
    key = first.this
    agg = None
    if isinstance(key, exp.AggFunc):
        agg = key
    else:                                             # alias or ordinal -> resolve in projections
        name = key.name if isinstance(key, (exp.Column, exp.Identifier)) else None
        idx = int(key.name) - 1 if isinstance(key, exp.Literal) and key.is_int else None
        for i, proj in enumerate(select.expressions):
            inner = proj.this if isinstance(proj, exp.Alias) else proj
            if isinstance(inner, exp.AggFunc) and (
                    i == idx or (name and isinstance(proj, exp.Alias) and proj.alias.lower() == name.lower())):
                agg = inner
                break
    if agg is None:
        return None
    return {"table": table, "group_col": group_col,
            "agg_sql": agg.sql(dialect="sqlite"), "desc": desc}


def aggregate_tie_src(plan: dict) -> str:
    """Self-contained generator-augmentation source: wraps an existing `populate` so that,
    after the base data is inserted, the rank-1 group (by the gold's own aggregate and
    direction) is cloned under a fresh group-key value. The clone ties the original on the
    aggregate, sits beside it inside any LIMIT window, and passes the same WHERE/HAVING
    (its rows are identical). Deterministic; integer PKs are reassigned via NULL."""
    order = "DESC" if plan["desc"] else "ASC"
    return f'''

# --- general edge-coverage augmentation (derived from the gold query's AST) -----------
# The gold orders primarily by {plan["agg_sql"]} over GROUP BY {plan["group_col"]}. Models
# could not author an equal-aggregate tie; this constructs one: clone the rank-1 group's
# rows under a fresh {plan["group_col"]} value, so two groups tie on the aggregate exactly.
_populate_base = populate

def _force_aggregate_tie(conn):
    top = conn.execute(
        "SELECT {plan["group_col"]} FROM {plan["table"]} WHERE {plan["group_col"]} IS NOT NULL "
        "GROUP BY {plan["group_col"]} ORDER BY {plan["agg_sql"]} {order}, {plan["group_col"]} LIMIT 1"
    ).fetchone()
    if top is None:
        return
    src = top[0]
    clone = (str(src) + "2") if isinstance(src, str) else None
    if clone is None:                                  # integer group key: use max+1
        clone = conn.execute("SELECT MAX({plan["group_col"]}) + 1 FROM {plan["table"]}").fetchone()[0]
    while conn.execute("SELECT 1 FROM {plan["table"]} WHERE {plan["group_col"]} = ? LIMIT 1",
                       (clone,)).fetchone():
        clone = (str(clone) + "2") if isinstance(clone, str) else clone + 1
    info = conn.execute("PRAGMA table_info({plan["table"]})").fetchall()
    cols = [c[1] for c in info]
    int_pk = {{c[1] for c in info if c[5] and (c[2] or "").upper() == "INTEGER"}}
    rows = conn.execute(
        "SELECT " + ", ".join(cols) + " FROM {plan["table"]} WHERE {plan["group_col"]} = ?",
        (src,)).fetchall()
    out = [tuple(None if c in int_pk else (clone if c == "{plan["group_col"]}" else v)
                 for c, v in zip(cols, r)) for r in rows]
    conn.executemany(
        "INSERT INTO {plan["table"]}(" + ", ".join(cols) + ") VALUES (" + ", ".join("?" * len(cols)) + ")",
        out)
    conn.commit()

def populate(conn, seed):
    _populate_base(conn, seed)
    _force_aggregate_tie(conn)
'''


def compose_checks(*checks):
    """Run several `check(conn, rows)` callables in sequence (skipping Nones)."""
    cs = [c for c in checks if c is not None]

    def _check(conn, rows):
        for c in cs:
            c(conn, rows)
    return _check
