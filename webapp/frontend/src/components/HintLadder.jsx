// Progressive disclosure, one axis of "how much help":
//   L1 (model)  conceptual nudge — what KIND of thing is off
//   L2 (model)  name the clause/operation to revisit
//   L3 (deterministic, NO model) the concrete diverging rows from the grader's diff
//
// L3 is execution-grounded evidence, never a query skeleton: a SQL skeleton derived from the
// student's own query reveals the answer's shape by construction, so we don't draw one. The
// student still has to infer which query change produces these rows. Because L1/L2 carry no
// SQL and L3 is deterministic, none of the three rungs can hand over a runnable answer.

import { Prose, DataTable } from "./bits.jsx";

const LABELS = {
  1: "Conceptual nudge",
  2: "The clause to revisit",
  3: "The rows that don't match",
};

// Column headers for the L3 evidence tables. Prefer the names the student's own query
// returned (`resultCols`); fall back to generic positional headers when they're unavailable
// or don't line up with the row width.
function evidenceCols(resultCols, width) {
  if (Array.isArray(resultCols) && resultCols.length === width) return resultCols;
  return Array.from({ length: width }, (_, i) => `col ${i + 1}`);
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

// L3 content, built only from the diff we already have on the client (no gold query exists here).
function Evidence({ diff, resultCols }) {
  if (!diff) return <span>Your result matches now — re-run to confirm.</span>;
  if (diff.kind === "state") return <StateEvidence diff={diff} />;
  if (diff.sql_error) {
    return <div className="rung-text">Your query didn't run, so there's nothing to compare. Fix the database error first:<pre>{diff.sql_error}</pre></div>;
  }
  if (diff.ordering_only) {
    return <span>Your rows are exactly right — only their order is off. Revisit the sort keys and their directions in your <code className="inline-code">ORDER BY</code>; the problem needs a specific order (including how ties are broken).</span>;
  }
  return (
    <div>
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

export default function HintLadder({ hints, diff, resultCols, loading, onRequest, maxLevel = 3, locked = false, lockedReason }) {
  const shown = hints.length;            // L1/L2 model hints already revealed
  const l3Open = shown >= 3;             // L3 stored as a sentinel hint {level:3}
  const next = shown + 1;

  return (
    <div className="hints">
      <div className="section-label"><span className="eyebrow">Hints</span></div>
      <div className="ladder">
        {hints.map((h) => (
          <div className="rung open" key={h.level}>
            <div className="rung-n">{h.level}</div>
            <div className="rung-body">
              <div className="rung-label">{LABELS[h.level]}</div>
              <div className="rung-text">
                {h.level === 3 ? <Evidence diff={diff} resultCols={resultCols} /> : <Prose text={h.text} />}
              </div>
            </div>
          </div>
        ))}
        {!l3Open && next <= maxLevel && (
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
                  <span className="run-hint">{LABELS[next]}</span>
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
