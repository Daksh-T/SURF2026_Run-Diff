import { useEffect, useState } from "react";
import { api } from "../lib/api.js";

export default function ApiKeySettings() {
  const [open, setOpen] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    api.apiKeyStatus().then((s) => setConfigured(s.configured)).catch(() => {});
  }, []);
  async function save(event) {
    event.preventDefault();
    setBusy(true);
    setMessage("");
    try {
      await api.saveApiKey(key);
      setKey("");
      setConfigured(true);
      setMessage("✓ API key saved.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="api-key-settings">
      <button className="btn sm ghost" aria-expanded={open} aria-controls="api-key-form" onClick={() => { setOpen(!open); setKey(""); setMessage(""); }}>
        {configured ? "API key ✓" : "Add API key"}
      </button>
      {open && <form id="api-key-form" className="card api-key-form" onSubmit={save}
        onKeyDown={(event) => { if (event.key === "Escape") { setOpen(false); setKey(""); } }}>
        <label htmlFor="groq-api-key">Groq API key</label>
        <p className="run-hint">Use your Groq key for authoring. Save it on this computer for future sessions.</p>
        <input id="groq-api-key" className="input" type="password" autoComplete="off" spellCheck={false}
          value={key} onChange={(e) => setKey(e.target.value)} placeholder={configured ? "Enter a replacement key" : "Paste your API key"} disabled={busy} autoFocus />
        <div className="cls-row" style={{ gap: 8, marginTop: 12 }}>
          <button className="btn primary" disabled={busy || !key.trim()}>{busy ? "Saving…" : "Save key"}</button>
          <button className="btn ghost" type="button" disabled={busy} onClick={() => { setOpen(false); setKey(""); }}>Close</button>
        </div>
        {message && <p role="status">{message}</p>}
      </form>}
    </div>
  );
}
