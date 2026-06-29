import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api.js";
import SqlEditor from "../components/SqlEditor.jsx";
import HintLadder from "../components/HintLadder.jsx";
import { Prose, Schema, DataTable } from "../components/bits.jsx";

const DIFF_LABEL = { easy: "Easy", medium: "Medium", hard: "Hard" };
const PAUSED_MSG = "The instructor paused the test. Hang tight — you'll be able to submit again when they resume.";
const ENDED_MSG = "The test has ended. You can finish the question you're on, but you can't switch questions or ask for hints.";
const CLOSED_GRACE_MSG = "This test just closed. You can finish the question you're on, but you can't switch questions or ask for hints.";

// full lock-screen copy keyed by reason (title, body). Shown to a fresh arrival once the test is
// no longer workable; students who were already in the session see a grace banner instead.
const LOCK_COPY = {
  scheduled: ["Not open yet", "The questions appear when your instructor opens this test."],
  archived: ["Class archived", "This class has been archived by your instructor and is no longer accepting attempts."],
  deleted: ["Class removed", "Your instructor removed this class. Your work so far is saved locally — you can still export it."],
  closed: ["Test closed", "This test is closed — the questions are no longer available."],
  ended: ["Test ended", "Your instructor ended this test — the questions are no longer available."],
  removed: ["Removed from class", "You're no longer on the roster for this classroom. Ask your instructor to add you back if this is a mistake. Your work so far is saved locally — you can still export it."],
};

