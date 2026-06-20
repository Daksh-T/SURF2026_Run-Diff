# Run·Diff — SURF SQL Tutor

Run·Diff is a desktop SQL tutor built for the SURF 2026 project at Sewanee. A student
writes a SQL query against a practice problem; the backend **runs** the student's query
and a hidden gold query against many randomly-generated datasets and **diffs** the result
sets (hence *Run·Diff*). When they disagree, a local LLM gives a hint that points at the
mistake without leaking the answer. Instructors author their own problems through an
in-app authoring flow.

> **This is the public "empty-set" build.** It ships with **no built-in problems** — the
> original CS284-derived exercises and their gold answers are intentionally omitted so
> answers aren't exposed. The app is fully functional: instructors create their own
> problem sets at runtime. See [Authoring problems](#authoring-problems).

---

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

- **Backend** (`webapp/backend`) — FastAPI app. The grading core, tutor harness, and
  data populator live in the sibling `tutor/`, `populator/`, and `eval/src/` trees and are
  wired onto `sys.path` at import time, so the repo layout must be preserved.
- **Frontend** (`webapp/frontend`) — React + Vite (CodeMirror SQL editor). Built to static
  assets that the backend serves.
- **Desktop shells** — `webapp/desktop-macos` (native Swift `WKWebView`, used for the macOS
  build) and `webapp/desktop` (Electron, used for the Linux build). Both bundle the backend
  as a self-contained PyInstaller sidecar plus the built frontend.

### Repo layout

```
webapp/
  backend/        FastAPI app + PyInstaller spec (rundiff_backend.spec)
  frontend/       React + Vite app
  desktop-macos/  Swift WKWebView shell + build.sh        (macOS builds)
  desktop/        Electron shell + electron-builder config (Linux builds)
  branding/       app icons
tutor/            grading core, tutor harness          (imported by the backend)
populator/        dataset generator + problem bank      (imported by the backend)
eval/src/         LLM provider layer (Groq + Ollama)    (imported by the backend)
```

---

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — Python toolchain (the backend targets Python ≥ 3.11).
- **[Bun](https://bun.sh/)** — used to install and build the frontend.
- **[Ollama](https://ollama.com/)** *(student hints)* — serves the local hint model
  `qwen2.5-coder:7b`. The app can pull it on first run, or: `ollama pull qwen2.5-coder:7b`.
- **Groq API key** *(instructor authoring only)* — authoring defaults to Groq. Put it in
  `webapp/backend/.env` as `groq_api_key=...`. Not needed to run, grade, or take problems.

macOS desktop builds additionally need the **Xcode Command Line Tools** (`xcode-select --install`)
for `swiftc`. Linux desktop builds need **Node.js** (electron-builder runs under Node).

---

## Running in development

Two terminals — backend (`:8077`) and frontend dev server (`:5180`, proxies `/api` to the backend):

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

Student view is at `/practice`; the instructor authoring view is also served by the app.

To run the whole desktop shell in dev (Electron spawns the backend via `uv run` for you):

```bash
cd webapp/desktop
bun install
bun run dev
```

---

## Building the desktop app

Every build produces the same two ingredients first, then wraps them in a platform shell:

| Ingredient | Command (from `webapp/desktop`) | Output |
| --- | --- | --- |
| Built frontend | `bun run build:frontend` | `webapp/frontend/dist/` |
| Backend sidecar (PyInstaller) | `bun run build:backend` | `webapp/backend/dist_backend/rundiff-backend/` |

> The PyInstaller sidecar is platform- and arch-native — it cannot be cross-compiled. Build
> each target on its own OS/arch (which is exactly what CI does).

### macOS (`.app` + `.dmg`, native WKWebView)

```bash
cd webapp/desktop && bun install
bun run build:frontend
bun run build:backend
cd ../desktop-macos
ARCH=arm64 ./build.sh --dmg     # or ARCH=x86_64 for an Intel build
# → webapp/desktop-macos/release/Run·Diff.app and Run·Diff.dmg
```

Builds are **unsigned** (ad-hoc codesigned). On first launch, right-click the app → *Open*,
or clear quarantine: `xattr -dr com.apple.quarantine "Run·Diff.app"`.

### Linux (`AppImage`, Electron)

```bash
cd webapp/desktop && npm install      # electron-builder runs under Node, not Bun
npm run build:frontend
npm run build:backend
npm run dist                          # electron-builder, linux target
# → webapp/desktop/release/*.AppImage
```

---

## Releases & CI

GitHub Actions (`.github/workflows/release.yml`) builds and publishes the desktop app for:

- **macOS arm64** (Apple Silicon) — `.dmg`
- **macOS x86_64** (Intel) — `.dmg`
- **Linux x86_64** — `.AppImage`

**A release is cut on every merged pull request.** The workflow triggers when a PR is
merged into `main`, and:

- the **release name** is the **PR title**,
- the **release notes** are the **PR description**,
- the **tag** is `v<YYYY.MM.DD>.<run_number>` (date of the run plus the workflow run number,
  so tags always increase).

The first/initial release was produced by manually dispatching the same workflow
(`workflow_dispatch`), which is also handy for re-cutting a build on demand.

---

## Authoring problems

Because this is the empty-set build, start by creating problems:

1. Open the app (or the dev frontend) and go to the instructor authoring view.
2. Set the author password on first use (stored hashed in `webapp/data/config.json`).
3. Create a set, then add problems — provide a prompt and a gold SQL query; the populator
   generates varied datasets and verifies the gold query exercises the intended clauses.
4. Publish the set; students select it from the practice view.

Instructor-authored content lives under `webapp/data/` (git-ignored runtime data) and the
Groq-backed authoring flow (`groq_api_key` in `webapp/backend/.env`).

---

## License

This repository is an academic project artifact. No open-source license is granted; all
rights reserved unless stated otherwise.
