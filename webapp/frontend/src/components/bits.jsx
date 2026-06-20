// Small shared display bits: schema viewer, data table, difficulty tag, diff panel.

const KW = /\b(CREATE TABLE|PRIMARY KEY|INTEGER|TEXT|REAL|NOT NULL|REFERENCES|UNIQUE)\b/g;

// Render `backtick` spans in a prompt as inline code (the bank prompts use markdown ticks),
// so a student never sees a stray backtick. Everything else stays plain prose.
export function Prose({ text }) {
  const parts = String(text).split(/(`[^`]+`)/g);
  return (
    <>
      {parts.map((p, i) =>
        p.startsWith("`") && p.endsWith("`") ? (
          <code key={i} className="inline-code">{p.slice(1, -1)}</code>
        ) : (
          <span key={i}>{p}</span>
        )
      )}
    </>
  );
}

export function Schema({ ddl }) {
  // light keyword emphasis — readable, not a syntax-highlight circus
  const html = ddl
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(KW, (m) => `<span class="kw">${m}</span>`);
  return <pre className="schema" dangerouslySetInnerHTML={{ __html: html }} />;
}

export function Diff({ diff }) {
  if (!diff) return null;
  if (diff.sql_error) {
    return (
      <div className="diff">
        <div className="diff-head">Your query didn't run</div>
        <div className="diff-body">
          <div className="diff-line mono" style={{ color: "var(--red)" }}>{diff.sql_error}</div>
        </div>
      </div>
    );
  }
  const cell = (r) => "(" + r.map((v) => (v === null ? "NULL" : typeof v === "string" ? `'${v}'` : v)).join(", ") + ")";
  return (
    <div className="diff">
      <div className="diff-head">How your result differs · test database #{diff.seed}</div>
      <div className="diff-body">
        {diff.ordering_only ? (
          <div className="diff-line">Right rows — wrong order. This problem requires a specific sort.</div>
        ) : (
          <>
            {diff.student_ncols !== diff.gold_ncols && (
              <div className="diff-line">
                Column count differs: expected <b>{diff.gold_ncols}</b>, you returned <b>{diff.student_ncols}</b>.
              </div>
            )}
            {diff.student_nrows !== diff.gold_nrows && (
              <div className="diff-line">
                Row count differs: expected <b>{diff.gold_nrows}</b>, you returned <b>{diff.student_nrows}</b>.
              </div>
            )}
            <div className="diff-rows">
              {diff.missing_total > 0 && (
                <div className="diff-col miss">
                  <h4>Missing — should be there ({diff.missing_total})</h4>
                  {diff.missing.map((r, i) => <span className="rowchip" key={i}>{cell(r)}</span>)}
                  {diff.missing_total > diff.missing.length && <span className="rowchip">+{diff.missing_total - diff.missing.length} more</span>}
                </div>
              )}
              {diff.extra_total > 0 && (
                <div className="diff-col extra">
                  <h4>Extra — shouldn't be there ({diff.extra_total})</h4>
                  {diff.extra.map((r, i) => <span className="rowchip" key={i}>{cell(r)}</span>)}
                  {diff.extra_total > diff.extra.length && <span className="rowchip">+{diff.extra_total - diff.extra.length} more</span>}
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function DataTable({ cols, rows }) {
  return (
    <div className="table-scroll">
      <table className="datatable">
        <thead>
          <tr>{cols.map((c) => <th key={c}>{c}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {r.map((v, j) => (
                <td key={j} className={v === null ? "null" : ""}>{v === null ? "NULL" : String(v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
