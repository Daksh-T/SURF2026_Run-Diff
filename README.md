# Run·Diff — a local-first SQL tutor

Run·Diff is a desktop SQL practice and classroom app. It checks a student's SQL by running the
student query and the instructor's expected query against multiple generated SQLite databases,
then comparing the results. When an answer is wrong, the app provides an adaptive three-step hint
ladder that points toward the problem without handing over a solution.

The same app supports both roles:

- **Students** join a classroom, work through one or more assigned sets, run queries or
  data-changing statements, request hints, and sync or export their attempts.
- **Instructors** author individual problems or multi-section assignments, publish student-safe
  sets, run scheduled or live classroom sessions, and review class, problem, and student insights.

For installation and day-to-day use, start with the [User guide](USER_GUIDE.md). For every setting,
environment variable, model choice, data location, and build option, use the
[Configuration reference](CONFIGURATION.md).

## What the app includes

- Result-based grading across multiple generated databases, including ordering and optional exact
  result-column-name checks.
- Final-state grading for `CREATE`, `INSERT`, `UPDATE`, `DELETE`, and `DROP` problems, including
  tables, rows, constraints, generated columns, views, indexes, and triggers.
- An error-adaptive hint ladder using deterministic evidence and local model-written questions or
  nudges. If Ollama or the model is unavailable, safe built-in hints keep practice usable.
- Single-problem and whole-assignment authoring, generated-data stress testing, edge-case
  confirmations, previews, and optional difficulty prediction.
- Published, student-safe bundles that contain baked expected results but never the instructor's
  gold SQL.
- Classrooms with multiple sets, open/roster/personal-passcode entry, optional schedules,
  archive/reactivate controls, and live pause/end/reopen controls.
- Offline assignment and attempt files, plus optional live synchronization over a LAN.
- Insights by class, set, problem, and student; live activity; predicted-versus-actual difficulty;
  and CSV export.
- Native macOS packaging and lean Tauri packages for Windows and Linux. An Electron shell remains
  available as an alternate development and packaging path.

## How Run·Diff works

```text
Desktop shell or browser
        │ HTTP on localhost:8077
        ▼
FastAPI backend ───── serves the built React app
        │
        ├── SQLite grading: generated databases + baked expected results
        ├── Ollama: optional local student-hint model
        └── Groq: default instructor-authoring model
```

The React frontend never grades SQL itself. The FastAPI backend owns grading, authoring, classes,
attempt logs, publishing, and synchronization. Packaged desktop shells start that backend as a
PyInstaller sidecar and open the app at `http://127.0.0.1:8077`.

### Privacy and content boundaries

- Student grading is local. Student SQL is sent only to the backend running on that student's
  machine, unless an attempt is synchronized to the instructor as part of the classroom log.
- Student hints use Ollama by default. They fall back to built-in templates when the local model
  cannot be reached.
- Default authoring uses Groq and therefore requires internet access and a Groq API key. Authoring
  sends instructor-authored prompts, schemas, and expected SQL—not student attempts.
- Private source sets live separately from published bundles. Publishing bakes expected results
  and verifies that no `gold_sql` field enters the student bundle.
- When class hosting is enabled, student sync endpoints are reachable on the network. Instructor
  endpoints remain loopback-only unless remote administration is explicitly enabled.

Exported assignment files are sealed against casual reading, not cryptographically protected
against a determined recipient. They do not contain gold SQL, but they necessarily contain the
baked results used for local grading. Prefer a live class server for higher-stakes use.

## Repository layout

```text
webapp/
  backend/          FastAPI API, persistence, grading bridge, authoring, publishing
  frontend/         React + Vite interface
  desktop-macos/    native Swift/WKWebView shell for macOS
  desktop-tauri/    Tauri shell for Windows and Linux
  desktop/          alternate Electron shell and shared build scripts
  branding/         icons and branding assets
  network_sync_guide.md
tutor/              result grading, hint generation, and evaluation harnesses
populator/          generated-dataset authoring and model registry
eval/src/           Groq, Gemini, and Ollama provider layer
```

The backend imports the sibling `tutor/`, `populator/`, and `eval/src/` trees. Preserve the
repository layout when running from source.

## Run from source

Prerequisites:

- [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer
- [Bun](https://bun.sh/)
- Optional: [Ollama](https://ollama.com/) for model-written student hints
- Optional: a Groq API key for instructor authoring

Run the backend and frontend in separate terminals:

```bash
cd webapp/backend
uv sync
uv run uvicorn app:app --host 127.0.0.1 --port 8077
```

```bash
cd webapp/frontend
bun install
bun run dev
```

Open `http://127.0.0.1:5180`. Vite proxies `/api` requests to the backend on port `8077`.

To serve a production-style frontend from the backend instead:

```bash
cd webapp/frontend
bun install
bun run build

cd ../backend
uv sync
uv run python run_server.py
```

Then open `http://127.0.0.1:8077`.

The default authoring provider needs `groq_api_key` in a repo-root `.env` file or in the process
environment. Student practice, grading, and offline hints do not need that key. See
[Configuration](CONFIGURATION.md#api-keys-and-env) for an exact example.

## Validate a change

```bash
cd webapp/backend
uv sync
uv run python -m unittest

cd ../frontend
bun install
bun run build
```

The backend test suite exercises grading and state-comparison behavior. A production frontend
build catches JSX, import, and bundling failures.

## Desktop builds

Release automation builds:

| Platform | Primary shell | Output |
| --- | --- | --- |
| macOS arm64 | Swift + WKWebView | `.dmg` |
| Windows x86-64 | Tauri + WebView2 | `.msi` and setup `.exe` |
| Linux x86-64 | Tauri + WebKitGTK | `.AppImage` |

All desktop targets bundle the same built frontend and platform-native PyInstaller backend. The
sidecar cannot be cross-compiled, so build it on the target operating system. Platform-specific
instructions live in [desktop-macos/README.md](webapp/desktop-macos/README.md),
[desktop-tauri/README.md](webapp/desktop-tauri/README.md), and
[desktop/README.md](webapp/desktop/README.md). Build flags and output paths are summarized in the
[Configuration reference](CONFIGURATION.md#build-and-development-settings).

## Documentation map

| Document | Use it for |
| --- | --- |
| [User guide](USER_GUIDE.md) | installation, first run, student practice, authoring, classrooms, sync, insights, and troubleshooting |
| [Configuration reference](CONFIGURATION.md) | environment variables, API keys, persistent settings, data files, models, networking, and build options |
| [Network sync guide](webapp/network_sync_guide.md) | a focused LAN and offline-file deployment walkthrough |
| Platform READMEs | building and packaging a specific desktop shell |

## License

Academic project artifact. No open-source license is granted; all rights reserved.
