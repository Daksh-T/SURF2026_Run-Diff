# Run·Diff — lean cross-platform shell (Tauri)

The **Windows + Linux** desktop path. A thin [Tauri](https://tauri.app) shell that wraps the app
in the **OS WebView** (WebView2 on Windows, WebKitGTK on Linux) plus a tiny Rust binary, instead
of bundling Chromium + Node like the Electron shell in [`../desktop`](../desktop).

| | Electron (`../desktop`) | Tauri (this) | Swift (`../desktop-macos`) |
|---|---|---|---|
| Platforms | mac / Win / Linux | **Win / Linux** | macOS only |
| UI runtime | bundles Chromium | system WebView | system WKWebView |
| Bundle size | ~279 MB | **~10–20 MB shell** (+ ~34 MB backend sidecar) | ~38 MB |
| Toolchain | bun + electron | Rust + Tauri CLI | `swiftc` |

macOS keeps the Swift shell (proven, no Rust needed). This Tauri shell gives the project its
**first Windows build** and a lean Linux build, both from one Rust codebase.

## How it works

Same behavior as the other two shells (see [`src-tauri/src/main.rs`](src-tauri/src/main.rs)):

1. On launch, paints a small loading window ([`ui/index.html`](ui/index.html)).
2. Reuses an already-running backend on `:8077`; otherwise spawns the bundled PyInstaller
   `rundiff-backend` sidecar (with `HOST=0.0.0.0`, `PORT=8077`, `TUTOR_DATA_DIR`,
   `TUTOR_FRONTEND_DIST`).
3. Polls `http://127.0.0.1:8077/api/health` for up to 30 s.
4. Navigates the window to `http://127.0.0.1:8077/practice` (the backend serves the built React
   frontend).
5. On quit, kills **only** a backend it spawned (never one it reused).

### Why a bundled resource, not `externalBin`

Tauri's `externalBin` (sidecar) mechanism bundles and launches a **single file**. Our backend is
a PyInstaller *onedir* bundle — an executable plus an `_internal/` tree that must sit next to it —
so it can't be expressed as one externalBin file. Instead the whole `rundiff-backend/` directory
(and the frontend `dist/`) ride as Tauri **bundle resources**, and `main.rs` launches the inner
executable from the resolved resource dir with `std::process::Command`. This is the same
resource-dir + spawn pattern the Electron (`process.resourcesPath`) and Swift
(`Bundle.main.resourcePath`) shells already use, so all three shells behave identically.

## Build

Requires the same prebuilt artifacts the other shells consume:

- `../backend/dist_backend/rundiff-backend/` — the PyInstaller sidecar
- `../frontend/dist/` — the built frontend

```sh
# 1. Build the inputs (from webapp/):
( cd frontend && bun install && bun run build )
( cd backend  && uv sync && uv run --with pyinstaller pyinstaller rundiff_backend.spec \
    --noconfirm --distpath dist_backend --workpath build_backend )

# 2. Stage them into src-tauri/resources/ (gitignored):
./prep-resources.sh

# 3. Bundle (uses the local toolchain set up below):
cd src-tauri && cargo tauri build
```

Output (under `src-tauri/target/release/bundle/`):
- Windows: `msi/*.msi`, `nsis/*-setup.exe`
- Linux: `appimage/*.AppImage`, `deb/*.deb`

The bundle is **unsigned** (no code-signing identity configured), same as the Electron/Swift
builds. On Windows, SmartScreen may warn on first run; on Linux the AppImage just needs `chmod +x`.

## Local-only Rust toolchain (no system install)

This project never installs Rust system-wide. Point `CARGO_HOME` / `RUSTUP_HOME` at a directory
**inside the repo** and install everything there:

```sh
# from the repo root — installs rustup/cargo under ./.toolchain (gitignored)
export CARGO_HOME="$PWD/.toolchain/cargo"
export RUSTUP_HOME="$PWD/.toolchain/rustup"
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
  sh -s -- -y --no-modify-path --profile minimal --default-toolchain stable

# install the Tauri CLI locally (into ./.toolchain/bin), not globally:
"$CARGO_HOME/bin/cargo" install tauri-cli --version "^2.0" --locked --root "$PWD/.toolchain"

# then run cargo / cargo-tauri via the local toolchain:
export PATH="$PWD/.toolchain/bin:$CARGO_HOME/bin:$PATH"
```

`.toolchain/`, `src-tauri/target/`, `src-tauri/gen/`, and `src-tauri/resources/` are all
gitignored — nothing heavy lands in the repo.

### System WebView dependencies

- **Windows:** WebView2 runtime ships with Windows 10/11 (and is preinstalled on the
  `windows-latest` CI runner). No extra step.
- **Linux:** needs WebKitGTK at build + run time, e.g. on Debian/Ubuntu:
  `libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev` (build) / the runtime equivalents.
- **macOS:** uses WKWebView (no deps) — but macOS is served by the Swift shell, not this one.

CI builds Windows (`.msi` + `.exe`) and Linux (`.AppImage`) from this project; see
`.github/workflows/release.yml` (release) and `build.yml` (PR validation).
