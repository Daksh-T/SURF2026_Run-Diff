# Run·Diff — SURF SQL Tutor

Run·Diff is a desktop SQL tutor. A student writes a SQL query against a practice problem;
the backend **runs** the student's query and a hidden gold query against many
randomly-generated datasets and **diffs** the result sets (hence *Run·Diff*). When they
disagree, a local LLM gives a hint that points at the mistake without giving away the
answer. Instructors author their own problems through an in-app authoring flow.

## How it works

```
┌─────────────────────────────┐         ┌──────────────────────────────┐
│  Desktop shell              │  HTTP   │  FastAPI backend (:8077)     │
│  • macOS: Swift WKWebView   │ ──────► │  • grades by running + diff  │
│  • Linux: Electron          │         │  • serves the built frontend │
│  (spawns + supervises the   │         │  • instructor authoring flow │
│   backend, opens a window)  │         └──────────────┬───────────────┘
└─────────────────────────────┘                        │
                                       ┌────────────────┴───────────────┐
                                       │  Ollama (local)  → student hints│
                                       │  Groq (cloud)    → authoring    │
                                       └─────────────────────────────────┘
```

- **Backend** (`webapp/backend`) — FastAPI. The grading core, tutor harness, and data
  populator live in the sibling `tutor/`, `populator/`, and `eval/src/` trees and are wired
  onto `sys.path` at import time, so the repo layout must be preserved.
- **Frontend** (`webapp/frontend`) — React + Vite, with a CodeMirror SQL editor. Built to
  static assets that the backend serves.
- **Desktop shells** — `webapp/desktop-macos` (native Swift `WKWebView`) for macOS and
  `webapp/desktop` (Electron) for Linux. Both bundle the backend as a self-contained
  PyInstaller sidecar plus the built frontend.

### Repo layout

```
webapp/
  backend/        FastAPI app + PyInstaller spec (rundiff_backend.spec)
  frontend/       React + Vite app
  desktop-macos/  Swift WKWebView shell + build.sh         (macOS builds)
  desktop/        Electron shell + electron-builder config (Linux builds)
  branding/       app icons
tutor/            grading core, tutor harness          (imported by the backend)
populator/        dataset generator                    (imported by the backend)
eval/src/         LLM provider layer (Groq + Ollama)    (imported by the backend)
```

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — Python toolchain (backend targets Python ≥ 3.11).
- **[Bun](https://bun.sh/)** — installs and builds the frontend.
- **[Ollama](https://ollama.com/)** *(student hints)* — serves the local hint model
  `qwen2.5-coder:7b`. The app can pull it on first run, or `ollama pull qwen2.5-coder:7b`.
- **Groq API key** *(instructor authoring only)* — put it in the repo-root `.env` as
  `groq_api_key=...` (or export it as an environment variable). Not needed to run, grade, or
  take problems. See `CONFIGURATION.md` for all configuration.

macOS desktop builds also need the **Xcode Command Line Tools** (`xcode-select --install`)
for `swiftc`. Linux desktop builds need **Node.js** (electron-builder runs under Node).

## Running in development

Two terminals — backend on `:8077`, frontend dev server on `:5180` (proxies `/api` to the backend):

```bash
# 1) backend
cd webapp/backend
uv sync
uv run uvicorn app:app --host 127.0.0.1 --port 8077

# 2) frontend
cd webapp/frontend
bun install
bun run dev          # open http://127.0.0.1:5180
```

To run the whole desktop shell in dev (Electron spawns the backend for you):

```bash
cd webapp/desktop
bun install
bun run dev
```

## Building the desktop app

Prebuilt installers for each platform are attached to the
[Releases](../../releases) page. To build locally:

Every build first produces the same two ingredients, then wraps them in a platform shell:

| Ingredient | Command (from `webapp/desktop`) | Output |
| --- | --- | --- |
| Built frontend | `bun run build:frontend` | `webapp/frontend/dist/` |
| Backend sidecar (PyInstaller) | `bun run build:backend` | `webapp/backend/dist_backend/rundiff-backend/` |

The PyInstaller sidecar is platform- and arch-native and cannot be cross-compiled — build
each target on its own OS/arch.

### macOS (`.app` + `.dmg`, native WKWebView)

```bash
cd webapp/desktop && bun install
bun run build:frontend
bun run build:backend
cd ../desktop-macos
ARCH=arm64 ./build.sh --dmg     # or ARCH=x86_64 for an Intel build
# → webapp/desktop-macos/release/Run·Diff.app and Run·Diff.dmg
```

Builds are unsigned. On first launch, right-click the app → *Open*, or
`xattr -dr com.apple.quarantine "Run·Diff.app"`.

### Linux (`AppImage`, Electron)

```bash
cd webapp/desktop && npm install      # electron-builder runs under Node, not Bun
npm run build:frontend
npm run build:backend
npm run dist                          # → webapp/desktop/release/*.AppImage
```

## Authoring problems

The app ships with no built-in problems — instructors create their own:

1. Open the app and go to the instructor authoring view.
2. Set the author password on first use.
3. Create a set, then add problems — a prompt plus a gold SQL query. The populator
   generates varied datasets and verifies the gold query exercises the intended clauses.
4. Publish the set; students select it from the practice view.

Authored content lives under `webapp/data/` and uses the Groq-backed authoring flow
(`groq_api_key` in the repo-root `.env`, or exported as an environment variable).

## License

Academic project artifact. No open-source license is granted; all rights reserved.
