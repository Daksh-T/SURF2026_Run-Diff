import { useContext, useEffect, useState } from "react";
import { Link, NavLink, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import Student from "./routes/Student.jsx";
import Instructor from "./routes/Instructor.jsx";
import Classes from "./routes/Classes.jsx";
import Insights from "./routes/Insights.jsx";
import Setup from "./routes/Setup.jsx";
import AuthorGate, { AuthorAuthContext } from "./components/AuthorGate.jsx";
import TooltipLayer from "./components/Tooltip.jsx";
import { api } from "./lib/api.js";

const tabClass = ({ isActive }) => "author-tab" + (isActive ? " on" : "");

// Author is a hub: Sets (author + manage problem sets), Classes (classrooms that assign sets),
// Insights (per-class, per-set analytics). One full-width toolbar carries the section tabs on
// the left and the password-lock control on the right, then the active section fills the rest.
function AuthorLayout() {
  // remember the active author sub-tab so switching to Practice and back (or clicking the
  // top-bar "Author" link) returns to the section you left, not always "Sets".
  const loc = useLocation();
  useEffect(() => {
    const m = loc.pathname.match(/^\/author\/(sets|classes|insights)/);
    if (m) localStorage.setItem("tutor.authorTab", m[1]);
  }, [loc.pathname]);

  return (
    <div className="author-shell">
      <div className="author-toolbar">
        <nav className="author-tabs">
          <NavLink to="/author/sets" className={tabClass}>Sets</NavLink>
          <NavLink to="/author/classes" className={tabClass}>Classes</NavLink>
          <NavLink to="/author/insights" className={tabClass}>Insights</NavLink>
        </nav>
        <AuthorLock />
      </div>
      <Outlet />
    </div>
  );
}

// The /author index redirect honors the remembered sub-tab (defaults to Sets).
function AuthorIndexRedirect() {
  const tab = localStorage.getItem("tutor.authorTab") || "sets";
  return <Navigate to={tab} replace />;
}

// Compact lock control living in the author toolbar. No password yet → a "Set password" pill
// that expands to an inline field; password set → a "Locked · Lock" affordance.
function AuthorLock() {
  const { passwordSet, setPassword, changePassword, clearPassword, lock } = useContext(AuthorAuthContext);
  // mode: null (idle) | "set" (first password) | "change" | "remove"
  const [mode, setMode] = useState(null);
  const [current, setCurrent] = useState("");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  function reset() { setMode(null); setCurrent(""); setValue(""); setErr(null); }

  async function submit() {
    setBusy(true);
    setErr(null);
    try {
      if (mode === "set") {
        if (!value.trim()) return;
        await setPassword(value);
      } else if (mode === "change") {
        if (!value.trim()) return;
        await changePassword(current, value);
      } else if (mode === "remove") {
        await clearPassword(current);
      }
      reset();
    } catch (e) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  // expanded inline form (set / change / remove)
  if (mode) {
    const isRemove = mode === "remove";
    return (
      <div className="author-lock">
        {(mode === "change" || mode === "remove") && (
          <input
            className="input author-lock-pw"
            type="password"
            placeholder="Current password"
            value={current}
            autoFocus
            onChange={(e) => setCurrent(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") reset(); }}
          />
        )}
        {!isRemove && (
          <input
            className="input author-lock-pw"
            type="password"
            placeholder={mode === "change" ? "New password" : "New password"}
            value={value}
            autoFocus={mode === "set"}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") reset(); }}
          />
        )}
        <button
          className={"btn sm " + (isRemove ? "danger" : "primary")}
          onClick={submit}
          disabled={busy || (!isRemove && !value.trim())}
        >
          {busy ? "…" : isRemove ? "Remove" : "Save"}
        </button>
        <button className="btn sm ghost" onClick={reset}>Cancel</button>
        {err && <span className="cls-joinbar-err">{err}</span>}
      </div>
    );
  }

  if (passwordSet) {
    return (
      <div className="author-lock">
        <span className="author-lock-state">🔒 Locked</span>
        <button className="btn sm ghost" onClick={lock}>Lock now</button>
        <button className="btn sm ghost" onClick={() => setMode("change")}>Change</button>
        <button className="btn sm ghost" onClick={() => setMode("remove")}>Remove</button>
      </div>
    );
  }

  return (
    <div className="author-lock">
      <span className="author-lock-state author-lock-open">Authoring is open</span>
      <button className="btn sm ghost" onClick={() => setMode("set")}>Set password</button>
    </div>
  );
}

function SetupNudge() {
  const [show, setShow] = useState(false);

  useEffect(() => {
    if (sessionStorage.getItem("tutor.setupNudgeDismissed")) return;
    let alive = true;
    api
      .setupStatus()
      .then((s) => {
        // only nudge when live hints are actually unavailable
        if (alive && !(s.ollama_running && s.model_present)) setShow(true);
      })
      .catch(() => {}); // backend unreachable: stay quiet
    return () => {
      alive = false;
    };
  }, []);

  if (!show) return null;
  return (
    <div className="setup-nudge">
      <span>
        Live hints need the local model.{" "}
        <Link to="/setup">Set up</Link>
      </span>
      <button
        className="setup-nudge-x"
        aria-label="Dismiss"
        onClick={() => {
          sessionStorage.setItem("tutor.setupNudgeDismissed", "1");
          setShow(false);
        }}
      >
        ×
      </button>
    </div>
  );
}

export default function App() {
  return (
    <div className="shell">
      <TooltipLayer />
      <header className="topbar">
        <span className="wordmark">
          Run<span className="dot">·</span>Diff
        </span>
        <span className="spacer" />
        <nav className="roleswitch">
          <NavLink to="/practice" className={({ isActive }) => (isActive ? "on" : "")}>
            Practice
          </NavLink>
          <NavLink to="/author" className={({ isActive }) => (isActive ? "on" : "")}>
            Author
          </NavLink>
        </nav>
      </header>

      <SetupNudge />

      <Routes>
        <Route path="/" element={<Navigate to="/practice" replace />} />
        <Route path="/practice" element={<Student />} />
        <Route path="/practice/:setId" element={<Student />} />
        <Route path="/author" element={<AuthorGate><AuthorLayout /></AuthorGate>}>
          <Route index element={<AuthorIndexRedirect />} />
          <Route path="sets" element={<Instructor />} />
          <Route path="classes" element={<Classes />} />
          <Route path="insights" element={<Insights />} />
        </Route>
        {/* old top-level /insights link → new home under Author */}
        <Route path="/insights" element={<Navigate to="/author/insights" replace />} />
        <Route path="/setup" element={<Setup />} />
        <Route path="*" element={<Navigate to="/practice" replace />} />
      </Routes>
    </div>
  );
}
