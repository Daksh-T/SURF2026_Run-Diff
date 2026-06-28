// Run·Diff — lean cross-platform desktop shell (Tauri + system WebView).
//
// Cross-platform counterpart to the Swift WKWebView shell in ../../desktop-macos and the
// Electron shell in ../../desktop. Uses the OS WebView (WebView2 on Windows, WebKitGTK on
// Linux) instead of bundling Chromium, so the bundle is a fraction of Electron's size.
//
// On launch: reuse an already-running backend on :8077, else spawn the bundled PyInstaller
// sidecar; poll /api/health; then point the window at the backend (which serves the built
// frontend) at /practice. Quit kills only a backend WE spawned.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent};

const HOST: &str = "127.0.0.1"; // the window + health checks always talk to loopback
                                // Bind the backend to all interfaces so other devices on the LAN can reach it when the
                                // instructor enables "Host on this network". Loopback still works for this window.
const BIND_HOST: &str = "0.0.0.0";
const PORT: u16 = 8077;

/// Holds the backend child process — Some only if WE spawned it (nil when we reused an
/// already-running server, so quit never kills someone else's backend).
struct Backend(Mutex<Option<Child>>);

fn base() -> String {
    format!("http://{HOST}:{PORT}")
}

// MARK: - Health check (tiny raw HTTP/1.0 GET so we pull in no HTTP client dependency)

fn health_ok() -> bool {
    let addr: SocketAddr = match format!("{HOST}:{PORT}").parse() {
        Ok(a) => a,
        Err(_) => return false,
    };
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(1000)) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(1000)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(1000)));
    let req =
        format!("GET /api/health HTTP/1.0\r\nHost: {HOST}\r\nConnection: close\r\n\r\n");
    if stream.write_all(req.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 64];
    let n = match stream.read(&mut buf) {
        Ok(n) => n,
        Err(_) => return false,
    };
    // status line looks like "HTTP/1.0 200 OK"; accept anything the server answered with
    // (2xx–4xx), matching the other shells.
    let head = String::from_utf8_lossy(&buf[..n]);
    if let Some(code) = head.split_whitespace().nth(1) {
        if let Ok(c) = code.parse::<u16>() {
            return (200..500).contains(&c);
        }
    }
    false
}

fn wait_for_health(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if health_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

// MARK: - Resource resolution (bundled app vs. dev run)

fn backend_exe_name() -> &'static str {
    if cfg!(windows) {
        "rundiff-backend.exe"
    } else {
        "rundiff-backend"
    }
}

/// Returns (executable, cwd) for the PyInstaller sidecar, or None if not bundled.
/// The sidecar is a PyInstaller onedir bundle (executable + `_internal/`), shipped as a Tauri
/// resource directory; we launch the inner executable directly.
fn resolve_backend(resource_dir: &Path) -> Option<(PathBuf, PathBuf)> {
    let exe = backend_exe_name();
    let candidates = [
        resource_dir.join("resources").join("rundiff-backend"),
        resource_dir.join("rundiff-backend"),
    ];
    for cwd in candidates {
        let e = cwd.join(exe);
        if e.exists() {
            return Some((e, cwd));
        }
    }
    None
}

fn resolve_frontend(resource_dir: &Path) -> Option<PathBuf> {
    let candidates = [
        resource_dir.join("resources").join("frontend-dist"),
        resource_dir.join("frontend-dist"),
    ];
    for c in candidates {
        if c.join("index.html").exists() {
            return Some(c);
        }
    }
    None
}

fn spawn_backend(app: &tauri::AppHandle) {
    let resource_dir = match app.path().resource_dir() {
        Ok(d) => d,
        Err(e) => {
            eprintln!("[rundiff] could not resolve resource dir: {e}");
            return;
        }
    };
    let data_dir = app
        .path()
        .app_local_data_dir()
        .map(|d| d.join("data"))
        .ok();

    let Some((exe, cwd)) = resolve_backend(&resource_dir) else {
        eprintln!("[rundiff] backend sidecar not found under {resource_dir:?}");
        return;
    };

    let mut cmd = Command::new(&exe);
    cmd.current_dir(&cwd);
    cmd.env("HOST", BIND_HOST);
    cmd.env("PORT", PORT.to_string());
    if let Some(dd) = &data_dir {
        let _ = std::fs::create_dir_all(dd);
        cmd.env("TUTOR_DATA_DIR", dd);
    }
    if let Some(fd) = resolve_frontend(&resource_dir) {
        cmd.env("TUTOR_FRONTEND_DIST", fd);
    }

    // On Windows the sidecar is a console app; don't flash a console window.
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    eprintln!("[rundiff] spawning backend: {exe:?} (cwd={cwd:?})");
    match cmd.spawn() {
        Ok(child) => {
            *app.state::<Backend>().0.lock().unwrap() = Some(child);
        }
        Err(e) => eprintln!("[rundiff] failed to spawn backend: {e}"),
    }
}

fn kill_backend(app: &tauri::AppHandle) {
    if let Some(mut child) = app.state::<Backend>().0.lock().unwrap().take() {
        eprintln!("[rundiff] killing spawned backend");
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn main() {
    tauri::Builder::default()
        .manage(Backend(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();
            // Health-check / spawn off-thread so the loading window paints immediately.
            std::thread::spawn(move || {
                if health_ok() {
                    eprintln!(
                        "[rundiff] reusing existing backend on :{PORT} (will not kill it on quit)"
                    );
                } else {
                    spawn_backend(&handle);
                }
                if !wait_for_health(Duration::from_secs(30)) {
                    eprintln!("[rundiff] backend did not become healthy within 30s");
                }
                if let Some(win) = handle.get_webview_window("main") {
                    match tauri::Url::parse(&format!("{}/practice", base())) {
                        Ok(url) => {
                            if let Err(e) = win.navigate(url) {
                                eprintln!("[rundiff] navigate failed: {e}");
                            }
                        }
                        Err(e) => eprintln!("[rundiff] bad practice url: {e}"),
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Run·Diff")
        .run(|app, event| {
            // Kill a backend we spawned whenever the app is on its way out.
            if let RunEvent::ExitRequested { .. } | RunEvent::Exit = event {
                kill_backend(app);
            }
        });
}
