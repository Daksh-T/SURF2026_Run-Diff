# Run·Diff — Configuration reference

Technical reference for every environment variable, persisted setting, and build-time flag the
app reads. For *using* the app see `webapp/user_guide.md`; for hosting/network sync see
`webapp/network_sync_guide.md`; for the architecture see `README.md`.

> Scope: this covers the **app** (backend runtime, desktop shells, builds). Configuration for
> the offline **research/CLI tooling** under `populator/` and `tutor/` is intentionally left out
> for now and will be added once that pipeline settles.

Quick map of where configuration comes from:

| Layer | Mechanism | Who sets it |
| --- | --- | --- |
| Backend runtime | environment variables | you (dev), or the desktop shell (packaged) |
| API keys | repo-root `.env` (or env vars) | you |
| Per-install settings | `<data>/config.json` | the Author UI / API at runtime |
| Desktop shells | hardcoded constants + env passed to the sidecar | the shell |
| Builds | env vars + script flags | you / CI |

---

## 1. Backend runtime environment variables

Read by the FastAPI backend (`webapp/backend/`). In dev you export these before
`uv run uvicorn app:app`; in a packaged build the desktop shell sets them when it spawns the
sidecar (see §4). All are optional — every one has a default.

| Variable | Default | Read in | What it does |
| --- | --- | --- | --- |
| `HOST` | `127.0.0.1` | `run_server.py` | Bind address for the server. The desktop shells set this to `0.0.0.0` so students can reach the class server over the LAN. |
| `PORT` | `8077` | `run_server.py`, `app.py` | TCP port. Also reported to students by `/api/instructor/host-info`. |
| `TUTOR_DATA_DIR` | repo `webapp/data/` | `store.py` | Writable data root for all persisted state (sets, bundles, classes, attempts, `config.json`). `config.py` and `classes.py` derive their paths from it, so this one switch relocates everything. Packaged builds point it at a per-user app dir. |
| `TUTOR_FRONTEND_DIST` | `../frontend/dist` | `static.py` | Directory of the built frontend the backend serves. If unset and the default dir is absent, the backend runs API-only (the dev path, where Vite serves the UI). |
| `TUTOR_HINT_MODEL` | `qwen7b` | `app.py`, `setup_ollama.py` | Friendly name (see §5) of the **local** model used for L1/L2 hints. Resolved to an Ollama tag for status/pull. |
| `TUTOR_AUTHOR_MODEL` | `groq` | `app.py` | Friendly name (see §5) of the model used for **authoring** (schema inference, generator synthesis). Cloud is fine here — authoring sees no student data. |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | `setup_ollama.py`, `eval/src/providers.py` | Base URL of the Ollama server. Used for first-run detection/pull **and** for actually generating hints, so a non-default address works end to end. |
| `RUNDIFF_ALLOW_REMOTE_ADMIN` | unset (off) | `app.py` | Opt in to reaching the **instructor/admin** API (`/api/instructor/*`, `/api/auth/set`, `/api/auth/clear`) from another device. Off by default these are **local-only** (loopback). Accepts `1`/`true`/`yes`/`on`. See §6. |

### Notes
- **`HINT_MODEL` vs `AUTHOR_MODEL`.** Hints must stay local (student data never leaves the
  machine), so `TUTOR_HINT_MODEL` should be an Ollama model. Authoring may use a cloud model.
- **Difficulty prediction models are fixed**, not env-configurable: `predictor.py` hardcodes the
  simulated student (`qwen2.5-coder:1.5b`) and tutor (`qwen2.5-coder:7b`).
- With no hint model installed, hints fall back to deterministic offline templates — the app is
  fully usable; only model-written L1/L2 phrasing is missing.

---

## 2. API keys — repo-root `.env`

`eval/src/providers.py` calls `load_dotenv(<repo-root>/.env)` at import (and the webapp imports
this transitively for authoring). So put cloud keys in the **repo-root `.env`** — i.e.
`/.env`, next to this file — or export them as real environment variables (either works; an
exported var wins over `.env`).

| Key | Used by | Needed for |
| --- | --- | --- |
| `groq_api_key` | `providers.gen_groq` | Instructor **authoring** (the default `groq` author model). Not needed to run, grade, take problems, or get hints. |
| `aistudio_api_key` | `providers.gen_gemini` | Google AI Studio / Gemini models — only if you select a Gemini model (research/eval; not used by the default webapp flow). |

```
# /.env  (repo root, gitignored)
groq_api_key=gsk_...
# aistudio_api_key=...   # only if using Gemini
```

> Note: `.env` is gitignored. Never commit keys.

---

## 3. Persisted per-install settings — `<data>/config.json`

Not environment variables, but the other half of configuration. Stored in
`config.json` under `TUTOR_DATA_DIR`, managed at runtime through the Author UI (or the
`/api/auth/*` and `/api/instructor/config` endpoints). You normally never edit it by hand.

| Field | Default | Set via | Meaning |
| --- | --- | --- | --- |
| `author_password_sha256` | `null` | Author lock control / `POST /api/auth/set` | SHA-256 of the author password. `null` = authoring is open (single-user). When set, every `/api/instructor/*` request must carry a matching `X-Author-Key`. |
| `instructor_url` | `null` | Author → Classes sync panel / `PATCH /api/instructor/config` | This machine's publicly reachable address. When set ("Host on this network"), it is baked into exported assignment files and enables live attempt sync; it also flips the LAN-facing student endpoints on (see §6). |

