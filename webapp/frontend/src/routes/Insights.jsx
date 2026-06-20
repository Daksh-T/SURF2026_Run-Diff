import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../lib/api.js";
import { HBar, StackedHBar, StackedLegend, MiniBars } from "../components/charts.jsx";
import SessionControls from "../components/SessionControls.jsx";

// hint level -> the clause-naming language used at that rung (harness._LEVEL_RULES)
const HINT_LEVEL_LABEL = {
  1: "a conceptual nudge",
  2: "naming the clause",
  3: "a query skeleton",
};

const DIFF_LABEL = { easy: "Easy", medium: "Medium", hard: "Hard" };

const HINT_SEG_CLASSES = {
  1: "stacked-seg-l1",
  2: "stacked-seg-l2",
  3: "stacked-seg-l3",
};

function fmtPct(x) {
  return x == null ? "—" : `${Math.round(x * 100)}%`;
}

function fmtNum(x, digits = 1) {
  return x == null ? "—" : x.toFixed(digits);
}

// hint_requests keys may be string or number — normalize to a {1,2,3} -> count map
function hintCounts(hint_requests) {
  const hr = hint_requests || {};
  return {
    1: hr[1] ?? hr["1"] ?? 0,
    2: hr[2] ?? hr["2"] ?? 0,
    3: hr[3] ?? hr["3"] ?? 0,
  };
}

function numberedTitle(p, i) {
  return `${String(i + 1).padStart(2, "0")}. ${p.title}`;
}

// per-problem: for each hint level, who used it and how many times — drawn from each
// student row's `hint_levels` map ({"1": n, ...}). Returns { 1: [{student, n}], 2: [...], 3: [...] }.
function hintLevelStudents(students) {
  const out = { 1: [], 2: [], 3: [] };
  for (const s of students || []) {
    const hl = s.hint_levels || {};
    for (const lvl of [1, 2, 3]) {
      const n = hl[lvl] ?? hl[String(lvl)] ?? 0;
      if (n > 0) out[lvl].push({ student: s.student, n });
    }
  }
  for (const lvl of [1, 2, 3]) out[lvl].sort((a, b) => b.n - a.n);
  return out;
}

// hover text for a hint-level bar: "L2 — naming the clause\nAda ×2 · Alan ×1"
function levelTooltip(level, users) {
  const head = `L${level} — ${HINT_LEVEL_LABEL[level]}`;
  if (!users || users.length === 0) return `${head}\nNo requests yet.`;
  const lines = users.map((u) => `${u.student} ×${u.n}`).join(" · ");
  return `${head}\n${lines}`;
}

// truncate a title for the bar-chart label column without ever splitting mid-word abruptly
function truncTitle(s, max = 34) {
  if (s.length <= max) return s;
  return s.slice(0, max - 1).trimEnd() + "…";
}

// ---- live view helpers ---------------------------------------------------

// "Xs ago" / "Xm ago" relative to a server-provided `now` ISO timestamp (avoids client
// clock-skew bugs — the server tells us what "now" was when it computed the snapshot).
function relTime(ts, nowTs) {
  if (!ts || !nowTs) return "—";
  const dt = (new Date(nowTs) - new Date(ts)) / 1000;
  if (dt < 0) return "just now";
  if (dt < 60) return `${Math.floor(dt)}s ago`;
  if (dt < 3600) return `${Math.floor(dt / 60)}m ago`;
  return `${Math.floor(dt / 3600)}h ago`;
}

// short clock time, e.g. "2:41 pm"
function clockTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).toLowerCase();
}

const SESSION_OPTIONS = [
  { key: "all", label: "All time" },
  { key: "today", label: "Today" },
  { key: "hour", label: "Last hour" },
  { key: "now", label: "Since now" },
];

// resolve a session-scope key (+ a "since now" anchor timestamp) into the ISO `since`
// value sent to the API, or null for "all time"
function resolveSince(scope, nowAnchor) {
  switch (scope) {
    case "today": {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.toISOString();
    }
    case "hour":
      return new Date(Date.now() - 3600_000).toISOString();
    case "now":
      return nowAnchor;
    default:
      return null;
  }
}

// build a compact one-liner for a `recent` event
function recentLine(ev, problemTitles) {
  const idx = problemTitles.idxById[ev.problem_id];
  const num = idx != null ? String(idx + 1).padStart(2, "0") : ev.problem_id;
  const when = clockTime(ev.ts);
  if (ev.kind === "grade") {
    if (ev.correct) return `${when} — ${ev.student} solved ${num}`;
    const cat = ev.category ? ` (${ev.category.replace(/_/g, " ")})` : "";
    return `${when} — ${ev.student}'s run didn't pass${cat} on ${num}`;
  }
  return `${when} — ${ev.student} asked for an L${ev.hint_level ?? "?"} hint on ${num}`;
}

const STATUS_LABEL = { solved: "Solved", hinted: "Used a hint", trying: "Trying" };

