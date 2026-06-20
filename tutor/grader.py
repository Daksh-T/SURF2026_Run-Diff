"""Phase 3 — execution-based grader + deterministic result-set diff.

The grader is the project's core deterministic primitive ("run SQL, compare result
sets"), used to decide whether a student query is correct and to produce the structured
diff the tutor reasons over.  No LLM is involved here.

Mechanism (the reason K seeds matter): a student query is correct only if it matches the
gold query's result on EVERY one of the K seeded databases.  Each seed is a different
dataset produced by `populator/generators/<id>.py::populate`, so a query that is right by
accident on one dataset (lucky ordering, lucky data) is caught on another.  If a problem's
generator has low data diversity (all seeds yield identical data), the K-seed test
degenerates to K=1 for that problem — which is exactly why generator diversity matters.

Order sensitivity: a result is compared as an ordered sequence iff the gold query has a
top-level ORDER BY (subquery ORDER BYs are ignored); otherwise it is compared as a
multiset.  An order-sensitive problem where the student returns the right rows in the
wrong order is reported specifically as an ordering error (a common, teachable mistake).

The gold SQL lives only inside this deterministic layer.  It is never exposed to the
tutor's model (see harness.py): the model receives only the diff produced here.
"""
from __future__ import annotations

import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot

# reuse the Phase-2 problem bank, frozen generators, and instantiation primitive
_POP = Path(__file__).resolve().parents[1] / "populator"
if str(_POP) not in sys.path:
    sys.path.insert(0, str(_POP))

from problems import bank            # noqa: E402
from populate import load_populate   # noqa: E402

GEN_DIR = _POP / "generators"
DEFAULT_SEEDS = list(range(1, 11))   # K=10 grading seeds (disjoint from the 1000+ stress range)


# --------------------------------------------------------------------------- #
# instantiation
# --------------------------------------------------------------------------- #
def load_generator(pid: str, gendir: Path = GEN_DIR):
    path = Path(gendir) / f"{pid}.py"
    if not path.exists():
        raise FileNotFoundError(f"no frozen generator for {pid} at {path}")
    return load_populate(path.read_text())


def build_db(problem, populate, seed) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(problem.schema)
    populate(conn, seed)
    conn.commit()
    return conn


def run_query(conn: sqlite3.Connection, sql: str):
    """Return (column_names, rows). Raises sqlite3.Error on a bad query."""
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    return cols, cur.fetchall()


