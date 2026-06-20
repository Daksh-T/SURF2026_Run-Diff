import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api.js";

const DOWNLOAD_URL = "https://ollama.com/download";

function platformLabel(p) {
  if (p === "darwin") return "macOS";
  if (p === "win32") return "Windows";
  if (p && p.startsWith("linux")) return "Linux";
  return "your platform";
}

function fmtMB(bytes) {
  if (!bytes) return "0";
  return (bytes / (1024 * 1024)).toFixed(0);
}

export default function Setup() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [pull, setPull] = useState(null); // {completed,total,status,done,error}
  const [pulling, setPulling] = useState(false);
  const [pullErr, setPullErr] = useState(null);
  const pollRef = useRef(null);

  async function refresh() {
    setLoading(true);
    try {
      setStatus(await api.setupStatus());
    } catch (e) {
      setStatus(null);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    return () => clearInterval(pollRef.current);
  }, []);

  async function startPull() {
    setPullErr(null);
    setPulling(true);
    setPull({ status: "starting", completed: 0, total: 0, done: false, error: null });
    try {
      const { job_id } = await api.setupPull();
      pollRef.current = setInterval(async () => {
        try {
          const p = await api.setupPullStatus(job_id);
          setPull(p);
          if (p.done) {
            clearInterval(pollRef.current);
            setPulling(false);
            if (p.error) setPullErr(p.error);
            else refresh(); // re-derive steps; model should now be present
          }
        } catch (e) {
          clearInterval(pollRef.current);
          setPulling(false);
          setPullErr(e.message);
        }
      }, 700);
    } catch (e) {
      setPulling(false);
      setPullErr(e.message);
    }
  }

  const running = status?.ollama_running;
  const installed = status?.ollama_installed;
  const present = status?.model_present;
  const tag = status?.model_tag || "qwen2.5-coder:7b";
  const ready = running && present;

  // step state: "done" | "active" | "todo"
  const s1 = installed === true ? "done" : "active"; // install
  const s2 = !running ? (s1 === "done" ? "active" : "todo") : "done"; // start
  const s3 = ready ? "done" : running ? "active" : "todo"; // download model

  const pct = pull && pull.total ? Math.min(100, Math.round((pull.completed / pull.total) * 100)) : 0;

  return (
    <div className="setup-wrap">
      <div className="setup-head">
        <div className="eyebrow">First-run setup</div>
        <h1>Set up the local tutor model</h1>
        <p className="setup-lede">
          Live, model-written hints run entirely on your machine through{" "}
          <a href="https://ollama.com" target="_blank" rel="noreferrer">Ollama</a> — nothing leaves
          your computer. This is a one-time setup. The app works without it; hints just fall back to
          built-in templates until it's done.
        </p>
      </div>

      {ready && (
        <div className="setup-ready">
          <div className="setup-ready-mark numeral">OK</div>
          <div>
            <h2>Local tutor ready</h2>
            <p className="setup-muted">
              Ollama is running and <span className="mono">{tag}</span> is installed. Live hints are on.
            </p>
            <Link className="btn primary" to="/practice">Back to practice</Link>
          </div>
        </div>
      )}

      {!ready && (
        <ol className="setup-steps">
          {/* Step 1 — install */}
          <li className={`setup-step is-${s1}`}>
            <div className="setup-step-num numeral">1</div>
            <div className="setup-step-body">
              <h3>Install Ollama</h3>
              {s1 === "done" ? (
                <p className="setup-muted">Ollama is installed on this {platformLabel(status?.platform)} machine.</p>
              ) : (
                <>
                  <p>
                    Download and install Ollama for {platformLabel(status?.platform)}. It's a small,
                    free runtime that serves the model locally.
                  </p>
                  <div className="setup-actions">
                    <a className="btn primary" href={DOWNLOAD_URL} target="_blank" rel="noreferrer">
                      Get Ollama
                    </a>
                    <button className="btn ghost" onClick={refresh} disabled={loading}>
                      I've installed it — check again
                    </button>
                  </div>
                </>
              )}
            </div>
          </li>

          {/* Step 2 — start */}
          <li className={`setup-step is-${s2}`}>
            <div className="setup-step-num numeral">2</div>
            <div className="setup-step-body">
              <h3>Start Ollama</h3>
              {s2 === "done" ? (
                <p className="setup-muted">Ollama is running and reachable.</p>
              ) : s2 === "todo" ? (
                <p className="setup-muted">Install Ollama first.</p>
              ) : (
                <>
                  <p>
                    Ollama is installed but not running. Open the Ollama app, or run{" "}
                    <code>ollama serve</code> in a terminal, then check again.
                  </p>
                  <div className="setup-actions">
                    <button className="btn ghost" onClick={refresh} disabled={loading}>
                      Check again
                    </button>
                  </div>
                </>
              )}
            </div>
          </li>

          {/* Step 3 — download model */}
          <li className={`setup-step is-${s3}`}>
            <div className="setup-step-num numeral">3</div>
            <div className="setup-step-body">
              <h3>Download the tutor model</h3>
              {s3 === "todo" ? (
                <p className="setup-muted">Start Ollama first.</p>
              ) : present ? (
                <p className="setup-muted">
                  <span className="mono">{tag}</span> is already installed.
                </p>
              ) : (
                <>
                  <p>
                    Pull <span className="mono">{tag}</span> (about <strong>4.7 GB</strong>). This
                    first-time download takes a while depending on your connection, and only happens
                    once.
                  </p>
                  {!pulling && !pull?.done && (
                    <div className="setup-actions">
                      <button className="btn primary" onClick={startPull} disabled={!running}>
                        Download
                      </button>
                    </div>
                  )}
                  {pull && (pulling || pull.done) && (
                    <div className="setup-progress">
                      <div className="setup-bar">
                        <div className="setup-bar-fill" style={{ width: `${pct}%` }} />
                      </div>
                      <div className="setup-prog-meta">
                        <span>{pull.status}</span>
                        <span className="mono">
                          {pull.total
                            ? `${fmtMB(pull.completed)} / ${fmtMB(pull.total)} MB · ${pct}%`
                            : ""}
                        </span>
                      </div>
                    </div>
                  )}
                  {pullErr && <div className="banner err setup-banner">{pullErr}</div>}
                </>
              )}
            </div>
          </li>
        </ol>
      )}

      <p className="setup-foot setup-muted">
        Hints fall back to deterministic offline templates whenever the local model isn't available,
        so practice is never blocked by this setup.
      </p>
    </div>
  );
}
