// Error-class-adaptive hint ladder (redesign 2026-06-28). The ladder is built from four
// PRIMITIVES and which one sits at L1/L2/L3 depends on the grade's error-class FAMILY (sent by
// the backend as `rung_plan`). The two deterministic primitives (`diff`, `db_error`) carry no
// model text — they render the grader's evidence client-side and cannot leak. The model
// primitives (`socratic`, `conceptual`, `directive`) are plain prose with no runnable SQL.
//
//   membership / ordering : diff  -> socratic   -> conceptual
//   structure (default)   : socratic -> conceptual -> directive   (never shows the raw diff)
//   error                 : db_error -> conceptual -> directive
//
// So the diff rung can now appear at ANY level — there is no "L3 == diff" assumption anymore.

import { Prose, DataTable } from "./bits.jsx";

// Per-primitive rung labels. `diff`/`db_error` get a family-aware label below.
const PRIMITIVE_LABELS = {
  socratic: "A question to consider",
  conceptual: "Conceptual nudge",
  directive: "What to change",
  db_error: "The database error",
  diff: "The evidence",
};

function rungLabel(primitive, family) {
  if (primitive === "diff") {
    if (family === "ordering") return "What's out of order";
    if (family === "schema") return "How your database differs";
    return "The rows that don't match";
  }
  return PRIMITIVE_LABELS[primitive] || "Hint";
}

const DETERMINISTIC = new Set(["diff", "db_error"]);

// Column headers for the evidence tables. Prefer the names the student's own query returned
// (`resultCols`); fall back to generic positional headers when they're unavailable.
function evidenceCols(resultCols, width) {
  if (Array.isArray(resultCols) && resultCols.length === width) return resultCols;
  return Array.from({ length: width }, (_, i) => `col ${i + 1}`);
}

// Column-shape mismatches surfaced as a SEPARATE annotation (redesign §4.2), never as row
// redness. The backend computes these in diff.header_notes.
function HeaderNotes({ notes }) {
  if (!notes || notes.length === 0) return null;
  return (
    <ul className="diff-header-notes" style={{ margin: "0 0 8px", paddingLeft: 18 }}>
      {notes.map((n, i) => (
        <li key={i} className="run-hint" style={{ marginBottom: 2 }}>{n}</li>
      ))}
    </ul>
  );
}