def permute_db(conn: sqlite3.Connection) -> None:
    """Re-insert every table's rows in REVERSED storage order, so the incidental order SQLite
    returns for an under-specified query flips. A query whose ORDER BY imposes a TOTAL order is
    unaffected; one that leaves ties (e.g. a missing tie-break column) returns a different
    sequence — which is how we catch it without parsing the student's SQL.

    Integer-primary-key (rowid alias) columns are re-assigned (passed NULL) so the reversal
    actually changes rowid order even when the generator inserted explicit PK values."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    for t in tables:
        info = conn.execute(f"PRAGMA table_info({t})").fetchall()  # (cid,name,type,notnull,dflt,pk)
        cols = [c[1] for c in info]
        int_pk = {c[1] for c in info if c[5] and (c[2] or "").upper() == "INTEGER"}
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall()
        conn.execute(f"DELETE FROM {t}")
        ph = ", ".join("?" * len(cols))
        reinsert = [tuple(None if c in int_pk else v for c, v in zip(cols, r))
                    for r in reversed(rows)]
        conn.executemany(f"INSERT INTO {t}({', '.join(cols)}) VALUES ({ph})", reinsert)
    conn.commit()


def order_sensitive(gold_sql: str) -> bool:
    """True iff the gold query imposes a row order at the TOP level (a subquery's ORDER BY
    doesn't count). Parsed via sqlglot so the top-level ORDER BY is just an arg on the outer
    SELECT; falls back to a regex if the query won't parse."""
    try:
        return sqlglot.parse_one(gold_sql, read="sqlite").args.get("order") is not None
    except Exception:
        return bool(re.search(r"\border\s+by\b", gold_sql, re.I))


# --------------------------------------------------------------------------- #
# per-seed comparison + diff
# --------------------------------------------------------------------------- #
@dataclass
class Diff:
    """A structured, deterministic description of how the student result differs from
    gold on one seed.  Carries no gold SQL — only result-set facts."""
    seed: int
    ordered: bool
    gold_ncols: int
    student_ncols: int
    gold_nrows: int
    student_nrows: int
    missing: list = field(default_factory=list)   # rows in gold, not in student (multiset)
    extra: list = field(default_factory=list)     # rows in student, not in gold
    ordering_only: bool = False                   # right rows, wrong order
    sql_error: str | None = None                  # student query failed to execute

    def to_text(self, cap: int = 5) -> str:
        if self.sql_error:
            return f"Your query did not run: {self.sql_error}"
        L = [f"Comparing on test database #{self.seed}:"]
        if self.student_ncols != self.gold_ncols:
            L.append(f"- column count differs: expected {self.gold_ncols}, "
                     f"your query returned {self.student_ncols}")
        if self.ordering_only:
            L.append("- the rows are correct but in the wrong order "
                     "(this problem requires a specific ordering)")
            return "\n".join(L)
        if self.student_nrows != self.gold_nrows:
            L.append(f"- row count differs: expected {self.gold_nrows}, "
                     f"your query returned {self.student_nrows}")
        if self.missing:
            shown = ", ".join(repr(r) for r in self.missing[:cap])
            more = f" (+{len(self.missing) - cap} more)" if len(self.missing) > cap else ""
            L.append(f"- {len(self.missing)} row(s) the correct answer has but yours "
                     f"is missing: {shown}{more}")
        if self.extra:
            shown = ", ".join(repr(r) for r in self.extra[:cap])
            more = f" (+{len(self.extra) - cap} more)" if len(self.extra) > cap else ""
            L.append(f"- {len(self.extra)} row(s) your query returned that should not "
                     f"be there: {shown}{more}")
        if len(L) == 1:
            L.append("- results differ.")
        return "\n".join(L)


@dataclass
class SeedResult:
    seed: int
    ok: bool
    diff: Diff | None = None


def _compare(seed, gold_cols, gold_rows, stu_cols, stu_rows, ordered) -> SeedResult:
    if stu_cols is None:  # sentinel never used; kept explicit
        pass
    gold_nc, stu_nc = len(gold_cols), len(stu_cols)
    # column-count mismatch -> rows cannot align; report directly
    if gold_nc != stu_nc:
        d = Diff(seed, ordered, gold_nc, stu_nc, len(gold_rows), len(stu_rows),
                 missing=list(gold_rows), extra=list(stu_rows))
        return SeedResult(seed, False, d)

    same_multiset = Counter(gold_rows) == Counter(stu_rows)
    if ordered:
        if gold_rows == stu_rows:
            return SeedResult(seed, True)
        if same_multiset:  # right rows, wrong order
            d = Diff(seed, ordered, gold_nc, stu_nc, len(gold_rows), len(stu_rows),
                     ordering_only=True)
            return SeedResult(seed, False, d)
    else:
        if same_multiset:
            return SeedResult(seed, True)

    gc, sc = Counter(gold_rows), Counter(stu_rows)
    missing = list((gc - sc).elements())
    extra = list((sc - gc).elements())
    d = Diff(seed, ordered, gold_nc, stu_nc, len(gold_rows), len(stu_rows),
             missing=missing, extra=extra)
    return SeedResult(seed, False, d)


@dataclass
class GradeResult:
    problem_id: str
    correct: bool                       # matched gold on all graded seeds
    n_seeds: int
    per_seed: list[SeedResult]
    first_fail: SeedResult | None       # smallest failing seed (deterministic)

    @property
    def diff_text(self) -> str:
        return "" if self.correct or not self.first_fail or not self.first_fail.diff \
            else self.first_fail.diff.to_text()

    @property
    def student_error(self) -> str | None:
        if self.first_fail and self.first_fail.diff:
            return self.first_fail.diff.sql_error
        return None


def grade_problem(problem, populate, student_sql: str,
                  seeds: list[int] | None = None) -> GradeResult:
    """Grade a student query against an explicit problem + generator (works for problems not in
    the bank — e.g. ones an instructor just authored). Correct iff the student result matches
    the gold result on every seed. Deterministic."""
    seeds = seeds or DEFAULT_SEEDS
    ordered = order_sensitive(problem.gold_sql)
    per: list[SeedResult] = []
    first_fail: SeedResult | None = None
    for s in seeds:
        conn = build_db(problem, populate, s)
        gold_cols, gold_rows = run_query(conn, problem.gold_sql)
        try:
            stu_cols, stu_rows = run_query(conn, student_sql)
        except sqlite3.Error as e:
            sr = SeedResult(s, False, Diff(s, ordered, len(gold_cols), 0, len(gold_rows), 0,
                                           sql_error=str(e)))
        else:
            sr = _compare(s, gold_cols, gold_rows, stu_cols, stu_rows, ordered)
            # ordering-ambiguity guard (the permutation test): a student query that matched
            # only because of incidental storage order must ALSO match after the rows are
            # permuted. Enforced only when the gold itself is stable under permutation (a
            # total order) — otherwise sequence comparison would be arbitrary either way.
            if sr.ok and ordered:
                permute_db(conn)
                _, gold_perm = run_query(conn, problem.gold_sql)
                if gold_perm == gold_rows:          # gold imposes a total order on this seed
                    try:
                        _, stu_perm = run_query(conn, student_sql)
                    except sqlite3.Error:
                        stu_perm = stu_rows
                    if stu_perm != gold_rows:
                        sr = SeedResult(s, False,
                            Diff(s, ordered, len(gold_cols), len(stu_cols),
                                 len(gold_rows), len(stu_perm), ordering_only=True))
        per.append(sr)
        if not sr.ok and first_fail is None:
            first_fail = sr
        conn.close()
    return GradeResult(problem.id, all(r.ok for r in per), len(seeds), per, first_fail)


def grade(pid: str, student_sql: str, seeds: list[int] | None = None,
          gendir: Path = GEN_DIR) -> GradeResult:
    """Grade a student query for bank problem `pid` over K seeded databases (thin wrapper
    around grade_problem that loads the problem + its frozen generator)."""
    return grade_problem(bank.get(pid), load_generator(pid, gendir), student_sql, seeds)
