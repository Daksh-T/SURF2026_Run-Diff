# Run·Diff configuration reference

This is the authoritative reference for settings read by the Run·Diff app and its build tools.
For a guided installation and normal student or instructor workflows, use the
[User guide](USER_GUIDE.md). For architecture and developer orientation, use the
[README](README.md).

Most users do not need to edit configuration files. The app's Author interface manages the
author password and class-server address. Environment variables are mainly for source installs,
custom model hosts, and self-hosted deployments.

## Configuration at a glance

| Type | Where it is set | Typical owner |
| --- | --- | --- |
| Backend runtime | environment variables before the backend starts | developer or administrator |
| Cloud credentials | repo-root `.env` or environment variables | instructor or administrator |
| App settings | `<data>/config.json`, normally changed in the Author UI | instructor |
| Classroom settings | `<data>/classes/*.json`, changed in Author → Classes | instructor |
| Desktop runtime | constants and environment set by the desktop shell | application build |
| Build settings | script flags and environment variables | developer or CI |

Runtime values are read when the backend process starts. Restart Run·Diff after changing an
environment variable. Values saved through the app take effect immediately unless noted.

## Backend environment variables

All variables are optional.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Address used by `webapp/backend/run_server.py`. Use `0.0.0.0` to listen on every interface. Packaged desktop shells set this to `0.0.0.0`; their own window still connects over loopback. |
| `PORT` | `8077` | Backend port. It is also used when the app reports possible LAN addresses. The packaged shells and their windows are fixed to `8077`, so changing it is supported for source/server runs, not for an unmodified packaged shell. |
| `TUTOR_DATA_DIR` | `webapp/data/` | Writable root for private sets, published bundles, classrooms, attempts, and `config.json`. Relative paths are resolved from the backend process's working directory; an absolute path is safer. |
| `TUTOR_FRONTEND_DIST` | `webapp/frontend/dist/` | Directory containing a built Vite app, including `index.html`. If neither this path nor the default build exists, the backend runs API-only and Vite should serve the UI. |
| `TUTOR_HINT_MODEL` | `qwen7b` | Friendly model-registry name used for student hints. The default resolves to Ollama's `qwen2.5-coder:7b`. A raw Ollama tag is recognized by first-run setup only; normal hint generation expects a registry name, so use a listed name below. |
| `TUTOR_AUTHOR_MODEL` | `groq` | Friendly model-registry name used for schema inference and generated-data authoring. The default resolves to Groq's `qwen/qwen3.6-27b`. |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama base URL used both for setup/status/model pulls and actual model calls. Do not include `/api/chat`. |
| `RUNDIFF_ALLOW_REMOTE_ADMIN` | unset / false | Allows `/api/instructor/*`, `/api/auth/set`, and `/api/auth/clear` from non-loopback clients. Accepted true values are `1`, `true`, `yes`, and `on` (case-insensitive). See [Network exposure](#network-exposure-and-security). |

Example source-server configuration:

```bash
export HOST=0.0.0.0
export PORT=8077
export TUTOR_DATA_DIR="$HOME/rundiff-data"
export TUTOR_HINT_MODEL=qwen7b
export TUTOR_AUTHOR_MODEL=groq
uv run python run_server.py
```

Use this from `webapp/backend/`. `uv run uvicorn app:app ...` is also valid, but Uvicorn's CLI
flags determine its host and port; setting `HOST` alone does not override an explicit
`--host` value.

### Important model behavior

- Grading never requires a language model.
- If the hint model fails or is unavailable, the backend returns deterministic built-in hints.
- Difficulty prediction does not follow `TUTOR_HINT_MODEL`: it currently fixes the simulated
  student to `qwen2.5-coder:1.5b` and the tutor to the `qwen7b` registry entry.
- The first-run setup page can pull only an Ollama-backed hint model. Do not configure a cloud
  registry name as `TUTOR_HINT_MODEL` if you want the privacy and setup behavior promised by the
  app.

## API keys and `.env`

The provider layer loads `.env` from the repository root—the directory containing this file.
Real environment variables take precedence over values loaded from `.env`.

```dotenv
# .env at the repository root
groq_api_key=gsk_...
# aistudio_api_key=...  # only for Gemini research/provider calls
```

| Key | Required when | Not required for |
| --- | --- | --- |
| `groq_api_key` | `TUTOR_AUTHOR_MODEL` selects `groq`, `groq-llama`, or `gptoss` | installing the app, joining a class, grading, local hints, publishing an already-authored set |
| `aistudio_api_key` | calling a Gemini model through `eval/src/providers.py` | the default web app, whose app model registry has no Gemini entry |

The variable names are intentionally lowercase because that is what the code reads. `.env` is
ignored by Git. Never commit credentials or include them in an assignment export.

In a packaged desktop build there is no repo-root `.env` beside the source tree. Launch the app
from an environment containing the key, or configure the key in whatever process manager starts
a custom backend. A normal student installation needs no cloud key.

## Persistent application settings

`<data>` means the directory selected by `TUTOR_DATA_DIR`. The app stores these two global
settings in `<data>/config.json`:

| Field | Default | UI control | Meaning |
| --- | --- | --- | --- |
| `author_password_sha256` | `null` | Author toolbar → Set/Change/Remove password | SHA-256 digest of the author password. The plaintext is not stored. `null` means the local Author area is open. |
| `instructor_url` | `null` | Author → Classes → Host on this network / Enter URL manually / Turn off | Publicly reachable base URL for this class server, such as `http://192.168.1.5:8077`. A non-empty value enables LAN assignment fetch and attempt ingest and is embedded in exported assignments. |

Example shape—not a recommended way to set a password:

```json
{
  "author_password_sha256": null,
  "instructor_url": "http://192.168.1.5:8077"
}
```

Prefer the UI. Manually entering a plaintext password in this field will not work because the app
compares SHA-256 digests.

### Classroom settings

Classroom records are managed through Author → Classes rather than a global configuration file.
Each record can contain:

- one or more published set IDs;
- `open`, `roster`, or `passcode` entry mode;
- a shared three-word class passphrase and, in passcode mode, one personal code per roster name;
- an optional opening and closing time, stored as UTC ISO timestamps;
- active or archived status; and
- live session state: `running`, `paused`, or `ended`.

Changing a classroom's roster in passcode mode preserves codes for unchanged names and creates
codes for new names. Its ID and shared passphrase are immutable through the UI.

## Data directory and backups

The data root contains:

```text
<data>/
  config.json             global password digest and class-server URL
  sets/<set-id>.json      private authoring source, including gold SQL
  bundles/<set-id>.json   published student-safe grading bundle
  classes/<class-id>.json classroom configuration and credentials
  attempts/<class-id>.jsonl
                          grade and hint events, one JSON record per line
```

Default locations:

| Runtime | Data location |
| --- | --- |
| Source checkout | `webapp/data/` |
| Native macOS app | `~/Library/Application Support/RunDiff/data/` |
| Electron packaged app on macOS | Electron's Run·Diff user-data directory, typically `~/Library/Application Support/Run·Diff/data/` |
| Tauri Windows/Linux app | the operating system's app-local-data directory for `edu.sewanee.surf.rundiff`, under `data/` |

The exact Tauri parent path follows the operating system. Set `TUTOR_DATA_DIR` on a source/server
deployment when you need a predictable location.

To back up an installation, close Run·Diff and copy the entire data directory. Copying only
`sets/` loses published bundles and classrooms; copying only `attempts/` loses the class records
needed to interpret them. Treat this backup as sensitive because source sets contain expected SQL,
class files contain join credentials, and attempt logs contain student names and submitted SQL.

Deleting a classroom removes its active class record and renames its attempt log to
`<class-id>.deleted-<UTC timestamp>.jsonl`. The UI no longer shows its insights. Deleting a student
or an individual attempt rewrites the active attempt log and is permanent. Deleting a set removes
its source and published bundle and is blocked while a classroom still assigns it.

## Model registry

`TUTOR_HINT_MODEL` and `TUTOR_AUTHOR_MODEL` use names from `populator/model.py`:

| Name | Provider | Concrete model | Intended use |
| --- | --- | --- | --- |
| `groq` | Groq | `qwen/qwen3.6-27b` | default cloud authoring model |
| `groq-llama` | Groq | `llama-3.3-70b-versatile` | deprecated comparison alias |
| `gptoss` | Groq | `openai/gpt-oss-120b` | alternate cloud model |
| `qwen3.6-local` | Ollama | `qwen3.6:27b` | local form of the authoring model |
| `qwen1.5b` | Ollama | `qwen2.5-coder:1.5b` | small local model / simulated student |
| `qwen7b` | Ollama | `qwen2.5-coder:7b` | default student-hint model |
| `qwen14b` | Ollama | `qwen2.5-coder:14b` | larger local alternative |
| `qwen32b` | Ollama | `qwen2.5-coder:32b` | larger local alternative |
| `qwen3coder` | Ollama | `qwen3-coder:30b` | local mixture-of-experts alternative |

Cloud models require the corresponding API key and internet access. Ollama models must already be
present or must be pulled from the Setup page/CLI. Larger models need substantially more memory and
disk than the default.

The provider layer hides reasoning traces for Groq Qwen3 and GPT-OSS models and gives them a larger
completion allowance. Qwen3 also runs with reasoning effort disabled for this structured authoring
workflow. These are implementation defaults, not user-configurable switches.

`eval/src/providers.py` contains a second display-name registry used by research/evaluation code.
Names such as `groq-qwen3.6-27b` belong to that provider-level registry and are not valid values for
the app's `TUTOR_AUTHOR_MODEL`; use the app names in the table above.

## Network exposure and security

Packaged shells bind the backend to `0.0.0.0:8077` so LAN hosting can be enabled without restarting
the app. Setting `instructor_url` controls whether the network-facing class-server operations are
actually enabled.

| Surface | Default network behavior |
| --- | --- |
| Local UI and health endpoint | available on the bound address |
| Student assignment fetch and attempt ingest | remote requests are accepted only while `instructor_url` is set |
| Instructor routes (`/api/instructor/*`) | loopback-only and additionally protected by the author password when one is set |
| Password set/clear routes | loopback-only |
| Student grading/hints and setup routes | not covered by the instructor gate; do not expose a source server to an untrusted network without an external firewall or reverse-proxy policy |

`RUNDIFF_ALLOW_REMOTE_ADMIN=1` removes the loopback restriction for instructor and password-change
routes. Use it only when intentionally administering a headless backend from another machine, and
set an author password first. It does not add TLS or rate limiting.

The app permits cross-origin API requests. For internet-facing deployment, put it behind a reverse
proxy that provides HTTPS, authentication/access controls appropriate to your environment, request
limits, and a restricted firewall. The built-in LAN workflow is designed for a trusted classroom
network, not direct public-internet exposure.

For normal LAN setup and failure modes, see the
[Network sync guide](webapp/network_sync_guide.md).

## Build and development settings

### Frontend

- `bun run dev` in `webapp/frontend/` starts Vite on port `5180` and proxies `/api` to
  `http://127.0.0.1:8077`.
- `bun run build` writes `webapp/frontend/dist/`.
- There are no frontend environment variables in the current app; API calls use same-origin
  `/api/*` paths.

### Backend sidecar

From `webapp/desktop/`:

```bash
bun run build:backend
```

This invokes PyInstaller through `uv` and writes
`webapp/backend/dist_backend/rundiff-backend/`. It is an `onedir` bundle: keep the executable and
its `_internal` directory together. Build it on each target operating system and architecture.

### Native macOS shell

`webapp/desktop-macos/build.sh` consumes the frontend build and backend sidecar.

| Setting | Default | Effect |
| --- | --- | --- |
| `ARCH` | `arm64` | Swift target architecture; `x86_64` is supported by the script for local Intel builds |
| `VERSION` | `0.1.0` | bundle version and short version |
| `--dmg` | off | also creates `release/Run·Diff.dmg` |

The release workflow currently publishes macOS arm64 only.

### Tauri Windows/Linux shell

Run `webapp/desktop-tauri/prep-resources.sh` after building the frontend and sidecar, then run
`cargo tauri build` from `webapp/desktop-tauri/src-tauri/`. Configured bundle targets are MSI and
NSIS on Windows, and DEB and AppImage on Linux. CI currently publishes Windows MSI/setup EXE and
Linux AppImage artifacts.

Tauri requires Rust plus platform WebView/build dependencies. See its
[platform README](webapp/desktop-tauri/README.md) for exact prerequisites.

### Electron alternate shell

`webapp/desktop/` supplies shared `build:frontend` and `build:backend` scripts and an alternate
Electron shell. `bun run dev` starts the Electron development window. Its `electron-builder`
configuration targets DMG and AppImage; `pack.mjs` is the alternate macOS packager described in the
[Electron README](webapp/desktop/README.md). The primary release workflow uses Swift for macOS and
Tauri for Windows/Linux.

### Fixed desktop values

All three desktop shells currently use:

- window/health address `127.0.0.1`;
- backend bind address `0.0.0.0`;
- port `8077`; and
- a 30-second backend startup timeout.

Changing those requires modifying and rebuilding the shell; environment variables passed to an
already packaged app are overwritten for host/port when it spawns its sidecar. If another healthy
Run·Diff backend already answers on port `8077`, the shell reuses it and does not stop it when the
window closes.

## Behavior that is not configurable

The following are code defaults, not settings:

- published sets use the grader's fixed seed list;
- the adaptive hint plan is selected from the detected error family;
- model-written hints are checked for executable-answer leakage and replaced by an offline hint if
  necessary;
- state-problem evidence redacts missing expected rows;
- authoring validates on six initial seeds and stress-tests on 60 additional seeds;
- class activity treats a student as “active now” for 120 seconds after their latest event; and
- the live insights view polls on a fixed interval in the frontend.

Per-problem exact column-name enforcement is not an environment setting. Turn on **Require exact
column names** while authoring/editing a SELECT problem, then republish the set.