---

## 4. Desktop shells

Both shells (`webapp/desktop/main.js` Electron, `webapp/desktop-macos/RunDiff.swift` WKWebView)
use the same hardcoded constants and pass env to the backend sidecar they spawn.

| Constant | Value | Why |
| --- | --- | --- |
| `HOST` (window + health checks) | `127.0.0.1` | The window always talks to loopback. |
| `BIND_HOST` (sidecar bind) | `0.0.0.0` | So LAN devices can reach the class server when the instructor enables hosting. Nothing is advertised until `instructor_url` is set, and the admin API stays local-only (§6). |
| `PORT` | `8077` | Fixed port for the window and sidecar. |

Env the shells set on the spawned backend: `HOST=0.0.0.0`, `PORT=8077`, `TUTOR_DATA_DIR` (a
per-user writable dir), and `TUTOR_FRONTEND_DIST` (the bundled `dist`). If a backend is already
healthy on `:8077`, the shell reuses it and does **not** kill it on quit.

---

## 5. Model names (values for `TUTOR_HINT_MODEL` / `TUTOR_AUTHOR_MODEL`)

Friendly names resolve through `populator/model.py::REGISTRY`:

| Name | Provider | Model |
| --- | --- | --- |
| `groq` | Groq (cloud) | `llama-3.3-70b-versatile` |
| `gptoss` | Groq (cloud) | `openai/gpt-oss-120b` |
| `qwen1.5b` | Ollama (local) | `qwen2.5-coder:1.5b` |
| `qwen7b` | Ollama (local) | `qwen2.5-coder:7b` *(default hint model)* |
| `qwen14b` | Ollama (local) | `qwen2.5-coder:14b` |
| `qwen32b` | Ollama (local) | `qwen2.5-coder:32b` |
| `qwen3coder` | Ollama (local) | `qwen3-coder:30b` |

A cloud name needs the matching API key (§2); a local name needs Ollama serving that tag (§1).

---

## 6. Network exposure & the admin gate

When the backend binds to `0.0.0.0` (desktop default), endpoints fall into three tiers:

- **Student/sync endpoints** (`/api/student/*`, `/api/sync/attempts`) — reachable on the LAN
  *by design*, but the LAN-facing ones (`fetch-assignment`, attempt ingest, proxied
  class-status) only answer when **hosting is on** (`instructor_url` set). Turning hosting off
  makes the class server go dark to other devices.
- **Instructor/admin endpoints** (`/api/instructor/*`, `/api/auth/set`, `/api/auth/clear`) —
  **local-only by default.** They answer only requests from loopback (`127.0.0.1`/`::1`), since
  the Author UI and the dev proxy are the only legitimate callers. This holds even with no
  author password set, so a LAN peer cannot drive authoring/publishing/class management.
  - Set `RUNDIFF_ALLOW_REMOTE_ADMIN=1` to allow the admin API from other devices — only for the
    rare headless self-host where you drive the Author UI from another machine's browser. Pair
    it with an author password.
- **Author password** (`config.json` → `author_password_sha256`) — an additional gate on
  `/api/instructor/*` regardless of origin; recommended on any shared machine.

> The backend trusts proxy headers only from loopback (uvicorn default), so a remote peer cannot
> spoof `X-Forwarded-For` to look local.

---

## 7. Build-time configuration

### Frontend (Vite)
- Build: `bun run build` in `webapp/frontend/` (or `bun run build:frontend` from
  `webapp/desktop/`) → `webapp/frontend/dist/`.
- Dev server: port **5180**, proxies `/api/*` → `http://127.0.0.1:8077` (`vite.config.js`).

### Backend sidecar (PyInstaller)
- `bun run build:backend` from `webapp/desktop/`:
  `pyinstaller rundiff_backend.spec --distpath dist_backend --workpath build_backend`
  → `webapp/backend/dist_backend/rundiff-backend/`. Platform/arch-native; build on the target OS.

### Electron packaging (Linux / cross)
- `electron-builder` via `bun run dist` (`package.json` `build`): appId
  `edu.sewanee.surf.rundiff`, targets **AppImage** (Linux) and **dmg** (mac). Bundles the
  sidecar and `dist` as `extraResources`.
- `pack.mjs` (`@electron/packager`) is an alternate macOS path (`darwin`/`arm64`) used where
  electron-builder won't run.

### macOS native shell (`webapp/desktop-macos/build.sh`)
| Flag / env | Default | Effect |
| --- | --- | --- |
| `ARCH` (env) | `arm64` | Target CPU for the Swift shell. `ARCH=x86_64` builds Intel. |
| `VERSION` (env) | `0.1.0` | `CFBundleVersion` / `CFBundleShortVersionString`. |
| `--dmg` (arg) | off | Also emit a compressed LZMA `.dmg` installer. |

Both desktop builds consume the same two ingredients first: the built frontend (`dist/`) and the
PyInstaller sidecar (`dist_backend/rundiff-backend/`).