function cellTooltip(cell, nowTs) {
  if (!cell) return "No activity";
  const parts = [`${cell.n_grades} grade${cell.n_grades === 1 ? "" : "s"}`];
  if (cell.n_hints) parts.push(`${cell.n_hints} hint${cell.n_hints === 1 ? "" : "s"} (max L${cell.max_hint_level})`);
  parts.push(`last activity ${relTime(cell.last_ts, nowTs)}`);
  return `${STATUS_LABEL[cell.status] || ""} — ${parts.join(" · ")}`;
}

// "pred 1.3 · actual 1.6" with a small check when the two are within 0.5 of each other
function HintLevelCompare({ predicted, actual }) {
  if (predicted == null && actual == null) return <span>—</span>;
  if (predicted == null) return <span>actual {fmtNum(actual)}</span>;
  if (actual == null) return <span>pred {fmtNum(predicted)}</span>;
  const close = Math.abs(predicted - actual) <= 0.5;
  return (
    <span>
      pred {fmtNum(predicted)} · actual {fmtNum(actual)}
      {close && <span className="ins-accurate" data-tip="within 0.5 of the actual class result"> ✓</span>}
    </span>
  );
}

export default function Insights() {
  const [classes, setClasses] = useState(null);
  const [classId, setClassId] = useState(null);
  const [setFilter, setSetFilter] = useState(null); // null = all of the class's sets
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  // null = Overview; "live" = Live mode; otherwise a problem_id
  const [activeProblem, setActiveProblem] = useState(null);

  // when set, a single student's full progression takes over the work pane (item: drill into
  // a student from the overview). Cleared on class/set change.
  const [activeStudent, setActiveStudent] = useState(null);
  const [studentDetail, setStudentDetail] = useState(null);
  const [studentLoading, setStudentLoading] = useState(false);
  const [studentErr, setStudentErr] = useState(null);

  // ---- live mode state ----
  const [liveData, setLiveData] = useState(null);
  const [liveErr, setLiveErr] = useState(null);
  const [sessionScope, setSessionScope] = useState("all");
  const [sinceNowAnchor, setSinceNowAnchor] = useState(null); // ISO, set when "Since now" is clicked
  // live TEST control (server-backed): "running" | "paused" | "ended"
  const [sessionState, setSessionState] = useState("running");
  // class scheduling state (active | scheduled | closed | archived | deleted)
  const [clsState, setClsState] = useState("active");
  const [lastUpdated, setLastUpdated] = useState(null); // Date, for "updated Xs ago" ticking
  const [, forceTick] = useState(0); // re-render every second to refresh "Xs ago" labels

  useEffect(() => {
    api.listClasses().then((cs) => {
      setClasses(cs);
      if (cs.length) setClassId(cs[0].id);
    }).catch((e) => setErr(e.message));
  }, []);

  useEffect(() => {
    if (!classId) return;
    setLoading(true);
    setErr(null);
    setActiveProblem(null);
    setActiveStudent(null);
    setLiveData(null);
    api.classAnalytics(classId, setFilter)
      .then((d) => setData(d))
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [classId, setFilter]);

  // fetch one student's full progression when their name is opened
  useEffect(() => {
    if (!activeStudent || !classId) { setStudentDetail(null); return; }
    setStudentLoading(true);
    setStudentErr(null);
    api.classStudent(classId, activeStudent, setFilter)
      .then((d) => setStudentDetail(d))
      .catch((e) => setStudentErr(e.message))
      .finally(() => setStudentLoading(false));
  }, [activeStudent, classId, setFilter]);

  // ---- live polling: every 4s while the Live chip is selected, the tab is visible,
  // and not paused. Stops entirely when another chip is selected. ----
  useEffect(() => {
    if (activeProblem !== "live" || !classId) return;

    const since = resolveSince(sessionScope, sinceNowAnchor);

    const poll = () => {
      if (document.hidden) return;
      api.classLive(classId, since, setFilter)
        .then((d) => {
          setLiveData(d);
          setLiveErr(null);
          setLastUpdated(new Date());
        })
        .catch((e) => setLiveErr(e.message));
    };

    poll();
    const interval = setInterval(poll, 4000);
    const onVis = () => { if (!document.hidden) poll(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [activeProblem, classId, sessionScope, sinceNowAnchor, setFilter]);

  // track the class's session + scheduling state, and pick the default landing tab:
  // Live when the test is open or merely paused; Overview when it's closed/ended/scheduled.
  useEffect(() => {
    if (!classId) return;
    let alive = true;
    api.classStatus(classId)
      .then((s) => {
        if (!alive) return;
        const ss = s.session_state || "running";
        const cs = s.state || "active";
        setSessionState(ss);
        setClsState(cs);
        const goLive = cs === "active" && (ss === "running" || ss === "paused");
        setActiveProblem(goLive ? "live" : null);
      })
      .catch(() => {});
    return () => { alive = false; };
  }, [classId]);

  async function changeSession(state) {
    const prev = sessionState;
    setSessionState(state); // optimistic
    try {
      await api.setSession(classId, state);
    } catch (e) {
      setSessionState(prev);
      setErr(e.message);
    }
  }

  // tick once a second while in live mode so "updated Xs ago" stays fresh
  useEffect(() => {
    if (activeProblem !== "live") return;
    const t = setInterval(() => forceTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [activeProblem]);

  // entering Live mode: reset the "since now" anchor only when explicitly chosen
  function selectSessionScope(key) {
    if (key === "now") setSinceNowAnchor(new Date().toISOString());
    setSessionScope(key);
  }

  // client-side actionable insight: lowest solve-rate problem + most-requested hint level
  const insight = useMemo(() => {
    if (!data || !data.problems.length) return null;
    const withRate = data.problems.filter((p) => p.solve_rate != null);
    if (!withRate.length) return null;
    const worst = withRate.reduce((a, b) => (b.solve_rate < a.solve_rate ? b : a));

    const levelTotals = {};
    for (const p of data.problems) {
      for (const [lvl, n] of Object.entries(p.hint_requests || {})) {
        levelTotals[lvl] = (levelTotals[lvl] || 0) + n;
      }
    }
    const levels = Object.entries(levelTotals);
    if (!levels.length) {
      return `Most students get stuck on "${worst.title}" (${fmtPct(worst.solve_rate)} solve rate).`;
    }
    const [topLevel] = levels.reduce((a, b) => (b[1] > a[1] ? b : a));
    const label = HINT_LEVEL_LABEL[Number(topLevel)] || `level ${topLevel} hints`;
    return `Most students get stuck on "${worst.title}" (${fmtPct(worst.solve_rate)} solve rate); `
      + `hints most often needed: ${label}.`;
  }, [data]);

  // worst problem by solve_rate (for the danger-colored bar). Only flag when there's
  // a comparison to make AND the class is actually struggling — a lone problem, or a
  // worst-problem everyone solves, shouldn't glow red.
  const worstProblemId = useMemo(() => {
    if (!data || data.problems.length < 2) return null;
    const withRate = data.problems.filter((p) => p.solve_rate != null);
    if (!withRate.length) return null;
    const worst = withRate.reduce((a, b) => (b.solve_rate < a.solve_rate ? b : a));
    return worst.solve_rate < 1 ? worst.problem_id : null;
  }, [data]);

  // derived summary stats: attempts per student + average hint requests per question
  const extraStats = useMemo(() => {
    if (!data) return null;
    let totalHints = 0;
    for (const p of data.problems) {
      const hc = hintCounts(p.hint_requests);
      totalHints += hc[1] + hc[2] + hc[3];
    }
    const nStudents = data.summary.n_students || 0;
    const nProblems = data.summary.n_problems || data.problems.length || 0;
    return {
      totalHints,
      attemptsPerStudent: nStudents ? data.summary.n_attempts / nStudents : null,
      hintsPerQuestion: nProblems ? totalHints / nProblems : null,
      nProblems,
    };
  }, [data]);

  if (classes === null) {
    return (
      <div className="main">
        <div className="empty-pane"><span className="spin" /></div>
      </div>
    );
  }

  if (!classes.length) {
    return (
      <div className="main">
        <div className="empty-pane">
          <div className="empty">
            <div className="big">No classes yet</div>
            <div>Create a class on the <b>Author</b> page to start collecting attempts.</div>
          </div>
        </div>
      </div>
    );
  }

  const problems = data?.problems ?? [];
  const activeIdx = activeProblem != null ? problems.findIndex((p) => p.problem_id === activeProblem) : -1;
  const active = activeIdx >= 0 ? problems[activeIdx] : null;

  return (
    <div className="main">
      <aside className="rail">
        <div className="rail-head">
          <h2>Insights</h2>
          <div className="rail-sub">{classes.length} class{classes.length === 1 ? "" : "es"}</div>
        </div>
        <ul className="plist">
          {classes.map((c) => (
            <li key={c.id}>
              <button
                className={"pitem" + (c.id === classId ? " on" : "")}
                onClick={() => { setClassId(c.id); setSetFilter(null); }}
              >
                <span className="pitem-title" style={{ gridColumn: "1 / -1" }}>{c.title}</span>
                <span className="pitem-meta" style={{ gridColumn: "1 / -1" }}>
                  {c.n_students} student{c.n_students === 1 ? "" : "s"} · {c.n_attempts} attempt{c.n_attempts === 1 ? "" : "s"}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="work">
        <div className="work-wrap">
          {err && <div className="banner err">{err}</div>}
          {loading && <div className="empty"><span className="spin" /></div>}

          {data && !loading && (
            <>
              <h1>{data.title}</h1>

              {/* per-set selector when the class assigns more than one set; a plain label when
                  it assigns exactly one (so the set's name is always visible) */}
              {data.sets && data.sets.length > 1 ? (
                <div className="ins-setpick">
                  <span className="ins-setpick-label">Set</span>
                  <div className="live-session-switch">
                    <button
                      className={"live-session-btn" + (setFilter == null ? " on" : "")}
                      onClick={() => setSetFilter(null)}
                    >All sets</button>
                    {data.sets.map((s) => (
                      <button
                        key={s.id}
                        className={"live-session-btn" + (setFilter === s.id ? " on" : "")}
                        onClick={() => setSetFilter(s.id)}
                      >{s.title}</button>
                    ))}
                  </div>
                </div>
              ) : data.sets && data.sets.length === 1 ? (
                <div className="ins-setpick">
                  <span className="ins-setpick-label">Set</span>
                  <span className="ins-setpick-one">{data.sets[0].title}</span>
                </div>
              ) : null}

              {/* chip strip: Live + Overview + one chip per problem */}
              <div className="ins-chiprow">
                <button
                  className={"ins-chip ins-chip-live" + (activeProblem === "live" && !activeStudent ? " on" : "")}
                  onClick={() => { setActiveStudent(null); setActiveProblem("live"); }}
                >
                  <span className={"live-dot" + (clsState !== "active" || sessionState === "ended" ? " live-dot-red" : "")} /> Live
                </button>
                <button
                  className={"ins-chip" + (activeProblem == null && !activeStudent ? " on" : "")}
                  onClick={() => { setActiveStudent(null); setActiveProblem(null); }}
                >
                  Overview
                </button>
                {problems.map((p, i) => (
                  <button
                    key={p.problem_id}
                    className={"ins-chip" + (activeProblem === p.problem_id && !activeStudent ? " on" : "")}
                    onClick={() => { setActiveStudent(null); setActiveProblem(p.problem_id); }}
                  >
                    <span className="numeral">{String(i + 1).padStart(2, "0")}</span> {p.title}
                  </button>
                ))}
              </div>

              {activeStudent ? (
                <StudentDetail
                  student={activeStudent}
                  detail={studentDetail}
                  loading={studentLoading}
                  err={studentErr}
                  problems={problems}
                  onBack={() => setActiveStudent(null)}
                  onOpenProblem={(pid) => { setActiveStudent(null); setActiveProblem(pid); }}
                />
              ) : activeProblem === "live" ? (
                <LiveView
                  liveData={liveData}
                  liveErr={liveErr}
                  problems={problems}
                  sessionScope={sessionScope}
                  onSessionScope={selectSessionScope}
                  sessionState={sessionState}
                  onChangeSession={changeSession}
                  lastUpdated={lastUpdated}
                  onJumpToProblem={(pid) => setActiveProblem(pid)}
                  onOpenStudent={(name) => setActiveStudent(name)}
                />
              ) : activeProblem == null ? (
                <>
                  {insight && <div className="banner info ins-insight">{insight}</div>}

                  <div className="section-label"><span className="eyebrow">Summary</span></div>
                  <div className="ins-statstrip">
                    <div className="ins-statbox">
                      <div className="ins-statval">{data.summary.n_students}</div>
                      <div className="ins-statlabel">students</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{data.summary.n_attempts}</div>
                      <div className="ins-statlabel">attempts</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{fmtNum(extraStats?.attemptsPerStudent, 1)}</div>
                      <div className="ins-statlabel">attempts / student</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{fmtPct(data.summary.overall_solve_rate)}</div>
                      <div className="ins-statlabel">solve rate</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{fmtNum(extraStats?.hintsPerQuestion, 1)}</div>
                      <div className="ins-statlabel">avg hints / question</div>
                    </div>
                  </div>

                  {problems.length === 0 ? (
                    <div className="empty" style={{ padding: 24 }}>No attempts logged yet for this class.</div>
                  ) : (
                    <>
                      <div className="section-label" style={{ marginTop: 24 }}>
                        <span className="eyebrow">Solve rate by problem</span>
                      </div>
                      <div className="hbar-chart">
                        {problems.map((p, i) => (
                          <HBar
                            key={p.problem_id}
                            label={truncTitle(numberedTitle(p, i))}
                            ratio={p.solve_rate}
                            valueLabel={
                              p.solve_rate == null
                                ? "—"
                                : `${fmtPct(p.solve_rate)} · ${p.students_solved}/${p.students_attempted}`
                            }
                            danger={p.problem_id === worstProblemId}
                            onClick={() => setActiveProblem(p.problem_id)}
                            title={`Open ${p.title}`}
                          />
                        ))}
                      </div>

                      <div className="section-label" style={{ marginTop: 24 }}>
                        <span className="eyebrow">Hint pressure by problem</span>
                      </div>
                      <StackedLegend
                        items={[
                          { className: "stacked-seg-l1", label: "L1 — conceptual nudge" },
                          { className: "stacked-seg-l2", label: "L2 — naming the clause" },
                          { className: "stacked-seg-l3", label: "L3 — query skeleton" },
                        ]}
                      />
                      <div className="hbar-chart">
                        {problems.map((p, i) => {
                          const hc = hintCounts(p.hint_requests);
                          const total = hc[1] + hc[2] + hc[3];
                          return (
                            <StackedHBar
                              key={p.problem_id}
                              label={truncTitle(numberedTitle(p, i))}
                              segments={[
                                { value: hc[1], className: "stacked-seg-l1", label: "L1" },
                                { value: hc[2], className: "stacked-seg-l2", label: "L2" },
                                { value: hc[3], className: "stacked-seg-l3", label: "L3" },
                              ]}
                              total={total}
                              onClick={() => setActiveProblem(p.problem_id)}
                              title={`Open ${p.title}`}
                            />
                          );
                        })}
                      </div>
                    </>
                  )}

                  <div className="section-label" style={{ marginTop: 24 }}>
                    <span className="eyebrow">Per-student</span>
                  </div>
                  {data.students.length === 0 ? (
                    <div className="empty" style={{ padding: 24 }}>No students yet.</div>
                  ) : (
                    <OverviewStudentGrid
                      students={data.students}
                      problems={problems}
                      onOpenStudent={(name) => setActiveStudent(name)}
                      onOpenProblem={(pid) => setActiveProblem(pid)}
                    />
                  )}

                  <div className="ins-actions">
                    <a className="btn" href={api.classAnalyticsCsvUrl(classId, setFilter)} download>
                      Download CSV
                    </a>
                  </div>
                </>
              ) : active ? (
                <>
                  <div className="ins-drill-head">
                    <button className="btn ghost sm" onClick={() => setActiveProblem(null)}>
                      ← Overview
                    </button>
                    <h2 className="ins-drill-title">
                      <span className="numeral">{String(activeIdx + 1).padStart(2, "0")}</span> {active.title}
                    </h2>
                    {active.difficulty && (
                      <span className="tag">
                        <span className={"dot " + active.difficulty} /> {DIFF_LABEL[active.difficulty] || active.difficulty}
                      </span>
                    )}
                  </div>

                  <div className="ins-statstrip">
                    <div className="ins-statbox">
                      <div className="ins-statval">{active.attempts}</div>
                      <div className="ins-statlabel">attempts</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{active.students_attempted}</div>
                      <div className="ins-statlabel">students attempted</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{active.students_solved}</div>
                      <div className="ins-statlabel">students solved</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{fmtPct(active.solve_rate)}</div>
                      <div className="ins-statlabel">solve rate</div>
                    </div>
                    <div className="ins-statbox">
                      <div className="ins-statval">{fmtNum(active.avg_attempts_to_first_solve, 2)}</div>
                      <div className="ins-statlabel">avg attempts to first solve</div>
                    </div>
                  </div>

                  <div className="section-label" style={{ marginTop: 24 }}>
                    <span className="eyebrow">Hint levels used</span>
                  </div>
                  {(() => {
                    const hc = hintCounts(active.hint_requests);
                    const byLevel = hintLevelStudents(active.students);
                    return (
                      <MiniBars
                        bars={[
                          { label: "L1", value: hc[1], className: "stacked-seg-l1", title: levelTooltip(1, byLevel[1]) },
                          { label: "L2", value: hc[2], className: "stacked-seg-l2", title: levelTooltip(2, byLevel[2]) },
                          { label: "L3", value: hc[3], className: "stacked-seg-l3", title: levelTooltip(3, byLevel[3]) },
                        ]}
                      />
                    );
                  })()}

                  {active.predicted && (
                    <>
                      <div className="section-label" style={{ marginTop: 24 }}>
                        <span className="eyebrow">Predicted vs actual</span>
                      </div>
                      <div className="ins-statstrip">
                        <div className="ins-statbox">
                          <div className="ins-statval">{fmtNum(active.predicted.avg_max_hint_level)}</div>
                          <div className="ins-statlabel">predicted avg max hint level</div>
                        </div>
                        <div className="ins-statbox">
                          <div className="ins-statval">{fmtNum(active.avg_max_hint_level_used)}</div>
                          <div className="ins-statlabel">actual avg max hint level</div>
                        </div>
                        <div className="ins-statbox">
                          <div className="ins-statval">
                            <HintLevelCompare
                              predicted={active.predicted.avg_max_hint_level}
                              actual={active.avg_max_hint_level_used}
                            />
                          </div>
                          <div className="ins-statlabel">comparison</div>
                        </div>
                        {active.predicted.solved_unaided_rate != null && (
                          <div className="ins-statbox">
                            <div className="ins-statval">{fmtPct(active.predicted.solved_unaided_rate)}</div>
                            <div className="ins-statlabel">predicted solved unaided</div>
                          </div>
                        )}
                      </div>
                    </>
                  )}

                  <div className="section-label" style={{ marginTop: 24 }}>
                    <span className="eyebrow">Per-student</span>
                  </div>
                  {(active.students ?? []).length === 0 ? (
                    <div className="empty" style={{ padding: 24 }}>
                      per-student detail appears for new attempts
                    </div>
                  ) : (
                    <div className="table-scroll">
                      <table className="datatable ins-table">
                        <thead>
                          <tr>
                            <th>Student</th>
                            <th>Grade attempts</th>
                            <th>Solved</th>
                            <th>First solved on attempt #</th>
                            <th>Max hint level</th>
                          </tr>
                        </thead>
                        <tbody>
                          {(active.students ?? []).map((s) => (
                            <tr key={s.student}>
                              <td>
                                <button className="ins-name-btn" onClick={() => setActiveStudent(s.student)} data-tip="Open student progression">
                                  {s.student}
                                </button>
                              </td>
                              <td>{s.n_grades}</td>
                              <td>{s.solved ? "✓" : "—"}</td>
                              <td>{s.first_solve_at ?? "—"}</td>
                              <td>{s.max_hint_level}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              ) : null}
            </>
          )}
        </div>
      </section>
    </div>
  );
}

// ---- Live: class-progress grid + ticker, polling-driven (no websockets) -------------
function LiveView({
  liveData, liveErr, problems, sessionScope, onSessionScope,
  sessionState, onChangeSession, lastUpdated, onJumpToProblem, onOpenStudent,
}) {
  // one-time-per-session explainer the first time the instructor opens the live controls
  const [showExplainer, setShowExplainer] = useState(
    () => !sessionStorage.getItem("tutor.sessionControlExplained"));
  function dismissExplainer() {
    sessionStorage.setItem("tutor.sessionControlExplained", "1");
    setShowExplainer(false);
  }
  // index lookup for the recent-events ticker (numbered titles)
  const problemTitles = useMemo(() => {
    const idxById = {};
    problems.forEach((p, i) => { idxById[p.problem_id] = i; });
    return { idxById };
  }, [problems]);

  if (liveErr) {
    return <div className="banner err">{liveErr}</div>;
  }

  if (!liveData) {
    return <div className="empty"><span className="spin" /></div>;
  }

  const liveProblems = liveData.problems || [];
  const students = liveData.students || [];
  const recent = liveData.recent || [];
  const nowTs = liveData.now;

  // active students first, then alphabetical (students array already sorted by name)
  const ordered = [...students].sort((a, b) => {
    if (a.active !== b.active) return a.active ? -1 : 1;
    return a.student.toLowerCase().localeCompare(b.student.toLowerCase());
  });

  const updatedLabel = lastUpdated
    ? `updated ${relTime(lastUpdated.toISOString(), new Date().toISOString())}`
    : "—";
  const stateLabel = { running: "live", paused: "paused", ended: "ended" }[sessionState] || "live";

  return (
    <>
      {showExplainer && (
        <div className="banner info live-explainer">
          <div>
            <b>Pause</b> freezes the whole test — students can't submit or ask for hints until you
            resume. <b>End test</b> lets students finish only the question they're on (no hints, no
            moving to other questions). Both apply to everyone in this class.
          </div>
          <button className="setup-nudge-x" aria-label="Dismiss" onClick={dismissExplainer}>×</button>
        </div>
      )}
      <div className="live-head">
        <div className="live-session">
          <span className="live-session-label">Session</span>
          <div className="live-session-switch">
            {SESSION_OPTIONS.map((o) => (
              <button
                key={o.key}
                className={"live-session-btn" + (sessionScope === o.key ? " on" : "")}
                onClick={() => onSessionScope(o.key)}
              >
                {o.label}
              </button>
            ))}
          </div>
        </div>
        <div className="live-status">
          {sessionState === "running" && <span className="live-dot" />}
          <span className={"live-state-tag live-state-" + sessionState}>{stateLabel}</span>
          <span className="live-status-sep">· {updatedLabel}</span>
          <SessionControls sessionState={sessionState} onChangeSession={onChangeSession} />
        </div>
      </div>

      {liveProblems.length === 0 || (students.length === 0 && recent.length === 0) ? (
        <div className="empty-pane">
          <div className="empty">
            <div className="big">No activity yet</div>
            <div>No activity yet in this session — students appear the moment they run a query.</div>
          </div>
        </div>
      ) : (
        <>
          <div className="ins-statstrip" style={{ marginBottom: 14 }}>
            <div className="ins-statbox">
              <div className="ins-statval">{liveData.summary.n_students}</div>
              <div className="ins-statlabel">students</div>
            </div>
            <div className="ins-statbox">
              <div className="ins-statval">{liveData.summary.n_active}</div>
              <div className="ins-statlabel">active now</div>
            </div>
            <div className="ins-statbox">
              <div className="ins-statval">{liveData.summary.n_solved_cells}</div>
              <div className="ins-statlabel">solved cells</div>
            </div>
            <div className="ins-statbox">
              <div className="ins-statval">{liveData.summary.n_attempts}</div>
              <div className="ins-statlabel">attempts</div>
            </div>
          </div>

          <div className="live-grid-scroll">
            <table className="live-grid">
              <thead>
                <tr>
                  <th className="live-grid-corner">Student</th>
                  {liveProblems.map((p, i) => (
                    <th
                      key={p.problem_id}
                      className="live-grid-col"
                      data-tip={p.title}
                      onClick={() => onJumpToProblem(p.problem_id)}
                    >
                      {String(i + 1).padStart(2, "0")}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ordered.map((s) => (
                  <tr key={s.student}>
                    <td className="live-grid-name">
                      {s.active && <span className="live-dot" data-tip="active in the last 2 minutes" />}
                      <button className="ins-name-btn" onClick={() => onOpenStudent(s.student)} data-tip="Open student progression">
                        {s.student}
                      </button>
                    </td>
                    {liveProblems.map((p) => {
                      const cell = s.problems[p.problem_id];
                      const status = cell?.status;
                      let content = "";
                      let cls = "live-cell";
                      if (status === "solved") { cls += " live-cell-solved"; content = "✓"; }
                      else if (status === "hinted") { cls += " live-cell-hinted"; content = cell.max_hint_level || ""; }
                      else if (status === "trying") { cls += " live-cell-trying"; content = cell.n_grades; }
                      return (
                        <td key={p.problem_id} className={cls} data-tip={cellTooltip(cell, nowTs)}>
                          {content}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="section-label" style={{ marginTop: 24 }}>
            <span className="eyebrow">Recent activity</span>
          </div>
          {recent.length === 0 ? (
            <div className="empty" style={{ padding: 24 }}>No activity yet.</div>
          ) : (
            <ul className="live-ticker">
              {recent.map((ev, i) => (
                <li key={i} className="live-ticker-row">{recentLine(ev, problemTitles)}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </>
  );
}

// ---- Overview student × problem grid (all-time) -------------------------------------
// Same shape as the Live grid, but covers every student (including roster names who haven't
// started) and is keyed off the all-time analytics rollup. Names open a student's full
// progression; column headers jump to that problem's drill-down.
function OverviewStudentGrid({ students, problems, onOpenStudent, onOpenProblem }) {
  // (student-key, problem_id) -> per-cell rollup, built from each problem's student rows
  const cells = useMemo(() => {
    const m = {};
    for (const p of problems) {
      for (const r of p.students || []) {
        const k = r.student.toLowerCase();
        (m[k] || (m[k] = {}))[p.problem_id] = r;
      }
    }
    return m;
  }, [problems]);

  const total = problems.length;

  return (
    <>
      <div className="ins-grid-legend">
        <span><span className="live-cell-solved ins-legend-chip">✓</span> solved</span>
        <span><span className="live-cell-trying ins-legend-chip">2</span> attempts, not solved</span>
        <span><span className="live-cell-stuck ins-legend-chip" style={{ width: "auto", padding: "0 5px" }}>2 ?1</span> attempts · highest hint level</span>
        <span><span className="ins-legend-chip ins-legend-empty" /> not started</span>
      </div>
      <div className="live-grid-scroll">
        <table className="live-grid">
          <thead>
            <tr>
              <th className="live-grid-corner">Student</th>
              {problems.map((p, i) => (
                <th key={p.problem_id} className="live-grid-col" data-tip={p.title}
                    onClick={() => onOpenProblem(p.problem_id)}>
                  {String(i + 1).padStart(2, "0")}
                </th>
              ))}
              <th className="live-grid-col" data-tip="Problems solved (of total)" style={{ cursor: "default" }}>% solved</th>
            </tr>
          </thead>
          <tbody>
            {students.map((s) => {
              const row = cells[s.student.toLowerCase()] || {};
              const pct = total ? Math.round((s.solved / total) * 100) : null;
              return (
                <tr key={s.student}>
                  <td className="live-grid-name">
                    <button className="ins-name-btn" onClick={() => onOpenStudent(s.student)} data-tip="Open student progression">
                      {s.student}
                    </button>
                    {!s.attempted && <span className="ins-notstarted">not started</span>}
                  </td>
                  {problems.map((p) => {
                    const cell = row[p.problem_id];
                    let cls = "live-cell";
                    let content = "";
                    let tip = "No activity";
                    if (cell) {
                      if (cell.solved) {
                        cls += " live-cell-solved"; content = "✓";
                        tip = `Solved${cell.first_solve_at ? ` on attempt #${cell.first_solve_at}` : ""}`;
                      } else if (cell.n_grades > 0 || cell.max_hint_level > 0) {
                        const hint = cell.max_hint_level > 0 ? `?${cell.max_hint_level}` : "";
                        content = [cell.n_grades > 0 ? cell.n_grades : "", hint].filter(Boolean).join(" ");
                        cls += cell.max_hint_level > 0 ? " live-cell-stuck" : " live-cell-trying";
                        const a = `${cell.n_grades} attempt${cell.n_grades === 1 ? "" : "s"}`;
                        tip = hint ? `${a}, asked up to an L${cell.max_hint_level} hint` : `${a}, not solved`;
                      }
                    }
                    return <td key={p.problem_id} className={cls} data-tip={tip}>{content}</td>;
                  })}
                  <td className="live-cell" data-tip={`${s.solved} of ${total} solved`}>
                    {pct == null ? "" : `${pct}%`}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ---- Single-student progression (drill from a name) ---------------------------------
function fullTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function TimelineEvent({ ev, idxById }) {
  let head, cls;
  if (ev.kind === "grade") {
    if (ev.correct) {
      cls = "ins-tl-solved";
      head = `Solved ${ev.title} (attempt #${ev.attempt_no}) — passed ${ev.n_passed}/${ev.n_seeds}`;
    } else {
      cls = "ins-tl-wrong";
      const cat = ev.category ? ` — ${ev.category}` : "";
      head = `Attempt #${ev.attempt_no} on ${ev.title}${cat} — passed ${ev.n_passed ?? "?"}/${ev.n_seeds ?? "?"}`;
    }
  } else {
    cls = "ins-tl-hint";
    head = `Asked for an L${ev.hint_level ?? "?"} hint on ${ev.title}`;
  }
  const idx = idxById[ev.problem_id];
  const num = idx != null ? String(idx + 1).padStart(2, "0") : null;
  return (
    <li className={"ins-tl-row " + cls}>
      <div className="ins-tl-when">
        {fullTime(ev.ts)}
        {num && <div className="numeral ins-tl-num" data-tip={ev.title}>{num}</div>}
      </div>
      <div className="ins-tl-body">
        <div className="ins-tl-head">{head}</div>
        {ev.sql && <pre className="ins-tl-sql">{ev.sql}</pre>}
      </div>
    </li>
  );
}

function StudentDetail({ student, detail, loading, err, problems, onBack, onOpenProblem }) {
  if (err) {
    return (
      <>
        <div className="ins-drill-head">
          <button className="btn ghost sm" onClick={onBack}>← Overview</button>
          <h2 className="ins-drill-title">{student}</h2>
        </div>
        <div className="banner err">{err}</div>
      </>
    );
  }
  if (loading || !detail) {
    return (
      <>
        <div className="ins-drill-head">
          <button className="btn ghost sm" onClick={onBack}>← Overview</button>
          <h2 className="ins-drill-title">{student}</h2>
        </div>
        <div className="empty"><span className="spin" /></div>
      </>
    );
  }

  // problem index for numbering, from the class-wide problem order
  const idxById = {};
  problems.forEach((p, i) => { idxById[p.problem_id] = i; });

  // per-problem averages across the problems this student touched, + the MODE hint level
  // (the single most-requested rung, not the mean) per the instructor's request.
  const tp = detail.problems || [];
  const touched = tp.length;
  const avgAttempts = touched ? tp.reduce((a, p) => a + (p.n_grades || 0), 0) / touched : null;
  const avgHints = touched
    ? tp.reduce((a, p) => a + Object.values(p.hint_levels || {}).reduce((x, y) => x + y, 0), 0) / touched
    : null;
  const modeHintLevel = (() => {
    const tally = { 1: 0, 2: 0, 3: 0 };
    for (const p of tp) for (const [lvl, n] of Object.entries(p.hint_levels || {})) tally[lvl] = (tally[lvl] || 0) + n;
    const entries = Object.entries(tally).filter(([, n]) => n > 0);
    if (!entries.length) return null;
    return entries.reduce((a, b) => (b[1] > a[1] ? b : a))[0]; // ties: lower level (insertion order 1<2<3)
  })();

  return (
    <>
      <div className="ins-drill-head">
        <button className="btn ghost sm" onClick={onBack}>← Overview</button>
        <h2 className="ins-drill-title">{detail.student}</h2>
      </div>

      <div className="ins-statstrip">
        <div className="ins-statbox">
          <div className="ins-statval">{detail.n_attempts}</div>
          <div className="ins-statlabel">attempts logged</div>
        </div>
        <div className="ins-statbox">
          <div className="ins-statval">{detail.n_solved} / {detail.n_problems_touched}</div>
          <div className="ins-statlabel">solved / attempted</div>
        </div>
        <div className="ins-statbox">
          <div className="ins-statval">{fmtNum(avgAttempts, 1)}</div>
          <div className="ins-statlabel">avg attempts / problem</div>
        </div>
        <div className="ins-statbox">
          <div className="ins-statval">{fmtNum(avgHints, 1)}</div>
          <div className="ins-statlabel">avg hints / problem</div>
        </div>
        <div className="ins-statbox">
          <div className="ins-statval">{modeHintLevel ? `L${modeHintLevel}` : "—"}</div>
          <div className="ins-statlabel">modal hint level</div>
        </div>
      </div>

      {detail.problems.length > 0 && (
        <>
          <div className="section-label" style={{ marginTop: 24 }}>
            <span className="eyebrow">Per problem</span>
          </div>
          <div className="table-scroll">
            <table className="datatable ins-table">
              <thead>
                <tr>
                  <th>Problem</th>
                  <th>Attempts</th>
                  <th>Solved</th>
                  <th>First solved on attempt #</th>
                  <th>Max hint level</th>
                </tr>
              </thead>
              <tbody>
                {detail.problems.map((p) => {
                  const i = idxById[p.problem_id];
                  return (
                    <tr key={p.problem_id} className="ins-row-btn" onClick={() => onOpenProblem(p.problem_id)} data-tip={`Open ${p.title}`}>
                      <td>
                        {i != null && <span className="numeral">{String(i + 1).padStart(2, "0")}</span>}{" "}
                        {p.title}
                      </td>
                      <td>{p.n_grades}</td>
                      <td>{p.solved ? "✓" : "—"}</td>
                      <td>{p.first_solve_at ?? "—"}</td>
                      <td>{p.max_hint_level || "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div className="section-label" style={{ marginTop: 24 }}>
        <span className="eyebrow">Progression - attempt log</span>
      </div>
      {detail.timeline.length === 0 ? (
        <div className="empty" style={{ padding: 24 }}>No attempts logged yet for this student.</div>
      ) : (
        <ul className="ins-timeline">
          {detail.timeline.map((ev, i) => <TimelineEvent key={i} ev={ev} idxById={idxById} />)}
        </ul>
      )}
    </>
  );
}
