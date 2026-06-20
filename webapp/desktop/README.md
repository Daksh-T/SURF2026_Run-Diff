# Run·Diff — desktop app

The SURF SQL tutor packaged as a single-process desktop app: an Electron shell points a window
at the FastAPI backend, which serves the built React frontend and exposes the `/api/*` surface.
There is **no bundled Ollama** — instead the app detects Ollama and walks the user through a
one-time install + model pull (the `/setup` flow). The app works without it; live hints just fall
back to the deterministic offline templates until setup is done.

## Prerequisites

- `bun` (package manager + electron/electron-builder runner)
- `uv` (drives the backend; PyInstaller runs via `uv run --with pyinstaller`)
- macOS arm64 (the build below targets `darwin/arm64`)
- `bun install` once in this directory

## Build (three steps)

```bash
# 1. build the frontend  ->  ../frontend/dist
bun run build:frontend

# 2. build the backend sidecar (PyInstaller, onedir)  ->  ../backend/dist_backend/rundiff-backend/
bun run build:backend

# 3. package the .app (unsigned)  ->  release/Run·Diff-darwin-arm64/Run·Diff.app
bun pack.mjs
```

Run all three in order; step 3 carries the outputs of 1 and 2 into the bundle as resources.

### Dev (no packaging)

```bash
bun run dev      # launches Electron against `uv run uvicorn ...` (or reuses an existing :8077)
```

In dev the shell reuses a backend already running on `:8077` and will **not** kill it on quit;
otherwise it spawns one with `uv` and tears it down on quit.

## Artifact + where things live

- **Artifact:** `release/Run·Diff-darwin-arm64/Run·Diff.app` (~268 MB; unsigned — see below).
  The PyInstaller sidecar is ~34 MB; the rest is the Electron runtime.
- **User data:** `~/Library/Application Support/Run·Diff/data/` (sets, bundles, classes, attempts,
  config.json). The backend honors `TUTOR_DATA_DIR`, which the shell points there in the packaged
  app, so **nothing is ever written inside the app bundle**.
- **Bundled frontend:** `Contents/Resources/dist/`, passed to the backend via `TUTOR_FRONTEND_DIST`.
- **Bundled sidecar:** `Contents/Resources/rundiff-backend/rundiff-backend`.

## Packaging notes

- **electron-builder does not run on this machine.** `node` here is a bun shim; electron-builder's
  `source-map-support`/`bluebird` stack crashes before reaching build logic. We ship via
  `@electron/packager` (`pack.mjs`) instead, which produces an **unsigned** `.app`. The
  `electron-builder` config + `dist` script are kept in `package.json` for a real-Node machine
  (it would emit a dmg); on a machine with a genuine Node + Developer ID, `bun run dist` should work.
- **Unsigned:** no Developer ID identity is configured, so the `.app` is unsigned. First launch
  needs right-click → Open (or `xattr -dr com.apple.quarantine` on the `.app`).
- **PyInstaller wrinkle — sibling source trees.** The backend reaches into `../tutor`,
  `../populator`, and `../eval/src` at runtime via `sys.path.insert(Path(__file__).parents[2]/...)`.
  Those inserts are dead in a bundle, so `rundiff_backend.spec` adds the same dirs to `pathex` and
  lists their dynamically-imported modules (grader, harness, model, populate, instructor_flow,
  edge_coverage, providers, predictor, sim_student_eval, problems.bank) as `hiddenimports`, plus
  `collect_submodules("uvicorn")` for uvicorn's dynamic loop/protocol/logging imports. The student
  grade path execs generator source embedded in each bundle JSON (`load_populate`), so no populator
  generator *files* are needed at runtime. A missing `.env` in the bundle is fine — only the
  instructor (Groq) authoring path reads it, never the student app.

## Explored and rejected

- **Bundling Ollama into the app** — adds gigabytes, plus model-license and self-update headaches;
  guided install + pull is leaner and keeps Ollama updating itself.
- **Hosted frontend + local backend** — CORS / mixed-content friction (https page → http
  localhost); serving the built frontend from the same backend process avoids it entirely.
