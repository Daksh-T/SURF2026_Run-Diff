"""Publish = bake. Turn an instructor-private source set (gold SQL present) into a
student-safe bundle (gold RESULTS only). This is the one and only place gold_sql is read at
the application boundary; `tutor_core.bake_gold` does the actual run-and-freeze.
"""
from __future__ import annotations

from datetime import datetime, timezone

import store
import tutor_core as tc


def publish(set_id: str, seeds: list[int] | None = None) -> dict:
    src = store.get_set(set_id)
    if not src["problems"]:
        raise ValueError("cannot publish an empty set")
    seeds = seeds or tc.DEFAULT_SEEDS

    import state_core as sc  # local import: keeps the select-only path free of state deps

    problems = []
    for p in src["problems"]:
        kind = p.get("kind", "select")
        if kind == "state":
            baked = sc.bake_gold_state(p["schema"], p["gold_sql"], p["generator_src"], seeds)
        else:
            baked = tc.bake_gold(p["schema"], p["gold_sql"], p["generator_src"], seeds)
        # build the student-safe problem record — note the absence of gold_sql by construction
        problems.append({
            "id": p["id"],
            "title": p["title"],
            "kind": kind,
            "difficulty": p.get("difficulty", "medium"),
            "prompt": p["prompt"],
            "schema": p["schema"],
            "generator_src": p["generator_src"],
            "target_clauses": p.get("target_clauses", []),
            "baked": baked,
        })

    bundle = {
        "id": src["id"],
        "title": src["title"],
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seeds": seeds,
        "problems": problems,
    }
    store.assert_student_safe(bundle)   # raises if any gold_sql slipped in
    store.save_bundle(bundle)

    src["published_at"] = bundle["published_at"]
    store.save_set(src)
    return {"id": bundle["id"], "title": bundle["title"],
            "published_at": bundle["published_at"], "n_problems": len(problems)}
