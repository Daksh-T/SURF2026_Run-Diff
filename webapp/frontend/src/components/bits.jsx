// Small shared display bits: prompt prose, schema viewer, data table.

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
