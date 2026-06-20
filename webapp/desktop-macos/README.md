# Run·Diff — macOS-native shell (WKWebView)

A macOS-only alternative to the Electron shell in [`../desktop`](../desktop). Same behavior, a
fraction of the size: it wraps the app in the **system WebView** (WKWebView) and a tiny native
AppKit binary instead of bundling Chromium + Node.

| | Electron (`../desktop`) | This (`desktop-macos`) |
|---|---|---|
| Bundle size | ~279 MB | **~38 MB** |
| UI runtime | bundles Chromium | system WKWebView |
| Toolchain | bun + electron | `swiftc` (Xcode CLT only) |
| Platforms | macOS / Windows / Linux | macOS only |

Electron stays the cross-platform path; this is the lean macOS build.

## How it works

The Python backend already serves both the API and the built React frontend on `:8077`, so the
shell does almost nothing: it reuses a running backend on `:8077` (else spawns the bundled
PyInstaller sidecar), polls `/api/health`, then points a WKWebView window at
`http://127.0.0.1:8077/practice`. Quit kills only a backend it spawned. This mirrors
`../desktop/main.js` — see [`RunDiff.swift`](RunDiff.swift).

## Build

Requires the same prebuilt artifacts the Electron build consumes:

```sh
# from ../desktop:
bun run build:frontend   # -> ../frontend/dist
bun run build:backend    # -> ../backend/dist_backend/rundiff-backend

# then here:
./build.sh               # -> release/Run·Diff.app
```

The app is **unsigned** (ad-hoc codesign only) — same as the Electron build, since no Developer ID
identity is configured. First launch may need right-click → Open.
