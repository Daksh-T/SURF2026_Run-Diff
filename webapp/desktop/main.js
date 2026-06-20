// Run·Diff desktop shell.
//
// On ready: ensure the backend is up (reuse an already-running :8077, else spawn it — the
// packaged sidecar binary, or `uv run` in dev), poll /api/health, then open a window at the
// backend (which serves the built frontend). Quit kills only a backend WE spawned.

const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const http = require("http");
const path = require("path");
const fs = require("fs");

const HOST = "127.0.0.1";        // the window + health checks always talk to loopback
// Bind the backend to all interfaces so other devices on the LAN can reach it when the
// instructor enables "Host on this network". Loopback still works for this machine's own
// window; nothing is advertised until the instructor opts in (sets instructor_url).
const BIND_HOST = "0.0.0.0";
const PORT = 8077;
const BASE = `http://${HOST}:${PORT}`;

let backendProc = null; // set only if we spawn it; if we reuse an existing server, stays null
let mainWindow = null;

function httpOk(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      res.resume();
      resolve(res.statusCode >= 200 && res.statusCode < 500);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(1000, () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForHealth(timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await httpOk(`${BASE}/api/health`)) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

// Resolve the bundled sidecar + frontend dist in a packaged build, else dev paths.
function paths() {
  const packaged = app.isPackaged;
  if (packaged) {
    // extraResources land in process.resourcesPath
    const res = process.resourcesPath;
    return {
      packaged,
      sidecar: path.join(res, "rundiff-backend", "rundiff-backend"),
      // @electron/packager copies each extraResource by basename: backend dir -> "rundiff-backend",
      // frontend "dist" -> "dist".
      frontendDist: path.join(res, "dist"),
      dataDir: path.join(app.getPath("userData"), "data"),
      backendCwd: path.join(res, "rundiff-backend"),
    };
  }
  // dev layout: webapp/desktop/.. -> webapp
  const webapp = path.resolve(__dirname, "..");
  return {
    packaged,
    backendCwd: path.join(webapp, "backend"),
    frontendDist: path.join(webapp, "frontend", "dist"),
    dataDir: null, // dev: use the repo-relative default
  };
}

function spawnBackend() {
  const p = paths();
  const env = { ...process.env, HOST: BIND_HOST, PORT: String(PORT) };
  if (p.frontendDist && fs.existsSync(p.frontendDist)) env.TUTOR_FRONTEND_DIST = p.frontendDist;
  if (p.dataDir) {
    fs.mkdirSync(p.dataDir, { recursive: true });
    env.TUTOR_DATA_DIR = p.dataDir;
  }

  let cmd, args, cwd;
  if (p.packaged) {
    cmd = p.sidecar;
    args = [];
    cwd = p.backendCwd;
  } else {
    // dev: run the uvicorn server via uv
    cmd = "uv";
    args = ["run", "uvicorn", "app:app", "--host", HOST, "--port", String(PORT)];
    cwd = p.backendCwd;
  }

  console.log(`[rundiff] spawning backend: ${cmd} ${args.join(" ")} (cwd=${cwd})`);
  backendProc = spawn(cmd, args, { cwd, env, stdio: "inherit" });
  backendProc.on("exit", (code) => {
    console.log(`[rundiff] backend exited with code ${code}`);
    backendProc = null;
  });
  backendProc.on("error", (err) => {
    console.error("[rundiff] failed to spawn backend:", err);
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Run·Diff",
    backgroundColor: "#f4f0e6", // warm-paper, so first paint isn't a white flash
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  mainWindow.loadURL(`${BASE}/practice`);
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

app.whenReady().then(async () => {
  app.setName("Run·Diff");

  // Reuse an already-running backend on :8077; otherwise spawn our own.
  const alreadyUp = await httpOk(`${BASE}/api/health`);
  if (alreadyUp) {
    console.log("[rundiff] reusing existing backend on :8077 (will not kill it on quit)");
  } else {
    spawnBackend();
  }

  const ok = await waitForHealth(30000);
  if (!ok) {
    console.error("[rundiff] backend did not become healthy within 30s");
  }
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

function killBackend() {
  if (backendProc) {
    console.log("[rundiff] killing spawned backend");
    try {
      backendProc.kill();
    } catch (_) {}
    backendProc = null;
  }
}

app.on("window-all-closed", () => {
  killBackend();
  if (process.platform !== "darwin") app.quit();
  else app.quit(); // single-window desktop app: quit on close even on mac
});

app.on("before-quit", killBackend);
app.on("quit", killBackend);
