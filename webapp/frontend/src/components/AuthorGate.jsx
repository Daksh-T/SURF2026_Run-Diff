import { createContext, useEffect, useState } from "react";
import { api } from "../lib/api.js";

// Auth state shared with the author toolbar (AuthorLayout), which renders the compact
// lock control inline instead of a separate full-width banner.
//   passwordSet — has a password been configured at all
//   setPassword(value) — set one (throws on error); lock() — drop the stored key + re-gate
export const AuthorAuthContext = createContext({
  passwordSet: false,
  setPassword: async () => {},
  changePassword: async () => {},
  clearPassword: async () => {},
  lock: () => {},
});

// Wraps the /author routes with a password gate.
//
// - If a password is set and we have no stored key (or the stored key is bad),
//   show a locked pane asking for the password.
// - Otherwise render children inside an AuthorAuthContext provider; the toolbar
//   reads it to offer "set a password" (when none) or "Lock" (when one exists).
export default function AuthorGate({ children }) {
  const [status, setStatus] = useState(null); // { password_set } | null while loading
  const [unlocked, setUnlocked] = useState(false);
  const [pw, setPw] = useState("");
  const [err, setErr] = useState(null);
  const [checking, setChecking] = useState(false);

  useEffect(() => {
    api.authStatus().then(async (s) => {
      setStatus(s);
      if (!s.password_set) {
        setUnlocked(true);
        return;
      }
      const stored = localStorage.getItem("tutor.authorKey");
      if (!stored) return;
      try {
        const res = await api.authCheck(stored);
        if (res.ok) setUnlocked(true);
        else localStorage.removeItem("tutor.authorKey");
      } catch {
        localStorage.removeItem("tutor.authorKey");
      }
    }).catch(() => {
      setStatus({ password_set: false });
      setUnlocked(true);
    });
  }, []);

  async function tryUnlock() {
    if (!pw.trim()) return;
    setChecking(true);
    setErr(null);
    try {
      const res = await api.authCheck(pw);
      if (res.ok) {
        localStorage.setItem("tutor.authorKey", pw);
        setUnlocked(true);
      } else {
        setErr("Incorrect password.");
      }
    } catch (e) {
      setErr(e.message);
    } finally {
      setChecking(false);
    }
  }

  // set a password (called by the toolbar's compact control; throws so the caller can show
  // its own inline error). On success the toolbar flips to the "Locked · Lock" state.
  async function setPassword(value) {
    await api.authSet(value, null);
    localStorage.setItem("tutor.authorKey", value);
    setStatus({ password_set: true });
  }

  // change an existing password (needs the current one); on success the stored unlock key
  // rolls over to the new value so this session stays unlocked.
  async function changePassword(current, value) {
    await api.authSet(value, current);
    localStorage.setItem("tutor.authorKey", value);
    setStatus({ password_set: true });
  }

  // remove the password entirely (authoring goes open). Needs the current password.
  async function clearPassword(current) {
    await api.authClear(current);
    localStorage.removeItem("tutor.authorKey");
    setStatus({ password_set: false });
    setUnlocked(true);
  }

  function lock() {
    localStorage.removeItem("tutor.authorKey");
    setUnlocked(false);
    setPw("");
  }

  if (!status) {
    return <div className="main"><div className="empty"><span className="spin" /></div></div>;
  }

  if (status.password_set && !unlocked) {
    return (
      <div className="main">
        <div className="empty-pane">
          <div className="empty" style={{ maxWidth: 320 }}>
            <div className="big">Author access</div>
            <div className="field" style={{ marginTop: 16, marginBottom: 10, textAlign: "left" }}>
              <label>Password</label>
              <input
                className="input"
                type="password"
                value={pw}
                onChange={(e) => setPw(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && tryUnlock()}
                autoFocus
              />
            </div>
            {err && <div className="banner err">{err}</div>}
            <button className="btn primary" onClick={tryUnlock} disabled={checking || !pw.trim()}>
              {checking ? "Checking…" : "Unlock"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <AuthorAuthContext.Provider value={{ passwordSet: status.password_set, setPassword, changePassword, clearPassword, lock }}>
      {children}
    </AuthorAuthContext.Provider>
  );
}
