import { useEffect, useRef, useState } from "react";
import QRCode from "qrcode";
import { api } from "../lib/api.js";
import SessionControls from "../components/SessionControls.jsx";

// Classrooms live in their own flow now (not tucked under a single set). A classroom assigns
// one OR MORE published sets, runs in open / roster / passcode mode, and can be archived or
// scheduled to a window. Insights reads the same classes and lets you drill in per set.

const MODES = [
  { key: "open", label: "Open", hint: "Any name may join with the class code." },
  { key: "roster", label: "Roster", hint: "Only names on the roster may join with the class code." },
  { key: "passcode", label: "Passcodes", hint: "Each student gets a personal 3-word passcode and signs in with that alone — no class code, no name." },
];

const STATE_BADGE = {
  active: { cls: "cls-badge-active", label: "active" },
  archived: { cls: "cls-badge-archived", label: "archived" },
  scheduled: { cls: "cls-badge-scheduled", label: "scheduled" },
  closed: { cls: "cls-badge-closed", label: "closed" },
};

export default function Classes() {
  const [sets, setSets] = useState([]);
  const [classes, setClasses] = useState(null);
  const [err, setErr] = useState(null);

  function refresh() {
    api.listClasses().then(setClasses).catch((e) => { setClasses([]); setErr(e.message); });
  }
  useEffect(() => {
    api.instructorSets().then(setSets).catch(() => setSets([]));
    refresh();
  }, []);

  const published = sets.filter((s) => s.published_at);

  return (
    <div className="main">
      <section className="work" style={{ gridColumn: "1 / -1" }}>
        <div className="work-wrap">
          {err && <div className="banner err">{err}</div>}

          <div className="prompt-block">
            <h1>Classrooms</h1>
            <p className="prompt-text" style={{ fontSize: 15 }}>
              A classroom assigns one or more published sets to a group of students, hands out a
              join code (or personal passcodes), and collects every attempt for Insights.
            </p>
          </div>

          <SyncSettings />

          <CreateClass published={published} onCreated={refresh} />

          <div className="section-label" style={{ marginTop: 30 }}>
            <span className="eyebrow">Your classrooms</span>
          </div>
          {classes === null ? (
            <div className="empty"><span className="spin" /></div>
          ) : classes.length === 0 ? (
            <div className="cls-item-meta">no classrooms yet</div>
          ) : (
            <div className="cls-list">
              {classes.map((c) => (
                <ClassRow key={c.id} cls={c} published={published} onChanged={refresh} />
              ))}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

// ---- network-sync settings ----
// Two ways a class collects attempts:
//   • file sync (default) — students export an attempts file, the instructor imports it.
//   • network sync — THIS machine hosts the class server on the LAN; students connect to its
//     URL (one click sets it from the detected LAN address) and push attempts live. The URL is
//     also baked into every exported assignment, so the file remains a working backup.
function SyncSettings() {
  const [saved, setSaved] = useState(null);      // persisted instructor_url
  const [host, setHost] = useState(null);        // { lan_urls, port, hostname }
  const [pick, setPick] = useState("");          // chosen LAN url (one-click host)
  const [manual, setManual] = useState(false);   // manual-URL editor open
  const [url, setUrl] = useState("");            // manual-URL field
  const [qr, setQr] = useState(null);            // data-URL for the QR image
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  useEffect(() => {
    api.instructorConfig()
      .then((c) => { setSaved(c.instructor_url || ""); setUrl(c.instructor_url || ""); })
      .catch(() => {});
    api.hostInfo().then((h) => { setHost(h); setPick((h.lan_urls || [])[0] || ""); }).catch(() => {});
  }, []);

  useEffect(() => {
    if (saved) QRCode.toDataURL(saved, { margin: 1, width: 168 }).then(setQr).catch(() => setQr(null));
    else setQr(null);
  }, [saved]);

  async function persist(value) {
    setBusy(true); setMsg(null);
    try {
      const c = await api.setInstructorConfig({ instructor_url: (value || "").trim() });
      setSaved(c.instructor_url || "");
      setUrl(c.instructor_url || "");
      setManual(false);
      setMsg(c.instructor_url ? "Hosting on this network" : "Network sync turned off");
      setTimeout(() => setMsg(null), 4000);
    } catch (e) {
      setMsg(e.message);
    } finally {
      setBusy(false);
    }
  }

  const on = !!saved;
  const lanUrls = host?.lan_urls || [];

  return (
    <div className="card" style={{ padding: "14px 16px", marginBottom: 14 }}>
      <div className="cls-row" style={{ alignItems: "center", flexWrap: "wrap", gap: 10 }}>
        <span className={"cls-badge " + (on ? "cls-badge-active" : "cls-badge-closed")}>
          {on ? "network sync" : "file sync"}
        </span>
        <span className="cls-item-meta" style={{ flex: 1, minWidth: 220 }}>
          {on
            ? <>Students on this network can connect and push attempts live. The address is also baked into exported assignment files as a backup.</>
            : <>Classes sync by file: students export an attempts file, you import it. Host this machine on your LAN for live sync.</>}
        </span>
        {msg && <span className="cls-item-meta">{msg}</span>}
      </div>

      {on ? (
        <div className="netsync-live">
          <div className="netsync-qr">
            {qr ? <img src={qr} alt="QR of the class server address" width={140} height={140} /> : null}
          </div>
          <div className="netsync-live-body">
            <div className="netsync-url-label">Class server address</div>
            <div className="netsync-url mono">{saved}</div>
            <div className="cls-item-meta" style={{ marginTop: 6 }}>
              Students: open <b>Practice → Connect to class server</b>, enter this address and their
              class code (or personal passcode). Same Wi-Fi/LAN required.
            </div>
            <div className="cls-row" style={{ marginTop: 10, gap: 8 }}>
              <button className="btn sm ghost" onClick={() => setManual((v) => !v)}>Change address</button>
              <button className="btn sm danger" onClick={() => persist("")} disabled={busy}>Turn off</button>
            </div>
          </div>
        </div>
      ) : (
        <div className="cls-row" style={{ marginTop: 12, flexWrap: "wrap", gap: 8, alignItems: "center" }}>
          {lanUrls.length > 1 && (
            <select className="input cls-setselect" value={pick} onChange={(e) => setPick(e.target.value)}>
              {lanUrls.map((u) => <option key={u} value={u}>{u}</option>)}
            </select>
          )}
          <button
            className="btn sm primary"
            onClick={() => persist(pick || lanUrls[0])}
            disabled={busy || lanUrls.length === 0}
            data-tip={lanUrls.length === 0 ? "No LAN address detected" : pick || lanUrls[0]}
          >
            {lanUrls.length ? <>Host on this network ({(pick || lanUrls[0])?.replace(/^https?:\/\//, "")})</> : "No LAN address detected"}
          </button>
          <button className="btn sm ghost" onClick={() => setManual((v) => !v)}>Enter URL manually</button>
        </div>
      )}

      {manual && (
        <div className="cls-row" style={{ marginTop: 10, flexWrap: "wrap", gap: 8 }}>
          <input
            className="input"
            style={{ flex: 1, minWidth: 260 }}
            placeholder="http://192.168.1.5:8077"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") persist(url); if (e.key === "Escape") setManual(false); }}
          />
          <button className="btn sm primary" onClick={() => persist(url)} disabled={busy}>{busy ? "Saving…" : "Save"}</button>
          <button className="btn sm ghost" onClick={() => { setUrl(saved); setManual(false); }}>Cancel</button>
        </div>
      )}
    </div>
  );
}

// ---- multi-set picker (shared by create + edit) ----
function SetPicker({ published, selected, onToggle }) {
  if (published.length === 0) {
    return <div className="run-hint">No published sets yet — publish a set on the Sets tab first.</div>;
  }
  return (
    <div className="cls-setpick">
      {published.map((s) => (
        <label key={s.id} className={"cls-setpick-item" + (selected.includes(s.id) ? " on" : "")}>
          <input
            type="checkbox"
            checked={selected.includes(s.id)}
            onChange={() => onToggle(s.id)}
          />
          <span>{s.title}</span>
        </label>
      ))}
    </div>
  );
}

function CreateClass({ published, onCreated }) {
  const [title, setTitle] = useState("");
  const [mode, setMode] = useState("open");
  const [setIds, setSetIds] = useState([]);
  const [roster, setRoster] = useState("");
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState(null);

  function toggleSet(id) {
    setSetIds((arr) => (arr.includes(id) ? arr.filter((x) => x !== id) : [...arr, id]));
  }

  async function create() {
    if (!title.trim() || setIds.length === 0) return;
    setCreating(true);
    setErr(null);
    try {
      const names = roster.split("\n").map((s) => s.trim()).filter(Boolean);
      await api.newClass(title.trim(), setIds, mode, mode === "open" ? [] : names);
      setTitle(""); setRoster(""); setSetIds([]); setMode("open");
      onCreated();
    } catch (e) {
      setErr(e.message);
    } finally {
      setCreating(false);
    }
  }

  const modeHint = MODES.find((m) => m.key === mode)?.hint;

  return (
    <div className="card" style={{ padding: "16px 18px" }}>
      <div className="section-label"><span className="eyebrow">New classroom</span></div>

      <div className="field">
        <label>Title</label>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)}
               placeholder="e.g. CS284 — Section A" />
      </div>

      <div className="field">
        <label>Assigned sets <span className="hint-line">— pick one or more published sets</span></label>
        <SetPicker published={published} selected={setIds} onToggle={toggleSet} />
      </div>

      <div className="field">
        <label>Mode</label>
        <div className="cls-mode-toggle">
          {MODES.map((m) => (
            <button key={m.key} type="button"
                    className={"cls-mode-btn" + (mode === m.key ? " on" : "")}
                    onClick={() => setMode(m.key)}>{m.label}</button>
          ))}
        </div>
        <span className="hint-line" style={{ marginLeft: 0, display: "block", marginTop: 6 }}>{modeHint}</span>
      </div>

      {mode !== "open" && (
        <div className="field">
          <label>Roster <span className="hint-line">one name per line</span></label>
          <textarea className="input cls-roster" value={roster} onChange={(e) => setRoster(e.target.value)}
                    placeholder={"Ada Lovelace\nAlan Turing"} />
        </div>
      )}

      {err && <div className="cls-err" style={{ marginBottom: 10 }}>{err}</div>}

      <button className="btn primary sm" onClick={create}
              disabled={creating || !title.trim() || setIds.length === 0}>
        {creating ? "Creating…" : "Create classroom"}
      </button>
    </div>
  );
}

// ---- per-class student management (view list, delete a student, drill into one student
// to delete their individual attempts) ----
function fmtTime(ts) {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString();
}

function describeEvent(ev) {
  const t = ev.title || ev.problem_id;
  if (ev.kind === "grade") {
    return ev.correct
      ? `Solved ${t} (attempt #${ev.attempt_no}) — passed ${ev.n_passed}/${ev.n_seeds}`
      : `Attempt #${ev.attempt_no} on ${t}${ev.category ? ` — ${ev.category}` : ""} — passed ${ev.n_passed ?? "?"}/${ev.n_seeds ?? "?"}`;
  }
  return `Asked for an L${ev.hint_level ?? "?"} hint on ${t}`;
}

function eventClass(ev) {
  if (ev.kind === "hint") return "ins-tl-hint";
  return ev.correct ? "ins-tl-solved" : "ins-tl-wrong";
}

// One student's full attempt log, each event individually deletable.
function StudentAttempts({ cls: c, student, onBack, onChanged }) {
  const [detail, setDetail] = useState(null);
  const [err, setErr] = useState(null);

  function load() {
    setErr(null);
    api.classStudent(c.id, student).then(setDetail).catch((e) => setErr(e.message));
  }
  useEffect(load, [c.id, student]);

  async function removeAttempt(uid) {
    if (!confirm("Delete this attempt? It is permanently removed from this student's history.")) return;
    try {
      await api.deleteAttempt(c.id, uid);
      load();
      onChanged?.();
    } catch (e) {
      alert("Delete failed: " + e.message);
    }
  }

  return (
    <div style={{ gridColumn: "1 / -1", marginTop: 6 }}>
      <div className="cls-row" style={{ alignItems: "center", gap: 10, marginBottom: 8 }}>
        <button className="btn sm ghost" onClick={onBack}>← Students</button>
        <span className="cls-item-title" style={{ margin: 0 }}>{student}</span>
        {detail && <span className="cls-item-meta">{detail.n_attempts} attempt{detail.n_attempts === 1 ? "" : "s"} · {detail.n_solved} solved</span>}
      </div>
      {err && <div className="cls-err">{err}</div>}
      {detail == null ? (
        <div className="empty"><span className="spin" /></div>
      ) : detail.timeline.length === 0 ? (
        <div className="cls-item-meta">No attempts logged for this student.</div>
      ) : (
        <ul className="ins-timeline">
          {detail.timeline.map((ev) => (
            <li key={ev.uid} className={"ins-tl-row " + eventClass(ev)}>
              <div className="ins-tl-when">{fmtTime(ev.ts)}</div>
              <div className="ins-tl-body" style={{ display: "flex", alignItems: "flex-start", gap: 10 }}>
                <div style={{ flex: 1 }}>
                  <div className="ins-tl-head">{describeEvent(ev)}</div>
                  {ev.sql && <pre className="ins-tl-sql">{ev.sql}</pre>}
                </div>
                <button className="btn sm danger" onClick={() => removeAttempt(ev.uid)}>Delete</button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Roster of students who have entered the classroom (have logged attempts), each deletable;
// click a name to drill into their attempt log.
function StudentsPanel({ cls: c, onChanged }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [active, setActive] = useState(null);   // selected student -> detail view
  const [msg, setMsg] = useState(null);

  function load() {
    setErr(null);
    api.getClass(c.id).then(setData).catch((e) => setErr(e.message));
  }
  useEffect(load, [c.id]);

  async function removeStudent(name) {
    if (!confirm(`Remove ${name} from "${c.title}"? Every attempt they logged is permanently deleted.`)) return;
    try {
      const r = await api.deleteStudent(c.id, name);
      setMsg(`Removed ${name} (${r.removed} attempt${r.removed === 1 ? "" : "s"} deleted)`);
      setTimeout(() => setMsg(null), 4000);
      load();
      onChanged?.();
    } catch (e) {
      alert("Delete failed: " + e.message);
    }
  }

  if (active) {
    return <StudentAttempts cls={c} student={active}
                            onBack={() => { setActive(null); load(); }}
                            onChanged={() => { load(); onChanged?.(); }} />;
  }

  const students = data?.students || [];
  return (
    <div style={{ gridColumn: "1 / -1", marginTop: 6 }}>
      {err && <div className="cls-err">{err}</div>}
      {msg && <div className="cls-item-meta" style={{ marginBottom: 6 }}>{msg}</div>}
      {data == null ? (
        <div className="empty"><span className="spin" /></div>
      ) : students.length === 0 ? (
        <div className="cls-item-meta">No students have entered this classroom yet.</div>
      ) : (
        <div className="table-scroll">
          <table className="datatable">
            <thead>
              <tr><th>Student</th><th>Attempts</th><th>Solved</th><th></th></tr>
            </thead>
            <tbody>
              {students.map((s) => (
                <tr key={s.student}>
                  <td>
                    <button className="btn sm ghost" style={{ padding: "2px 6px" }}
                            data-tip="View this student's attempts" onClick={() => setActive(s.student)}>
                      {s.student}
                    </button>
                  </td>
                  <td>{s.attempts}</td>
                  <td>{s.n_solved}</td>
                  <td style={{ textAlign: "right" }}>
                    <button className="btn sm danger" onClick={() => removeStudent(s.student)}>Delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function ClassRow({ cls: c, published, onChanged }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [sessionState, setSessionState] = useState(c.session_state || "running");
  const [showStudents, setShowStudents] = useState(false);
  const importRef = useRef(null);

  const badge = STATE_BADGE[c.state] || STATE_BADGE.active;

  async function changeSession(state) {
    const prev = sessionState;
    setSessionState(state);
    try {
      await api.setSession(c.id, state);
      flash(state === "paused" ? "Test paused" : state === "ended" ? "Test ended" : "Test running");
    } catch (e) {
      setSessionState(prev);
      flash("Failed: " + e.message);
    }
  }

  async function patch(fields, note) {
    setBusy(true);
    try {
      await api.updateClass(c.id, fields);
      if (note) flash(note);
      onChanged();
    } catch (e) {
      flash("Failed: " + e.message);
    } finally {
      setBusy(false);
    }
  }

  function flash(t) { setMsg(t); setTimeout(() => setMsg(null), 5000); }

  async function remove() {
    if (!confirm(`Delete "${c.title}"? The join code stops working and its attempt log is archived (insights for it disappear).`)) return;
    try {
      await api.deleteClass(c.id);
      onChanged();
    } catch (e) {
      alert("Delete failed: " + e.message);
    }
  }

  async function exportAssignment() {
    try {
      const data = await api.exportAssignment(c.id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `${c.id}-assignment.json`; a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert("Export failed: " + e.message);
    }
  }

  async function importAttemptsFile(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    flash("Importing…");
    try {
      const parsed = JSON.parse(await file.text());
      const res = await api.importAttempts(c.id, parsed);
      flash(`+${res.accepted ?? 0} imported (${res.duplicates ?? 0} dup)`);
      onChanged();
    } catch (e) {
      flash(e.message);
    }
  }

  if (editing) {
    return <EditClass cls={c} published={published} onDone={() => { setEditing(false); onChanged(); }} onCancel={() => setEditing(false)} />;
  }

  return (
    <div className="cls-item">
      <span className="cls-item-title">
        {c.title} <span className={"cls-badge " + badge.cls}>{badge.label}</span>
      </span>
      {c.mode !== "passcode" && <span className="cls-passphrase mono">{c.passphrase}</span>}
      <span className="cls-item-meta">
        {(c.set_titles || []).join(" · ") || "no sets"}
      </span>
      <span className="cls-item-meta">
        {c.mode} · {c.n_students} student{c.n_students === 1 ? "" : "s"} · {c.n_attempts} attempt{c.n_attempts === 1 ? "" : "s"}
        {c.active_until ? ` · closes ${new Date(c.active_until).toLocaleString()}` : ""}
        {c.active_from ? ` · opens ${new Date(c.active_from).toLocaleString()}` : ""}
      </span>

      {c.mode === "passcode" && Object.keys(c.passcodes || {}).length > 0 && (
        <div className="cls-passcode-grid" style={{ gridColumn: "1 / -1" }}>
          {Object.entries(c.passcodes).map(([name, code]) => (
            <div className="cls-passcode-row" key={name}>
              <span className="cls-passcode-name">{name}</span>
              <span className="cls-passphrase mono">{code}</span>
            </div>
          ))}
        </div>
      )}

      <div className="cls-row" style={{ gridColumn: "1 / -1", marginTop: 6, flexWrap: "wrap" }}>
        <SessionControls sessionState={sessionState} onChangeSession={changeSession} />
        <span className="cls-divider" />
        <button className="btn sm ghost" data-tip="Edit this classroom's title, mode, assigned sets, roster, or schedule." onClick={() => setEditing(true)}>Edit</button>
        <button className={"btn sm ghost" + (showStudents ? " on" : "")} data-tip="View the students who have entered this classroom; delete a student or their individual attempts." onClick={() => setShowStudents((v) => !v)}>
          {showStudents ? "Hide students" : "Students"}
        </button>
        {c.status === "archived" ? (
          <button className="btn sm ghost" disabled={busy}
                  data-tip="Reactivate — make the join code work again and resume accepting attempts. Nothing was deleted."
                  onClick={() => patch({ status: "active" }, "Reactivated")}>Reactivate</button>
        ) : (
          <button className="btn sm ghost" disabled={busy}
                  data-tip="Archive — stop accepting attempts and disable the join code, but KEEP the class and all its data/insights. Reversible."
                  onClick={() => patch({ status: "archived" }, "Archived")}>Archive</button>
        )}
        <button className="btn sm ghost" data-tip="Download a sealed assignment file (class + sets + sync address) to hand out as a backup." onClick={exportAssignment}>Assignment file</button>
        <button className="btn sm ghost" data-tip="Load a student's exported attempts file into this class (file-sync fallback)." onClick={() => importRef.current?.click()}>Import attempts</button>
        <input ref={importRef} type="file" accept="application/json" style={{ display: "none" }} onChange={importAttemptsFile} />
        <button className="btn sm danger" data-tip="Delete — permanently remove this class and its insights. The join code stops working. NOT reversible." onClick={remove}>Delete</button>
        {msg && <span className="cls-item-meta" style={{ gridColumn: "auto" }}>{msg}</span>}
      </div>

      {showStudents && <StudentsPanel cls={c} onChanged={onChanged} />}
    </div>
  );
}

// toISOString-friendly value for <input type="datetime-local"> (local time, no seconds/zone)
function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function fromLocalInput(v) {
  return v ? new Date(v).toISOString() : "";
}

function EditClass({ cls: c, published, onDone, onCancel }) {
  const [title, setTitle] = useState(c.title);
  const [mode, setMode] = useState(c.mode);
  const [setIds, setSetIds] = useState(c.set_ids || (c.set_id ? [c.set_id] : []));
  const [roster, setRoster] = useState((c.roster || []).join("\n"));
  const [from, setFrom] = useState(toLocalInput(c.active_from));
  const [until, setUntil] = useState(toLocalInput(c.active_until));
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  function toggleSet(id) {
    setSetIds((arr) => (arr.includes(id) ? arr.filter((x) => x !== id) : [...arr, id]));
  }

  async function save() {
    if (!title.trim() || setIds.length === 0) { setErr("Title and at least one set are required."); return; }
    setSaving(true);
    setErr(null);
    try {
      const names = roster.split("\n").map((s) => s.trim()).filter(Boolean);
      await api.updateClass(c.id, {
        title: title.trim(),
        mode,
        set_ids: setIds,
        roster: mode === "open" ? [] : names,
        active_from: fromLocalInput(from),
        active_until: fromLocalInput(until),
      });
      onDone();
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="cls-item cls-item-editing">
      <div className="field" style={{ gridColumn: "1 / -1" }}>
        <label>Title</label>
        <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} />
      </div>
      <div className="field" style={{ gridColumn: "1 / -1" }}>
        <label>Assigned sets</label>
        <SetPicker published={published} selected={setIds} onToggle={toggleSet} />
      </div>
      <div className="field" style={{ gridColumn: "1 / -1" }}>
        <label>Mode</label>
        <div className="cls-mode-toggle">
          {MODES.map((m) => (
            <button key={m.key} type="button" className={"cls-mode-btn" + (mode === m.key ? " on" : "")}
                    onClick={() => setMode(m.key)}>{m.label}</button>
          ))}
        </div>
        {mode === "passcode" && (
          <span className="hint-line" style={{ marginLeft: 0, display: "block", marginTop: 6 }}>
            New roster names get a fresh personal passcode on save; existing names keep theirs.
          </span>
        )}
      </div>
      {mode !== "open" && (
        <div className="field" style={{ gridColumn: "1 / -1" }}>
          <label>Roster <span className="hint-line">one name per line</span></label>
          <textarea className="input cls-roster" value={roster} onChange={(e) => setRoster(e.target.value)} />
        </div>
      )}
      <div className="field" style={{ gridColumn: "1 / -1" }}>
        <label>Schedule <span className="hint-line">— optional; leave blank to stay open until archived</span></label>
        <div className="cls-schedule">
          <label className="cls-schedule-field">
            <span>Opens</span>
            <input className="input" type="datetime-local" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="cls-schedule-field">
            <span>Closes</span>
            <input className="input" type="datetime-local" value={until} onChange={(e) => setUntil(e.target.value)} />
          </label>
        </div>
      </div>
      {err && <div className="cls-err" style={{ gridColumn: "1 / -1" }}>{err}</div>}
      <div className="cls-row" style={{ gridColumn: "1 / -1" }}>
        <button className="btn primary sm" onClick={save} disabled={saving}>{saving ? "Saving…" : "Save"}</button>
        <button className="btn sm ghost" onClick={onCancel} disabled={saving}>Cancel</button>
      </div>
    </div>
  );
}
