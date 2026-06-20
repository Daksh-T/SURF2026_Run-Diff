"""Problem bank for the SURF SQL tutor.

Each problem is a self-contained, SQLite-dialect exercise: schema DDL, a
natural-language prompt, a gold query, the SQL clauses the data must exercise,
data-generation requirements, and a deterministic `check(conn, rows)` over the
gold result. A check raises AssertionError(<message>) when the populated data
fails to exercise the target clauses (e.g. a LIMIT that never truncates, a
GROUP BY with one group); the message feeds the authoring model's repair loop.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Problem:
    id: str
    title: str
    difficulty: str            # easy | medium | hard
    schema: str                # SQLite DDL (one or more CREATE TABLE statements)
    prompt: str                # natural-language question shown to a student
    gold_sql: str              # human-vetted SQLite gold query
    target_clauses: list[str]  # SQL features the data must exercise
    requirements: list[str]    # edge-case instructions handed to the authoring model
    check: Callable[[sqlite3.Connection, list[tuple]], None]
    tables: list[str] = field(default_factory=list)  # table names, for convenience


# --------------------------------------------------------------------------- #
# small assertion helpers
# --------------------------------------------------------------------------- #
def _q(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def _scalar(conn, sql, params=()):
    r = conn.execute(sql, params).fetchone()
    return r[0] if r else None


def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)


def _nonempty(rows, lo=1):
    _assert(len(rows) >= lo,
            f"gold result has {len(rows)} rows, need >= {lo} (result is trivial/empty)")


def _ck(v):
    # NULL-safe sort key matching SQLite: NULL is the smallest value.
    return (0,) if v is None else (1, v)


def _sorted_by(rows, keyfns, msg):
    """Assert `rows` is ordered by the list of (column_index, descending?) key specs.
    NULL-safe (NULLs sort smallest, as SQLite does)."""
    for i in range(len(rows) - 1):
        a, b = rows[i], rows[i + 1]
        for idx, desc in keyfns:
            ka, kb = _ck(a[idx]), _ck(b[idx])
            if ka == kb:
                continue
            ok = ka >= kb if desc else ka <= kb
            _assert(ok, f"{msg}: rows out of order at position {i} on column {idx}")
            break

# Problems are authored at runtime through the in-app authoring flow.
PROBLEMS: list[Problem] = []

BY_ID = {p.id: p for p in PROBLEMS}


def get(pid: str) -> Problem:
    return BY_ID[pid]


if __name__ == "__main__":
    print(f"{len(PROBLEMS)} built-in problems")