// L3 for state-graded questions (CREATE/INSERT/UPDATE/DELETE/DROP), built from diff.* fields
// describing how the final database state diverges from the gold state.
function StateEvidence({ diff }) {
  if (diff.sql_error) {
    return <div className="rung-text">Your statement didn't run, so there's nothing to compare. Fix the database error first:<pre>{diff.sql_error}</pre></div>;
  }

  const rows = [];

  if (diff.tables_missing?.length > 0) {
    rows.push(
      <div className="diff-line" key="missing-tables">
        Tables that should exist but don't: {diff.tables_missing.map((t) => <code className="inline-code" key={t}>{t}</code>)}
      </div>
    );
  }
  if (diff.tables_extra?.length > 0) {
    rows.push(
      <div className="diff-line" key="extra-tables">
        Tables that should be gone: {diff.tables_extra.map((t) => <code className="inline-code" key={t}>{t}</code>)}
      </div>
    );
  }

  if (diff.column_diffs) {
    for (const [table, cd] of Object.entries(diff.column_diffs)) {
      const parts = [];
      if (cd.missing?.length > 0) parts.push(`missing column${cd.missing.length > 1 ? "s" : ""} ${cd.missing.join(", ")}`);
      if (cd.extra?.length > 0) parts.push(`unexpected column${cd.extra.length > 1 ? "s" : ""} ${cd.extra.join(", ")}`);
      if (parts.length > 0) {
        rows.push(
          <div className="diff-line" key={"col-" + table}>
            <code className="inline-code">{table}</code>: {parts.join(" · ")}
          </div>
        );
      }
    }
  }

  if (diff.row_diffs) {
    for (const [table, rd] of Object.entries(diff.row_diffs)) {
      rows.push(
        <div key={"row-" + table}>
          <div className="diff-line">
            <code className="inline-code">{table}</code>: you have <b>{rd.n_student}</b> rows, expected <b>{rd.n_gold}</b>
            {rd.n_extra > 0 && <> — {rd.n_extra} of yours shouldn't be there</>}
            {rd.n_missing > 0 && <> — {rd.n_missing} expected row{rd.n_missing > 1 ? "s" : ""} missing</>}
          </div>
          {rd.extra_sample?.length > 0 && (
            <div style={{ marginTop: 6, marginBottom: 6 }}>
              <div className="run-hint" style={{ marginBottom: 4 }}>yours, not expected</div>
              <DataTable cols={rd.extra_sample[0].map((_, i) => `c${i + 1}`)} rows={rd.extra_sample} />
            </div>
          )}
        </div>
      );
    }
  }

  if (diff.no_effect) {
    rows.push(<div className="diff-line" key="no-effect">Your statement left the database unchanged.</div>);
  }

  if (rows.length === 0) {
    return <pre className="mono">{diff.text}</pre>;
  }

  return (
    <div>
      <div style={{ marginBottom: 8 }}>Compared on one hidden test database, here's how your database's state diverges:</div>
      <div className="diff-rows" style={{ flexDirection: "column", gap: 8 }}>
        {rows}
      </div>
    </div>
  );
}

// One labelled evidence table (the missing rows, or the extra rows), rendered in the same
// MySQL-style grid the student already sees for their own result — not a (a, b, c) tuple list.
function EvidenceTable({ title, kind, rows, total, resultCols }) {
  if (!rows.length) return null;
  const width = Math.max(...rows.map((r) => r.length));
  const cols = evidenceCols(resultCols, width);
  return (
    <div className={"diff-col " + kind}>
      <h4>{title} ({total})</h4>
      <DataTable cols={cols} rows={rows} />
      {total > rows.length && (
        <div className="run-hint" style={{ marginTop: 4 }}>+{total - rows.length} more not shown</div>
      )}
    </div>
  );
}

// The deterministic `diff` / `db_error` rung, built only from the diff we already hold on the
// client (no gold query exists here).
function Evidence({ diff, resultCols }) {
  if (!diff) return <span>Your result matches now — re-run to confirm.</span>;
  if (diff.kind === "state") return <StateEvidence diff={diff} />;
  if (diff.sql_error) {
    return <div className="rung-text">Your query didn't run, so there's nothing to compare. Fix the database error first:<pre>{diff.sql_error}</pre></div>;
  }
  // header/column-shape mismatches: a separate annotation, not row redness (§4.2)
  const headerNotes = <HeaderNotes notes={diff.header_notes} />;
  if (diff.ordering_only || diff.family === "ordering") {
    return (
      <div>
        {headerNotes}
        <span>Your rows are exactly right — only their order is off. Revisit the sort keys and their directions in your <code className="inline-code">ORDER BY</code>; the problem needs a specific order (including how ties are broken).</span>
      </div>
    );
  }
  return (
    <div>
      {headerNotes}
      <div style={{ marginBottom: 8 }}>Compared on one hidden test database, here's exactly where your rows diverge:</div>
      <div className="diff-rows">
        <EvidenceTable title="You're missing" kind="miss" rows={diff.missing} total={diff.missing_total} resultCols={resultCols} />
        <EvidenceTable title="You wrongly include" kind="extra" rows={diff.extra} total={diff.extra_total} resultCols={resultCols} />
      </div>
      <div style={{ marginTop: 10, color: "var(--muted)", fontSize: 13.5 }}>
        What change to your query would drop the wrong rows and recover the missing ones?
      </div>
    </div>
  );
}

export default function HintLadder({ hints, diff, resultCols, rungPlan, loading, onRequest, locked = false, lockedReason }) {
  // The ladder shape comes from the backend's family-adaptive rung_plan; fall back to the
  // legacy fixed shape if it isn't present (e.g. an older grade response in state).
  const plan = Array.isArray(rungPlan) && rungPlan.length ? rungPlan : ["conceptual", "conceptual", "diff"];
  const family = diff?.family;
  const shown = hints.length;
  const next = shown + 1;
  const allShown = shown >= plan.length;

  return (
    <div className="hints">
      <div className="section-label"><span className="eyebrow">Hints</span></div>
      <div className="ladder">
        {hints.map((h) => {
          const prim = h.primitive || plan[h.level - 1];
          return (
            <div className="rung open" key={h.level}>
              <div className="rung-n">{h.level}</div>
              <div className="rung-body">
                <div className="rung-label">{rungLabel(prim, family)}</div>
                <div className="rung-text">
                  {DETERMINISTIC.has(prim)
                    ? <Evidence diff={diff} resultCols={resultCols} />
                    : <Prose text={h.text} />}
                </div>
              </div>
            </div>
          );
        })}
        {!allShown && (
          <div className="rung locked">
            <div className="rung-n">{next}</div>
            <div className="rung-body hint-cta">
              {locked ? (
                <span className="run-hint">{lockedReason || "Hints are closed."}</span>
              ) : loading ? (
                <span className="thinking"><span className="spin" /> thinking…</span>
              ) : (
                <>
                  <button className="btn ghost sm" onClick={() => onRequest(next)}>
                    {shown === 0 ? "Ask for a hint" : `Next hint`}
                  </button>
                  <span className="run-hint">{rungLabel(plan[next - 1], family)}</span>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