export default function Student() {
  const [sets, setSets] = useState(null);
  const [setId, setSetId] = useState(null);
  const [set, setSet] = useState(null);
  const [active, setActive] = useState(null); // problem id
  const [solved, setSolved] = useState({}); // problemId -> true

  // per-problem working state, keyed by problem id, so switching problems keeps your query
  const [work, setWork] = useState({});

  // class-code sign-in: { class_id, student, title, set_id } or null
  const [cls, setCls] = useState(null);
  const [clsLoaded, setClsLoaded] = useState(false);
  const [joinCode, setJoinCode] = useState("");
  const [joinName, setJoinName] = useState("");
  const [joinErr, setJoinErr] = useState(null);
  const [joining, setJoining] = useState(false);

  // network connect (pull an assignment from a class server on the LAN)
  const [serverUrl, setServerUrl] = useState(() => localStorage.getItem("tutor.serverUrl") || "");
  const [connecting, setConnecting] = useState(false);

  // import-from-file path (when signed out)
  const [importErr, setImportErr] = useState(null);
  const [importing, setImporting] = useState(false);

  // sync / export status (signed-in joinbar)
  const [syncMsg, setSyncMsg] = useState(null);

  // live session control pushed by the instructor: "running" | "paused" | "ended"
  const [sessionState, setSessionState] = useState("running");
  // class scheduling window: "active" | "scheduled" | "closed" | "archived"
  const [clsState, setClsState] = useState("active");
  // Why this student is locked out, or null when they may work. The one nuance the instructor
  // asked for: a student who was *already in the live session* when the test ends/closes keeps
  // working (grace) so an in-progress answer isn't yanked away; but a fresh arrival — a reload,
  // a trip to Author and back, or a freshly opened app — gets the lock screen and can't start.
  // `liveSeenRef` records whether THIS mount ever saw a workable session, which is exactly what
  // separates "was here" from "just showed up": every one of those fresh-arrival paths remounts
  // the component, resetting the flag.
  const [lock, setLock] = useState(null); // null|"scheduled"|"archived"|"deleted"|"closed"|"ended"
  const liveSeenRef = useRef(false);
  const locked = lock !== null;
  // grace window: still workable, but the test is winding down — no switching, no hints
  const graceLock = !locked && (sessionState === "ended" || clsState === "closed");

  // restore sign-in from localStorage on load
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("tutor.class") || "null");
      if (saved?.class_id && saved?.student) setCls(saved);
    } catch {}
    setClsLoaded(true);
  }, []);

  // once signed in (and sets are loaded), auto-select the class's set
  useEffect(() => {
    if (cls && sets?.some((s) => s.id === cls.set_id)) setSetId(cls.set_id);
  }, [cls, sets]);

  // remember the active problem per set, so returning from Author lands on the same question
  useEffect(() => {
    if (setId && active) localStorage.setItem("tutor.active." + setId, active);
  }, [setId, active]);

  async function joinClass() {
    // name is optional: personal-passcode classes resolve the student from the code alone
    if (!joinCode.trim()) return;
    setJoining(true);
    setJoinErr(null);
    try {
      const res = await api.join(joinCode.trim(), joinName.trim());
      setCls(res);
      localStorage.setItem("tutor.class", JSON.stringify(res));
      setJoinCode("");
      setJoinName("");
    } catch (e) {
      setJoinErr(e.message);
    } finally {
      setJoining(false);
    }
  }

  // network connect: pull the assignment from a class server by URL + class code, then join.
  // proxied through our own backend, so no cross-origin call from the browser.
  async function connectServer() {
    if (!serverUrl.trim() || !joinCode.trim()) return;
    setConnecting(true);
    setJoinErr(null);
    try {
      const res = await api.connectToServer(serverUrl.trim(), joinCode.trim(), joinName.trim());
      setCls(res);
      localStorage.setItem("tutor.class", JSON.stringify(res));
      localStorage.setItem("tutor.serverUrl", serverUrl.trim());
      setJoinCode("");
      setJoinName("");
    } catch (e) {
      setJoinErr(e.message);
    } finally {
      setConnecting(false);
    }
  }

  // one action for the sign-in form: if a class-server address is filled in, pull the
  // assignment from it over the LAN; otherwise join a class already on this device.
  function joinOrConnect() {
    if (serverUrl.trim()) connectServer();
    else joinClass();
  }

  function leaveClass() {
    setCls(null);
    setLock(null);
    liveSeenRef.current = false;
    localStorage.removeItem("tutor.class");
  }

  function selectProblem(pid) {
    setActive(pid);
  }

  // poll class liveness + live session control (pause/end) every 8s while signed in
  useEffect(() => {
    if (!cls) return;
    let alive = true;
    const check = () => {
      api.classStatus(cls.class_id, cls.student)
        .then((s) => {
          if (!alive) return;
          const exists = s.exists;
          const state = exists ? s.state : "deleted";
          const session = s.session_state || "running";
          setClsState(state);
          setSessionState(session);

          // Mark that we were present during a workable moment. Once true for this mount, the
          // student keeps their grace even as later polls report the test ended/closed.
          const ended = session === "ended";
          if (exists && state === "active" && !ended) liveSeenRef.current = true;

          // Removed from the roster: a hard lock with no grace — an off-roster student stops
          // working immediately, mid-session or not. Checked before the softer states below.
          if (exists && s.removed) setLock("removed");
          else if (!exists) setLock("deleted");
          else if (state === "scheduled" || state === "archived") setLock(state);
          else if (state === "closed" || ended) {
            // grace only for students already in the session; fresh arrivals get locked out
            setLock(liveSeenRef.current ? null : (ended ? "ended" : "closed"));
          } else {
            setLock(null);
          }
        })
        .catch(() => {});
    };
    check();
    const id = setInterval(check, 8000);
    return () => { alive = false; clearInterval(id); };
  }, [cls]);

  async function importAssignmentFile(e) {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow re-selecting the same file later
    if (!file) return;
    if (!joinName.trim()) {
      setImportErr("Enter your name first, then choose the file.");
      return;
    }
    setImporting(true);
    setImportErr(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const imported = await api.importAssignment(parsed);
      const res = await api.join(imported.passphrase, joinName.trim());
      setCls(res);
      localStorage.setItem("tutor.class", JSON.stringify(res));
      setJoinName("");
    } catch (e) {
      setImportErr(e.message);
    } finally {
      setImporting(false);
    }
  }

  async function doSync() {
    if (!cls) return;
    setSyncMsg("Syncing…");
    try {
      const res = await api.syncAttempts(cls.class_id);
      setSyncMsg(`synced ${res.accepted}${res.duplicates ? ` (${res.duplicates} dup)` : ""}`);
    } catch (e) {
      const msg = /no instructor_url/i.test(e.message)
        ? "this class syncs by file — use Export attempts"
        : e.message;
      setSyncMsg(msg);
    }
    setTimeout(() => setSyncMsg(null), 5000);
  }

  async function doExportAttempts() {
    if (!cls) return;
    try {
      const data = await api.attemptsExport(cls.class_id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${cls.class_id}-attempts.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setSyncMsg(e.message);
      setTimeout(() => setSyncMsg(null), 5000);
    }
  }

  // sets are scoped to the signed-in class; without one, students see nothing
  useEffect(() => {
    if (!clsLoaded) return;
    if (!cls) {
      setSets([]);
      return;
    }
    api.studentSets(cls.class_id).then((s) => {
      setSets(s);
      if (s.length) setSetId(s[0].id);
    }).catch((e) => {
      // a stored class that no longer exists (deleted/renamed) 404s here — without this catch
      // `sets` stays null and the page spins forever. Drop the stale sign-in and show the
      // join screen; any other error still resolves to an empty list rather than a dead spinner.
      if (/no class|join a class/i.test(e.message)) leaveClass();
      else setSets([]);
    });
  }, [cls, clsLoaded]);

  useEffect(() => {
    if (!setId) return;
    api.studentSet(setId, cls?.class_id).then((s) => {
      setSet(s);
      // restore the problem the student was last on for this set (across Author↔Practice switches)
      const saved = localStorage.getItem("tutor.active." + setId);
      const pick = saved && s.problems.some((p) => p.id === saved) ? saved : s.problems[0]?.id ?? null;
      setActive(pick);
    }).catch((e) => {
      // 403 ("join a class to access this set") or a stale/deleted class id — treat as signed-out
      if (/join a class|no class/i.test(e.message)) {
        leaveClass();
      }
    });
  }, [setId, cls]);

  const problem = useMemo(
    () => set?.problems.find((p) => p.id === active) ?? null,
    [set, active]
  );

  const w = work[active] ?? { sql: "", result: null, hints: [], hintLoading: false };
  const setW = (patch) =>
    setWork((prev) => ({ ...prev, [active]: { ...(prev[active] ?? { sql: "", result: null, hints: [] }), ...patch } }));

  // schema hint for CodeMirror autocomplete: map table -> columns (best-effort parse)
  const cmSchema = useMemo(() => {
    if (!problem?.schema) return {};
    const out = {};
    const re = /CREATE TABLE\s+(\w+)\s*\(([\s\S]*?)\);/gi;
    let m;
    while ((m = re.exec(problem.schema))) {
      out[m[1]] = m[2]
        .split("\n")
        .map((l) => l.trim().split(/\s+/)[0])
        .filter((c) => c && /^\w+$/.test(c) && c.toUpperCase() !== "PRIMARY");
    }
    return out;
  }, [problem]);

  // scroll the verdict into view once a result lands — keeps the answer in sight after grading
  const resultRef = useRef(null);
  useEffect(() => {
    if (w.result && !w.result.error) resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [w.result]);

  async function run() {
    if (!w.sql.trim()) return;
    if (sessionState === "paused") { setW({ result: { error: PAUSED_MSG } }); return; }
    setW({ running: true });
    try {
      const attrib = cls ? { class_id: cls.class_id, student: cls.student } : undefined;
      const res = await api.grade(setId, active, w.sql, attrib);
      setW({ result: res, running: false, hints: [] });
      if (res.correct) setSolved((s) => ({ ...s, [active]: true }));
    } catch (e) {
      setW({ running: false, result: { error: e.message }, hints: [] });
    }
  }

  async function askHint(level) {
    if (sessionState !== "running" || graceLock) return; // hints are closed while paused/ended/closed
    // The ladder is now error-class-adaptive: the backend decides which PRIMITIVE sits at this
    // level (from the grade's family). Deterministic rungs (`diff`/`db_error`) come back with
    // hint:null and are rendered client-side from w.result.diff; model rungs carry text. Either
    // way the request is logged server-side for Insights.
    setW({ hintLoading: true });
    try {
      const attrib = cls ? { class_id: cls.class_id, student: cls.student } : undefined;
      const res = await api.hint(setId, active, w.sql, level, attrib);
      if (res.correct) {
        setW({ hintLoading: false });
        return;
      }
      setW({ hintLoading: false, hints: [...w.hints, { level, text: res.hint, primitive: res.primitive }] });
    } catch (e) {
      setW({ hintLoading: false });
      alert("Hint failed: " + e.message);
    }
  }

  // a one-line category of the mismatch — no rows, no counts (those are earned at L3)
  function verdictCategory(result) {
    const d = result.diff;
    if (problem?.kind === "state") {
      if (result.correct) return "Correct — your database ends in the right state on every test database.";
      if (d?.sql_error) return "Your statement didn't run.";
      return "Your database doesn't end up in the right state yet.";
    }
    if (result.correct) return "Correct on every database.";
    if (d?.sql_error) return "Your query didn't run.";
    if (d?.ordering_only) return "Right rows — wrong order.";
    // a pinned column-name mismatch (instructor enforced names): say so plainly — it's a
    // naming issue, not a values giveaway, so it's surfaced in the verdict rather than via hints
    if (d?.required_columns_missing?.length) return "Right results — but the column names don't match what's required.";
    return "Your result doesn't match yet.";
  }

  if (!sets || !clsLoaded) return <div className="main"><div className="empty"><span className="spin" /></div></div>;

  // signed out: nothing to list until the student joins a class (or imports an assignment file)
  if (!cls)
    return (
      <div className="main">
        <div className="empty-pane">
          <div className="empty" style={{ maxWidth: 380 }}>
            <div className="big">Join your class</div>
            <div className="field" style={{ marginTop: 16, textAlign: "left" }}>
              <label>Class code <span className="hint-line">or your personal passcode</span></label>
              <input
                className="input"
                placeholder="e.g. maple-river-stone"
                value={joinCode}
                onChange={(e) => setJoinCode(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && joinOrConnect()}
              />
            </div>
            <div className="field" style={{ textAlign: "left" }}>
              <label>Your name <span className="hint-line">— leave blank if you were given a personal passcode</span></label>
              <input
                className="input"
                placeholder="Your name"
                value={joinName}
                onChange={(e) => setJoinName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && joinOrConnect()}
              />
            </div>
            <div className="field" style={{ textAlign: "left" }}>
              <label>Class server address <span className="hint-line">— optional; on the same Wi-Fi as your instructor</span></label>
              <input
                className="input"
                placeholder="http://192.168.1.5:8077"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && joinOrConnect()}
              />
            </div>
            {joinErr && <div className="banner err">{joinErr}</div>}
            <button className="btn primary" onClick={joinOrConnect} disabled={joining || connecting || !joinCode.trim()}>
              {serverUrl.trim()
                ? (connecting ? "Connecting…" : "Connect")
                : (joining ? "Joining…" : "Join")}
            </button>

            <div className="section-label" style={{ marginTop: 28 }}>
              <span className="eyebrow">…or load an assignment file</span>
            </div>
            <div className="field" style={{ textAlign: "left" }}>
              <input type="file" accept=".json" className="input" onChange={importAssignmentFile} disabled={importing} />
              <span className="hint-line" style={{ marginLeft: 0, display: "block", marginTop: 6 }}>
                Enter your name above, then choose the assignment file.
              </span>
            </div>
            {importErr && <div className="banner err">{importErr}</div>}
          </div>
        </div>
      </div>
    );

  if (!sets.length)
    return (
      <div className="main">
        <div className="empty">
          <div className="big">No problem sets yet</div>
          <div>Switch to <b>Author</b> to write one, then publish it for practice.</div>
        </div>
      </div>
    );

  return (
    <div className="main">
      <aside className="rail">
        <div className="rail-head">
          {sets.length > 1 ? (
            <select
              className="rail-setswitch"
              value={setId ?? ""}
              onChange={(e) => setSetId(e.target.value)}
              data-tip="Switch to another assigned set"
              aria-label="Switch set"
            >
              {sets.map((s) => (
                <option key={s.id} value={s.id}>{s.title}</option>
              ))}
            </select>
          ) : (
            <h2>{set?.title ?? "…"}</h2>
          )}
          <div className="rail-sub">
            {set ? `${set.problems.length} problems` : ""}
            {sets.length > 1 ? ` · set ${sets.findIndex((s) => s.id === setId) + 1} of ${sets.length}` : ""}
          </div>
        </div>
        <ul className="plist">
          {!locked && set?.problems.map((p, i) => (
            <li key={p.id}>
              <button
                className={"pitem" + (p.id === active ? " on" : "") + (solved[p.id] ? " solved" : "")}
                onClick={() => selectProblem(p.id)}
                disabled={graceLock && p.id !== active}
                data-tip={graceLock && p.id !== active ? "Locked — the test has ended" : undefined}
              >
                <span className="numeral">{String(i + 1).padStart(2, "0")}</span>
                <span className="pitem-title">{p.title}</span>
                <span className="pitem-meta" style={{ gridColumn: 2 }}>{DIFF_LABEL[p.difficulty]}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="work">
        <div className="cls-joinbar">
          <span className="cls-joinbar-status">
            Signed in as <b>{cls.student}</b> · {cls.title}
          </span>
          <button className="btn sm" onClick={doSync}>Sync</button>
          <button className="btn sm" onClick={doExportAttempts}>Export attempts</button>
          {syncMsg && <span className="cls-joinbar-msg">{syncMsg}</span>}
          <span className="spacer" />
          <button className="cls-leave" onClick={leaveClass} data-tip="Leave class" aria-label="Leave class">×</button>
        </div>

        {/* grace banners — only for students who were already in the session when it wound down */}
        {!locked && sessionState === "paused" && (
          <div className="banner info" style={{ margin: "0 0 14px" }}>⏸ {PAUSED_MSG}</div>
        )}
        {!locked && sessionState === "ended" && (
          <div className="banner info" style={{ margin: "0 0 14px" }}>🏁 {ENDED_MSG}</div>
        )}
        {!locked && sessionState !== "ended" && clsState === "closed" && (
          <div className="banner info" style={{ margin: "0 0 14px" }}>🏁 {CLOSED_GRACE_MSG}</div>
        )}

        {locked && (
          <div className="empty" style={{ padding: 40 }}>
            <div className="big">{LOCK_COPY[lock][0]}</div>
            <div>{LOCK_COPY[lock][1]}</div>
            {(lock === "deleted" || lock === "closed" || lock === "ended") && (
              <div style={{ marginTop: 18 }}>
                <button className="btn sm" onClick={doExportAttempts}>Export attempts</button>
                <button className="btn sm ghost" style={{ marginLeft: 6 }} onClick={leaveClass}>Leave class</button>
              </div>
            )}
          </div>
        )}

        {!locked && problem && (
          <div className="work-wrap">
            <div className="prompt-block">
              <div className="tag" style={{ marginBottom: 8 }}>
                <span className={"dot " + problem.difficulty} /> {DIFF_LABEL[problem.difficulty]}
              </div>
              <h1>{problem.title}</h1>
              <p className="prompt-text"><Prose text={problem.prompt} /></p>
            </div>

            {problem.schema?.trim() ? (
              <>
                <div className="section-label"><span className="eyebrow">The tables</span></div>
                <Schema ddl={problem.schema} />
              </>
            ) : (
              <div className="run-hint" style={{ marginTop: 4 }}>This problem starts from an empty database.</div>
            )}

            <div className="section-label" style={{ marginTop: 24 }}>
              <span className="eyebrow">Your query</span>
            </div>
            <SqlEditor value={w.sql} onChange={(v) => setW({ sql: v })} onSubmit={run} schema={cmSchema} />
            <div className="editor-bar">
              <button className="btn primary" onClick={run}
                disabled={w.running || !w.sql.trim() || sessionState === "paused"}
                data-tip={sessionState === "paused" ? "Paused by your instructor" : undefined}>
                {w.running ? <span className="thinking" style={{ color: "#fff" }}><span className="spin" style={{ borderTopColor: "#fff" }} /> Running</span> : "Run & check"}
              </button>
              <span className="run-hint"><span className="kbd">⌘↵</span> to run</span>
            </div>

            {w.result?.error && <div className="banner err">{w.result.error}</div>}

            {w.result && !w.result.error && (
              <div ref={resultRef}>
                <div className={"verdict " + (w.result.correct ? "correct" : "wrong")}>
                  <span className="verdict-mark">{w.result.correct ? "✓" : "✕"}</span>
                  <div>
                    <div className="verdict-text">
                      {verdictCategory(w.result)}
                    </div>
                    <div className="verdict-sub">
                      passed {w.result.n_passed} of {w.result.n_seeds} databases
                    </div>
                    {!w.result.correct && w.result.diff?.required_columns_missing?.length > 0 && (
                      <ul className="verdict-colnotes" style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                        {w.result.diff.header_notes?.map((n, i) => (
                          <li key={i} className="run-hint" style={{ marginBottom: 2 }}>{n}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>

                {w.result.student_result?.kind === "state" && !w.result.student_result.error && (
                  <div className="cat-yourresult">
                    {Object.entries(w.result.student_result.tables).map(([name, t]) => (
                      <div key={name}>
                        <div className="section-label">
                          <span className="eyebrow">
                            {name} · after your statement · test database #1 · {t.n_rows} rows
                          </span>
                        </div>
                        <DataTable cols={t.cols} rows={t.rows} />
                      </div>
                    ))}
                  </div>
                )}

                {w.result.student_result?.kind === "state" && w.result.student_result.error && (
                  <div className="banner err">{w.result.student_result.error}</div>
                )}

                {w.result.student_result && !w.result.student_result.kind && !w.result.student_result.error && (
                  <div className="cat-yourresult">
                    <div className="section-label">
                      <span className="eyebrow">
                        Your result · test database #1 · {w.result.student_result.n_rows} rows
                      </span>
                    </div>
                    <DataTable cols={w.result.student_result.cols} rows={w.result.student_result.rows} />
                  </div>
                )}

                {!w.result.correct && (
                  <HintLadder
                    hints={w.hints}
                    diff={w.result.diff}
                    resultCols={w.result.student_result?.cols}
                    rungPlan={w.result.rung_plan}
                    loading={w.hintLoading}
                    onRequest={askHint}
                    locked={sessionState !== "running" || graceLock}
                    lockedReason={sessionState === "paused" ? "Paused — hints are closed." : "The test has ended — hints are closed."}
                  />
                )}
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
