import { useEffect, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { DataTable, Schema } from "../components/bits.jsx";

// The instructor writes ONLY a question + gold SQL. One model infers the schema, derives the
// target clauses, surfaces plain-English edge-case nudges, and authors a robust seeded data
// generator. The instructor previews it, adds it to a set, and publishes — at which point the
// gold query is baked into per-seed results and never shipped to students.

const EMPTY_ITEM = () => ({ title: "", prompt: "", gold_sql: "", difficulty: "medium" });
const EMPTY_SECTION = () => ({ tableHint: "", items: [EMPTY_ITEM()] });

export default function Instructor() {
  const [sets, setSets] = useState([]);
  const [setId, setSetId] = useState(""); // left-rail selection
  const [set, setSet] = useState(null);

  // which view the main pane shows: "author" (new/unfiled or "author into this set") or "set"
  // (set management). The rail selection sets the default; "author into this set" can switch
  // to "author" while keeping setId as the authoring target.
  const [view, setView] = useState("author");
  // the set authored problems get added to — independent of the rail selection, so "author
  // into this set" keeps targeting a set even while the view is "author"
  const [authorTargetId, setAuthorTargetId] = useState("");

  // mode switch: a single problem, or a whole assignment sharing one table
  const [mode, setMode] = useState("single"); // "single" | "assignment"

  // ---- single-problem form ----
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [gold, setGold] = useState("");
  const [difficulty, setDifficulty] = useState("medium");
  const [predict, setPredict] = useState(false); // run the simulated weak student after authoring

  const [job, setJob] = useState(null); // {state, result}
  const [authored, setAuthored] = useState(null); // result.problem etc.

  // ---- assignment (batch) form: each section has its own table hint + questions ----
  const [assignmentTitle, setAssignmentTitle] = useState(""); // names the set batch items land in
  const [sections, setSections] = useState([EMPTY_SECTION()]);
  const [batchJob, setBatchJob] = useState(null); // {state, progress, result}
  const [batchResults, setBatchResults] = useState(null); // {sections: [{ddl, items: [...]}]}

  const [banner, setBanner] = useState(null);
  const poll = useRef(null);
  const importRef = useRef(null);

  const refreshSets = () => api.instructorSets().then(setSets);
  useEffect(() => { refreshSets(); }, []);
  useEffect(() => {
    if (!setId) { setSet(null); setView("author"); return; }
    api.getSet(setId).then(setSet);
    setView("set");
  }, [setId]);

  // rail selection drives the authoring target too, by default
  useEffect(() => { setAuthorTargetId(setId); }, [setId]);

  useEffect(() => () => clearInterval(poll.current), []);

  const targetSet = sets.find((s) => s.id === authorTargetId);

  function selectRail(id) {
    setSetId(id);
  }

  function authorIntoThisSet() {
    setAuthorTargetId(setId);
    setView("author");
  }

  async function startAuthor(confirmedNudges, ddl) {
    if (!prompt.trim() || !gold.trim() || !title.trim()) {
      setBanner({ kind: "err", text: "Give the problem a title, a question, and the gold SQL." });
      return;
    }
    setBanner(null);
    setAuthored(null);
    setJob({ state: "running" });
    const payload = { prompt, gold_sql: gold, title, difficulty };
    if (confirmedNudges?.length) payload.confirmed_nudges = confirmedNudges;
    if (ddl) payload.ddl = ddl;
    if (predict) payload.predict = true;
    const { job_id } = await api.author(payload);
    poll.current = setInterval(async () => {
      const j = await api.job(job_id);
      if (j.state === "running") return;
      clearInterval(poll.current);
      setJob(j);
      if (j.result?.status === "ok") setAuthored(j.result);
      else setBanner({ kind: "err", text: `Authoring failed at ${j.result?.stage}: ${j.result?.reason}` });
    }, 1200);
  }

  async function addToSet(problem, fallbackTitle) {
    let sid = authorTargetId;
    if (!sid) {
      const created = await api.newSet(fallbackTitle || title || problem.title || "Untitled set");
      sid = created.id;
      await refreshSets();
      setAuthorTargetId(sid);
    }
    await api.addProblem(sid, problem);
    if (sid === setId) await api.getSet(sid).then(setSet);
    setBanner({ kind: "ok", text: `Added "${problem.title}" to the set.` });
  }

  async function addToSetSingle() {
    await addToSet(authored.problem);
    setAuthored(null);
    setTitle(""); setPrompt(""); setGold(""); setJob(null);
  }

  // ---- assignment (batch) form helpers — sections each have their own hint + questions ----
  function updateSectionHint(s, value) {
    setSections((arr) => arr.map((sec, j) => (j === s ? { ...sec, tableHint: value } : sec)));
  }
  function updateItem(s, i, field, value) {
    setSections((arr) => arr.map((sec, j) => (j === s
      ? { ...sec, items: sec.items.map((it, k) => (k === i ? { ...it, [field]: value } : it)) }
      : sec)));
  }
  function addItem(s) {
    setSections((arr) => arr.map((sec, j) => (j === s ? { ...sec, items: [...sec.items, EMPTY_ITEM()] } : sec)));
  }
  function removeItem(s, i) {
    setSections((arr) => arr.map((sec, j) => (j === s ? { ...sec, items: sec.items.filter((_, k) => k !== i) } : sec)));
  }
  function addSection() {
    setSections((arr) => [...arr, EMPTY_SECTION()]);
  }
  function removeSection(s) {
    setSections((arr) => arr.filter((_, j) => j !== s));
  }

  async function startBatch() {
    for (let s = 0; s < sections.length; s++) {
      const sec = sections[s];
      if (!sec.tableHint.trim()) {
        setBanner({ kind: "err", text: `Section ${s + 1}: describe what the table(s) hold.` });
        return;
      }
      const bad = sec.items.findIndex((it) => !it.title.trim() || !it.prompt.trim() || !it.gold_sql.trim());
      if (bad !== -1) {
        setBanner({ kind: "err", text: `Section ${s + 1}, question ${bad + 1} needs a title, a question, and gold SQL.` });
        return;
      }
    }
    setBanner(null);
    setBatchResults(null);
    const total = sections.reduce((n, sec) => n + sec.items.length, 0);
    setBatchJob({ state: "running", progress: { done: 0, total, current: sections[0]?.items[0]?.title } });
    const payload = sections.map((sec) => ({ table_hint: sec.tableHint, items: sec.items }));
    const { job_id } = await api.authorBatch(payload);
    poll.current = setInterval(async () => {
      const j = await api.job(job_id);
      if (j.state === "running") {
        setBatchJob(j);
        return;
      }
      clearInterval(poll.current);
      setBatchJob(j);
      if (j.result?.status === "ok") setBatchResults(j.result);
      else setBanner({ kind: "err", text: `Batch authoring failed: ${j.result?.reason || "unknown error"}` });
    }, 1200);
  }

  // re-author a single item from a batch section (e.g. after confirming a nudge), keeping
  // that section's shared ddl — or an overridden one if `ddlOverride` is given
  async function reauthorBatchItem(s, i, confirmedNudges, ddlOverride) {
    const it = sections[s].items[i];
    const ddl = ddlOverride ?? batchResults.sections[s].ddl;
    setBanner(null);
    setBatchResults((br) => ({
      ...br,
      sections: br.sections.map((sec, j) => (j === s
        ? { ...sec, items: sec.items.map((r, k) => (k === i ? { status: "running" } : r)) }
        : sec)),
    }));
    const payload = { prompt: it.prompt, gold_sql: it.gold_sql, title: it.title, difficulty: it.difficulty, ddl };
    if (confirmedNudges?.length) payload.confirmed_nudges = confirmedNudges;
    const { job_id } = await api.author(payload);
    const id = setInterval(async () => {
      const j = await api.job(job_id);
      if (j.state === "running") return;
      clearInterval(id);
      setBatchResults((br) => ({
        ...br,
        sections: br.sections.map((sec, jj) => (jj === s
          ? { ...sec, items: sec.items.map((r, k) => (k === i ? j.result : r)) }
          : sec)),
      }));
      if (j.result?.status !== "ok") {
        setBanner({ kind: "err", text: `Re-authoring "${it.title}" failed at ${j.result?.stage}: ${j.result?.reason}` });
      }
    }, 1200);
  }

  // re-author every item in a section against an edited shared schema; updates that
  // section's ddl so subsequent re-authors and "add to set" use the new schema/results.
  // Each item's re-author runs independently (own job + poll), same as a single re-author.
  function reauthorSection(s, ddl) {
    setBanner(null);
    setBatchResults((br) => ({
      ...br,
      sections: br.sections.map((sec, j) => (j === s ? { ...sec, ddl } : sec)),
    }));
    for (let i = 0; i < sections[s].items.length; i++) {
      reauthorBatchItem(s, i, undefined, ddl);
    }
  }

  async function publish() {
    try {
      const info = await api.publish(setId);
      await refreshSets();
      // reload the set so its published_at refreshes immediately (the Classes tab keys off it)
      await api.getSet(setId).then(setSet);
      setBanner({ kind: "ok", text: `Published "${info.title}" — ${info.n_problems} problems baked, gold answers stripped. Create or attach a class on the Classes tab.` });
    } catch (e) {
      setBanner({ kind: "err", text: e.message });
    }
  }

  // ---- set management ----
  async function reload() {
    await api.getSet(setId).then(setSet);
  }

  async function moveProblem(pid, dir) {
    const ids = set.problems.map((p) => p.id);
    const i = ids.indexOf(pid);
    const j = i + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    await api.reorderProblems(setId, ids);
    await reload();
  }

  async function deleteProblem(pid) {
    if (!window.confirm("Remove this problem from the set?")) return;
    await api.removeProblem(setId, pid);
    await reload();
  }

  async function patchProblem(pid, fields) {
    await api.updateProblem(setId, pid, fields);
    await reload();
  }

  // re-author a problem's data generator against an edited schema, keeping its id + position.
  // Returns the final job result ({status:'ok',...} or {status:'error', stage, reason}).
  async function reauthorProblemSchema(pid, ddl) {
    const { job_id } = await api.reauthorProblem(setId, pid, ddl);
    return new Promise((resolve) => {
      const id = setInterval(async () => {
        const j = await api.job(job_id);
        if (j.state === "running") return;
        clearInterval(id);
        if (j.result?.status === "ok") await reload();
        resolve(j.result);
      }, 1200);
    });
  }

  async function renameSet(title) {
    try {
      await api.renameSet(setId, title);
      await refreshSets();
      await api.getSet(setId).then(setSet);
      setBanner({ kind: "ok", text: `Renamed to "${title}".` });
    } catch (e) {
      setBanner({ kind: "err", text: e.message });
    }
  }

  async function deleteSet() {
    if (!window.confirm("Delete this set and its published version? This cannot be undone.")) return;
    try {
      await api.removeSet(setId);
      setBanner({ kind: "ok", text: "Set deleted." });
      setSetId("");
      await refreshSets();
    } catch (e) {
      setBanner({ kind: "err", text: e.message });
    }
  }

  async function exportSet() {
    const data = await api.exportSet(setId);
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = `${data.id}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function importSetFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const created = await api.importSet(parsed);
      await refreshSets();
      setSetId(created.id);
      setBanner({ kind: "ok", text: `Imported "${created.title}" as a new set.` });
    } catch (err) {
      setBanner({ kind: "err", text: `Import failed: ${err.message}` });
    } finally {
      e.target.value = "";
    }
  }

  const running = job?.state === "running";
  const batchRunning = batchJob?.state === "running";

  return (
    <div className="main">
      <aside className="rail">
        <div className="rail-head">
          <h2>Your sets</h2>
          <div className="rail-sub">Author problems, then publish a set for practice.</div>
          <button className="btn sm" onClick={() => importRef.current?.click()}>Import set</button>
        </div>
        <ul className="plist">
          <li>
            <button className={"pitem" + (setId === "" ? " on" : "")} onClick={() => selectRail("")}>
              <span className="numeral">＋</span>
              <span className="pitem-title">New / unfiled problem</span>
              <span className="pitem-meta" style={{ gridColumn: 2 }}>start here</span>
            </button>
          </li>
          {sets.map((s, i) => (
            <li key={s.id}>
              <button className={"pitem" + (setId === s.id ? " on" : "")} onClick={() => selectRail(s.id)}>
                <span className="numeral">{String(i + 1).padStart(2, "0")}</span>
                <span className="pitem-title">{s.title}</span>
                <span className="pitem-meta" style={{ gridColumn: 2 }}>
                  {s.n_problems} problems · {s.published_at ? "published" : "draft"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="work">
        <div className="work-wrap">
          {banner && <div className={"banner " + banner.kind}>{banner.text}</div>}

          <input ref={importRef} type="file" accept="application/json" style={{ display: "none" }} onChange={importSetFile} />

          {view === "set" && set ? (
            <>
              <SetCard
                set={set}
                onPublish={publish}
                onMove={moveProblem}
                onDelete={deleteProblem}
                onPatch={patchProblem}
                onReauthorSchema={reauthorProblemSchema}
                onExport={exportSet}
                onImportClick={() => importRef.current?.click()}
                onAuthorInto={authorIntoThisSet}
                onDeleteSet={deleteSet}
                onRename={renameSet}
              />

              {set.published_at && (
                <div className="run-hint" style={{ marginTop: 14 }}>
                  Published. Create a class for it on the <b>Classes</b> tab.
                </div>
              )}
            </>
          ) : (
            <>
              <div className="prompt-block">
                <h1>Author a problem</h1>
                <p className="prompt-text" style={{ fontSize: 15 }}>
                  Write what you'd write to set an exam — the question and the answer. The model builds
                  the schema and the test data; you never write either.
                </p>
                <div className="run-hint" style={{ marginTop: 6 }}>
                  {targetSet ? <>adding to: <b>{targetSet.title}</b></> : "no set yet — one will be created"}
                </div>
              </div>

              <div className="batch-mode-switch">
                <button className={"batch-mode-btn" + (mode === "single" ? " on" : "")} onClick={() => setMode("single")}>
                  Single problem
                </button>
                <button className={"batch-mode-btn" + (mode === "assignment" ? " on" : "")} onClick={() => setMode("assignment")}>
                  Whole assignment
                </button>
              </div>

          {mode === "single" ? (
            <>
            <div className={"author-grid" + (authored ? " has-result" : "")}>
              <div>
                <div className="field">
                  <label>Title</label>
                  <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Top subjects by enrollment" />
                </div>
                <div className="field">
                  <label>Question <span className="hint-line">— what the student reads</span></label>
                  <textarea className="input" rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
                    placeholder="From the class_schedule table, list the five subjects with the most enrolled students…" />
                </div>
                <div className="field">
                  <label>Gold SQL <span className="hint-line">— stays private; never shown to students</span></label>
                  <textarea className="input mono" rows={6} value={gold} onChange={(e) => setGold(e.target.value)}
                    placeholder={"SELECT subject, SUM(num_students) AS tot\nFROM class_schedule\nGROUP BY subject\nORDER BY tot DESC\nLIMIT 5"} />
                </div>
                <div className="field">
                  <label>Difficulty</label>
                  <select className="input" value={difficulty} onChange={(e) => setDifficulty(e.target.value)} style={{ maxWidth: 180 }}>
                    <option value="easy">Easy</option>
                    <option value="medium">Medium</option>
                    <option value="hard">Hard</option>
                  </select>
                </div>
                <label className="field" style={{ display: "flex", gap: 8, alignItems: "baseline", cursor: "pointer" }}>
                  <input type="checkbox" checked={predict} onChange={(e) => setPredict(e.target.checked)} />
                  <span style={{ fontSize: 13.5 }}>
                    Predict difficulty <span className="hint-line">— a simulated student attempts it first (~1–2 min extra)</span>
                  </span>
                </label>
                <button className="btn primary" onClick={() => startAuthor()} disabled={running}>
                  {running ? <span className="thinking" style={{ color: "#fff" }}><span className="spin" style={{ borderTopColor: "#fff" }} /> Authoring — schema, data, stress test…</span> : "Author with the model"}
                </button>
              </div>

              {!authored && (
                <div>
                  {!running && (
                    <div className="card" style={{ padding: 20, color: "var(--muted)" }}>
                      <div className="eyebrow" style={{ marginBottom: 8 }}>What you'll get</div>
                      <p className="prompt-text" style={{ fontSize: 14.5 }}>
                        An inferred schema, the SQL features it exercises, any plain-English edge-case
                        questions worth confirming, and a preview of the seeded data with your gold
                        result — all gated on a 60-seed robustness test before you publish.
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>

            {authored && (
              <div className="author-result-full">
                <AuthorResult r={authored} onAdd={addToSetSingle} onReauthor={startAuthor} running={running} />
              </div>
            )}
            </>
          ) : (
            <div className="batch-form">
              <div className="field">
                <label>Assignment title <span className="hint-line">— names the set these questions land in</span></label>
                <input className="input" value={assignmentTitle} onChange={(e) => setAssignmentTitle(e.target.value)}
                       placeholder="e.g. Week 3 — Aggregation drills" />
              </div>
              {sections.map((sec, s) => (
                <div className="batch-section" key={s}>
                  <div className="batch-item-head">
                    <span className="eyebrow">Section {s + 1}</span>
                    {sections.length > 1 && (
                      <button className="batch-remove" onClick={() => removeSection(s)} data-tip="Remove section">✕</button>
                    )}
                  </div>
                  <div className="field">
                    <label>What the table(s) hold <span className="hint-line">— this section's own schema</span></label>
                    <textarea className="input" rows={2} value={sec.tableHint} onChange={(e) => updateSectionHint(s, e.target.value)}
                      placeholder="a library books table: book_id, title, author, year, copies" />
                  </div>

                  {sec.items.map((it, i) => (
                    <div className="batch-item" key={i}>
                      <div className="batch-item-head">
                        <span className="eyebrow">Question {i + 1}</span>
                        {sec.items.length > 1 && (
                          <button className="batch-remove" onClick={() => removeItem(s, i)} data-tip="Remove question">✕</button>
                        )}
                      </div>
                      <div className="field">
                        <label>Title</label>
                        <input className="input" value={it.title} onChange={(e) => updateItem(s, i, "title", e.target.value)} placeholder="Count all books" />
                      </div>
                      <div className="field">
                        <label>Question</label>
                        <textarea className="input" rows={3} value={it.prompt} onChange={(e) => updateItem(s, i, "prompt", e.target.value)}
                          placeholder="How many books are there?" />
                      </div>
                      <div className="field">
                        <label>Gold SQL</label>
                        <textarea className="input mono" rows={4} value={it.gold_sql} onChange={(e) => updateItem(s, i, "gold_sql", e.target.value)}
                          placeholder="SELECT COUNT(*) FROM books;" />
                      </div>
                      <div className="field">
                        <label>Difficulty</label>
                        <select className="input" value={it.difficulty} onChange={(e) => updateItem(s, i, "difficulty", e.target.value)} style={{ maxWidth: 180 }}>
                          <option value="easy">Easy</option>
                          <option value="medium">Medium</option>
                          <option value="hard">Hard</option>
                        </select>
                      </div>
                    </div>
                  ))}

                  <button className="btn sm" onClick={() => addItem(s)} disabled={batchRunning}>+ add question</button>
                </div>
              ))}

              <div style={{ marginTop: 4 }}>
                <button className="btn sm" onClick={addSection} disabled={batchRunning}>+ add section</button>
              </div>

              <div style={{ marginTop: 16 }}>
                <button className="btn primary" onClick={startBatch} disabled={batchRunning}>
                  {batchRunning
                    ? <span className="thinking" style={{ color: "#fff" }}>
                        <span className="spin" style={{ borderTopColor: "#fff" }} />
                        {batchJob?.progress
                          ? ` authoring ${Math.min(batchJob.progress.done + 1, batchJob.progress.total)} of ${batchJob.progress.total}: ${batchJob.progress.current}`
                          : " authoring…"}
                      </span>
                    : "Author the assignment"}
                </button>
              </div>

              {batchResults && batchResults.sections.map((br, s) => (
                <div key={s} style={{ marginTop: 20 }}>
                  <div className="section-label"><span className="eyebrow">Section {s + 1} — inferred schema</span></div>
                  <SectionSchema ddl={br.ddl} onReauthorSection={(ddl) => reauthorSection(s, ddl)} />
                  {sections[s].items.map((it, i) => (
                    <BatchItemResult
                      key={i}
                      title={it.title}
                      result={br.items[i]}
                      onAdd={(problem) => addToSet(problem, assignmentTitle)}
                      onReauthor={(confirmed) => reauthorBatchItem(s, i, confirmed)}
                    />
                  ))}
                </div>
              ))}
            </div>
          )}
            </>
          )}
        </div>
      </section>
    </div>
  );
}

// ---- a batch section's shared schema: read-only by default, editable + re-authorable ---- //
function SectionSchema({ ddl, onReauthorSection }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(ddl);

  useEffect(() => { setText(ddl); }, [ddl]);

  if (!editing) {
    return (
      <>
        <Schema ddl={ddl} />
        <button className="btn sm" style={{ marginTop: 8 }} onClick={() => { setText(ddl); setEditing(true); }}>
          Edit schema
        </button>
      </>
    );
  }

  return (
    <div>
      <textarea className="input mono" rows={8} value={text} onChange={(e) => setText(e.target.value)} />
      <div style={{ marginTop: 8, display: "flex", gap: 8 }}>
        <button
          className="btn primary sm"
          onClick={() => { onReauthorSection(text); setEditing(false); }}
          disabled={!text.trim()}
        >
          Re-author section with this schema
        </button>
        <button className="btn sm" onClick={() => setEditing(false)}>Cancel</button>
      </div>
    </div>
  );
}

// ---- per-item result inside a batch (collapsed by default, expandable) ---- //
function BatchItemResult({ title, result, onAdd, onReauthor }) {
  const [open, setOpen] = useState(false);
  if (!result) return null;

  if (result.status === "running") {
    return (
      <div className="batch-result">
        <button className="batch-result-head" onClick={() => setOpen((o) => !o)}>
          <span>{title}</span>
          <span className="thinking"><span className="spin" /> re-authoring…</span>
        </button>
      </div>
    );
  }

  if (result.status !== "ok") {
    return (
      <div className="batch-result">
        <button className="batch-result-head" onClick={() => setOpen((o) => !o)}>
          <span>{title}</span>
          <span className="bad-pill">✕ failed at {result.stage}</span>
        </button>
        {open && <div className="run-hint" style={{ padding: "8px 0" }}>{result.reason}</div>}
      </div>
    );
  }

  return (
    <div className="batch-result">
      <button className="batch-result-head" onClick={() => setOpen((o) => !o)}>
        <span>{title}</span>
        <span className={result.stress.ok ? "ok-pill" : "bad-pill"}>
          {result.stress.ok ? "✓ robust" : "✕ weak"}
        </span>
      </button>
      {open && (
        <div className="batch-result-body">
          <AuthorResult r={result} onAdd={() => onAdd(result.problem)} onReauthor={onReauthor} running={false} />
        </div>
      )}
    </div>
  );
}

// ---- set management card: problem list with reorder/delete/edit, export/import ---- //
function SetCard({ set, onPublish, onMove, onDelete, onPatch, onReauthorSchema, onExport, onImportClick, onAuthorInto, onDeleteSet, onRename }) {
  const [editing, setEditing] = useState(null); // problem id being edited
  const [editTitle, setEditTitle] = useState("");
  const [editDifficulty, setEditDifficulty] = useState("medium");
  const [editPrompt, setEditPrompt] = useState("");
  const [editSchema, setEditSchema] = useState("");
  const [rebuilding, setRebuilding] = useState(null); // problem id currently rebuilding, or null
  const [rowError, setRowError] = useState(null); // {pid, text}

  // set-title rename (the set keeps its id; only the display title changes)
  const [renaming, setRenaming] = useState(false);
  const [nameDraft, setNameDraft] = useState(set.title);
  const [savingName, setSavingName] = useState(false);
  useEffect(() => { setNameDraft(set.title); setRenaming(false); }, [set.id, set.title]);

  async function saveRename() {
    const t = nameDraft.trim();
    if (!t || t === set.title) { setRenaming(false); return; }
    setSavingName(true);
    await onRename(t);
    setSavingName(false);
    setRenaming(false);
  }

  const unpublished = set.published_at && set.updated && set.updated > set.published_at;

  function startEdit(p) {
    setEditing(p.id);
    setEditTitle(p.title);
    setEditDifficulty(p.difficulty || "medium");
    setEditPrompt(p.prompt || "");
    setEditSchema(p.schema || "");
    setRowError(null);
  }

  async function saveEdit(p) {
    const pid = p.id;
    const schemaChanged = editSchema !== (p.schema || "");
    if (schemaChanged) {
      setEditing(null);
      setRebuilding(pid);
      setRowError(null);
      const result = await onReauthorSchema(pid, editSchema);
      setRebuilding(null);
      if (result?.status !== "ok") {
        setRowError({ pid, text: `Rebuild failed at ${result?.stage}: ${result?.reason}` });
        return;
      }
      // title/difficulty/prompt may have changed too — apply them on the rebuilt problem
      if (editTitle !== p.title || editDifficulty !== (p.difficulty || "medium") || editPrompt !== (p.prompt || "")) {
        await onPatch(pid, { title: editTitle, difficulty: editDifficulty, prompt: editPrompt });
      }
      return;
    }
    await onPatch(pid, { title: editTitle, difficulty: editDifficulty, prompt: editPrompt });
    setEditing(null);
  }

  return (
    <div className="card" style={{ padding: "14px 18px", marginBottom: 22 }}>
      {renaming ? (
        <div className="set-head set-head-editing">
          <input
            className="input"
            value={nameDraft}
            autoFocus
            onChange={(e) => setNameDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") saveRename(); if (e.key === "Escape") setRenaming(false); }}
          />
          <button className="btn sm primary" onClick={saveRename} disabled={savingName || !nameDraft.trim()}>
            {savingName ? "Saving…" : "Save"}
          </button>
          <button className="btn sm ghost" onClick={() => { setNameDraft(set.title); setRenaming(false); }} disabled={savingName}>Cancel</button>
        </div>
      ) : (
        <div className="set-head">
          <h2 className="set-head-title">{set.title}</h2>
          <button className="mgmt-btn" onClick={() => { setNameDraft(set.title); setRenaming(true); }} data-tip="Rename set">Rename</button>
        </div>
      )}
      <button className="btn sm" style={{ marginBottom: 10 }} onClick={onAuthorInto}>
        + Author a problem into this set
      </button>
      {!set.published_at ? null : (
        <div className="run-hint" style={{ marginBottom: 6 }}>
          last published {new Date(set.published_at).toLocaleString()}
          {unpublished && <span className="mgmt-unpublished"> · edited since last publish</span>}
        </div>
      )}
      {set.problems.length === 0 ? (
        <div className="run-hint">No problems yet — author one below.</div>
      ) : (
        <ol className="mgmt-list">
          {set.problems.map((p, i) => (
            <li key={p.id} className="mgmt-row" style={{ flexDirection: "column", alignItems: "stretch" }}>
              {editing === p.id ? (
                <div className="mgmt-edit">
                  <input className="input" value={editTitle} onChange={(e) => setEditTitle(e.target.value)} />
                  <select className="input" value={editDifficulty} onChange={(e) => setEditDifficulty(e.target.value)} style={{ maxWidth: 140 }}>
                    <option value="easy">Easy</option>
                    <option value="medium">Medium</option>
                    <option value="hard">Hard</option>
                  </select>
                  <textarea className="input" rows={2} value={editPrompt} onChange={(e) => setEditPrompt(e.target.value)} style={{ gridColumn: "1 / -1" }} />
                  <div className="field" style={{ gridColumn: "1 / -1", marginBottom: 0 }}>
                    <label>Schema <span className="hint-line">— editing the schema rebuilds the practice data against it</span></label>
                    <textarea className="input mono" rows={5} value={editSchema} onChange={(e) => setEditSchema(e.target.value)} />
                  </div>
                  <div className="mgmt-edit-actions">
                    <button className="btn sm primary" onClick={() => saveEdit(p)}>Save</button>
                    <button className="btn sm" onClick={() => setEditing(null)}>Cancel</button>
                  </div>
                </div>
              ) : (
                <div className="mgmt-row" style={{ padding: 0, border: "none" }}>
                  <span className="mgmt-row-main" onClick={() => startEdit(p)} data-tip="Click to edit">
                    <b>{p.title}</b> <span className="run-hint">· {p.target_clauses?.join(", ")}</span>
                  </span>
                  {rebuilding === p.id ? (
                    <span className="thinking"><span className="spin" /> rebuilding data…</span>
                  ) : (
                    <span className="mgmt-row-actions">
                      <button className="mgmt-btn" disabled={i === 0} onClick={() => onMove(p.id, -1)} data-tip="Move up">↑</button>
                      <button className="mgmt-btn" disabled={i === set.problems.length - 1} onClick={() => onMove(p.id, 1)} data-tip="Move down">↓</button>
                      <button className="mgmt-btn mgmt-danger" onClick={() => onDelete(p.id)} data-tip="Remove">✕</button>
                    </span>
                  )}
                </div>
              )}
              {rowError?.pid === p.id && (
                <div className="banner err" style={{ margin: "6px 0 0" }}>{rowError.text}</div>
              )}
            </li>
          ))}
        </ol>
      )}
      <div className="mgmt-footer">
        <button className="btn lime sm" disabled={!set.problems.length} onClick={onPublish}>
          {set.published_at ? "Re-publish set" : "Publish set"}
        </button>
        <button className="btn sm" onClick={onExport}>Export JSON</button>
        <button className="btn sm" onClick={onImportClick}>Import set</button>
        <button className="btn sm mgmt-danger-btn" onClick={onDeleteSet} style={{ marginLeft: "auto" }}>Delete set</button>
      </div>
    </div>
  );
}

function AuthorResult({ r, onAdd, onReauthor, running }) {
  const pv = r.preview || {};
  // answers: nudge id -> "yes" | "no" | undefined (unanswered). Reset whenever a fresh
  // authoring result arrives (new object identity), so a re-author starts clean.
  const [answers, setAnswers] = useState({});
  // add-to-set guard: once added, the button locks so the same problem can't be added twice
  const [added, setAdded] = useState(false);
  const [adding, setAdding] = useState(false);
  useEffect(() => { setAnswers({}); setAdded(false); setAdding(false); }, [r]);

  // nudges already baked into this (re-authored) data move to the "Guaranteed in the data"
  // line below — drop them from the open questions so a confirmed nudge stops being asked.
  const enforced = new Set(r.enforced_nudges || []);
  const nudges = (r.nudges || []).filter((n) => !enforced.has(n.id));
  const anyYes = nudges.some((n) => answers[n.id] === "yes");

  function reauthor() {
    const confirmed = nudges
      .filter((n) => answers[n.id] === "yes")
      .map((n) => ({ id: n.id, question: n.question, assert_sql: n.assert_sql }));
    onReauthor(confirmed);
  }

  async function add() {
    setAdding(true);
    try {
      await onAdd();
      setAdded(true);
    } finally {
      setAdding(false);
    }
  }

  return (
    <div>
      {r.kind === "state" && (
        <div className="tag state-tag" style={{ marginBottom: 10 }}>
          Statement question — graded by final database state
        </div>
      )}

      <div className="statline" style={{ marginBottom: 12 }}>
        <span className={r.stress.ok ? "ok-pill" : "bad-pill"}>
          {r.stress.ok ? "✓ robust" : "✕ weak"}
        </span>
        <span><b>{r.stress.distinct_datasets}</b>/{r.stress.n_seeds} distinct datasets</span>
        <span>authored in <b>{r.attempts}</b> {r.attempts === 1 ? "attempt" : "attempts"}</span>
        <span className="run-hint">{r.elapsed_s}s</span>
      </div>

      {r.prediction && (
        <div className="statline" style={{ marginBottom: 12 }}>
          <span className="run-hint">AI student forecast:</span>
          <span>solved <b>{r.prediction.solved_unaided}</b>/{r.prediction.trials} unaided</span>
          <span>avg hint level needed <b>{r.prediction.avg_max_hint_level}</b></span>
        </div>
      )}

      {!r.prediction && r.prediction_note && (
        <div className="hint-line" style={{ display: "block", marginBottom: 12 }}>{r.prediction_note}</div>
      )}

      <div className="section-label"><span className="eyebrow">Inferred schema</span></div>
      <Schema ddl={r.problem.schema} />

      <div className="clauses" style={{ marginTop: 10 }}>
        {r.problem.target_clauses.map((c) => <span className="clause" key={c}>{c}</span>)}
      </div>

      {r.enforced_nudges?.length > 0 && (
        <div className="nudge-guarantee" style={{ marginTop: 14 }}>
          Guaranteed in the data: {r.enforced_nudges
            .map((id) => nudges.find((n) => n.id === id)?.column || id)
            .join(", ")}
        </div>
      )}

      {nudges.length > 0 && (
        <>
          <div className="section-label" style={{ marginTop: 20 }}><span className="eyebrow">Worth confirming</span></div>
          {nudges.map((n) => (
            <div className="nudge" key={n.id}>
              <span className="q">{n.question}</span>
              <span className="nudge-actions">
                <button
                  className={"nudge-btn yes" + (answers[n.id] === "yes" ? " on" : "")}
                  onClick={() => setAnswers((a) => ({ ...a, [n.id]: "yes" }))}
                >YES</button>
                <button
                  className={"nudge-btn no" + (answers[n.id] === "no" ? " on" : "")}
                  onClick={() => setAnswers((a) => ({ ...a, [n.id]: "no" }))}
                >NO</button>
              </span>
            </div>
          ))}
          {anyYes && (
            <button className="btn primary sm" style={{ marginTop: 10 }} onClick={reauthor} disabled={running}>
              {running ? <span className="thinking" style={{ color: "#fff" }}><span className="spin" style={{ borderTopColor: "#fff" }} /> Re-authoring…</span> : "Re-author with confirmations"}
            </button>
          )}
        </>
      )}

      {pv.gold_preview && (
        <>
          <div className="section-label" style={{ marginTop: 20 }}><span className="eyebrow">Your gold result · seed 1 · {pv.gold_preview.n_rows} rows</span></div>
          <DataTable cols={pv.gold_preview.cols} rows={pv.gold_preview.rows} />
        </>
      )}

      {pv.tables && Object.entries(pv.tables).map(([name, t]) => (
        <div key={name}>
          <div className="table-name">{name}</div>
          <DataTable cols={t.cols} rows={t.rows} />
        </div>
      ))}

      {pv.post_tables && (
        <>
          <div className="section-label" style={{ marginTop: 20 }}><span className="eyebrow">After the gold statement runs</span></div>
          {Object.entries(pv.post_tables).map(([name, t]) => (
            <div key={name}>
              <div className="table-name">{name}</div>
              <DataTable cols={t.cols} rows={t.rows} />
            </div>
          ))}
        </>
      )}

      <div style={{ marginTop: 18 }}>
        <button className="btn primary" onClick={add} disabled={adding || added}>
          {added ? "✓ Added to set" : adding ? "Adding…" : "Add to set"}
        </button>
      </div>
    </div>
  );
}
