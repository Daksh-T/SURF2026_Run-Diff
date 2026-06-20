// Run·Diff — macOS-native desktop shell (WKWebView).
//
// macOS-only counterpart to the Electron shell in ../desktop. Same behavior, a fraction of the
// size: it uses the system WebView (WKWebView) instead of bundling Chromium, and a tiny native
// AppKit binary instead of the Electron/Node runtime.
//
// On launch: reuse an already-running backend on :8077, else spawn the bundled PyInstaller
// sidecar; poll /api/health; then open a window at the backend (which serves the built frontend).
// Quit kills only a backend WE spawned.

import AppKit
import WebKit

let HOST = "127.0.0.1"        // the window + health checks always talk to loopback
// Bind the backend to all interfaces so other devices on the LAN can reach it when the
// instructor enables "Host on this network". Loopback still works for this machine's own window.
let BIND_HOST = "0.0.0.0"
let PORT = 8077
let BASE = "http://\(HOST):\(PORT)"

// MARK: - Resource resolution (packaged .app vs. dev run)

struct Paths {
    let sidecar: String      // the PyInstaller executable
    let backendCwd: String   // dir containing the sidecar + its _internal
    let frontendDist: String // built frontend (served by the backend)
    let dataDir: String      // writable per-user data root
}

func resolvePaths() -> Paths {
    let res = Bundle.main.resourcePath ?? FileManager.default.currentDirectoryPath
    let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
    let dataDir = appSupport.appendingPathComponent("RunDiff/data").path
    let backendCwd = (res as NSString).appendingPathComponent("rundiff-backend")
    return Paths(
        sidecar: (backendCwd as NSString).appendingPathComponent("rundiff-backend"),
        backendCwd: backendCwd,
        frontendDist: (res as NSString).appendingPathComponent("dist"),
        dataDir: dataDir
    )
}

// MARK: - Health check

func httpOk(_ urlString: String, timeout: TimeInterval = 1.0) -> Bool {
    guard let url = URL(string: urlString) else { return false }
    var ok = false
    let sem = DispatchSemaphore(value: 0)
    var req = URLRequest(url: url)
    req.timeoutInterval = timeout
    let task = URLSession.shared.dataTask(with: req) { _, resp, _ in
        if let http = resp as? HTTPURLResponse {
            ok = http.statusCode >= 200 && http.statusCode < 500
        }
        sem.signal()
    }
    task.resume()
    _ = sem.wait(timeout: .now() + timeout + 0.5)
    return ok
}

func waitForHealth(timeoutMs: Int = 30000) -> Bool {
    let deadline = Date().addingTimeInterval(Double(timeoutMs) / 1000.0)
    while Date() < deadline {
        if httpOk("\(BASE)/api/health") { return true }
        Thread.sleep(forTimeInterval: 0.5)
    }
    return false
}

// MARK: - App delegate

class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var backend: Process?   // set only if WE spawn it; nil if we reused an existing server

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()   // a bare app has no menu bar → no Cmd+C/V/X/A/Z/Q; this wires them up
        buildWindow()

        // Do health check / spawn off the main thread so the window paints immediately.
        DispatchQueue.global(qos: .userInitiated).async {
            if httpOk("\(BASE)/api/health") {
                NSLog("[rundiff] reusing existing backend on :\(PORT) (will not kill it on quit)")
            } else {
                self.spawnBackend()
            }
            let ok = waitForHealth()
            if !ok { NSLog("[rundiff] backend did not become healthy within 30s") }
            DispatchQueue.main.async {
                self.webView.load(URLRequest(url: URL(string: "\(BASE)/practice")!))
            }
        }
    }

    func buildWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1280, height: 860)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false
        )
        window.title = "Run·Diff"
        window.minSize = NSSize(width: 900, height: 600)
        // warm-paper, so first paint isn't a white flash
        window.backgroundColor = NSColor(red: 0xf4/255.0, green: 0xf0/255.0, blue: 0xe6/255.0, alpha: 1)
        window.center()

        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: frame, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self                       // drives <input type=file> open panels
        webView.allowsBackForwardNavigationGestures = true
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func reload(_ sender: Any?) { webView.reload() }

    // Build a standard macOS menu bar. Without it, the responder chain has nothing to route
    // ⌘C/⌘V/⌘X/⌘A/⌘Z to, so editing shortcuts silently do nothing inside the WebView.
    func buildMenu() {
        let main = NSMenu()

        // App menu
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu(); appItem.submenu = appMenu
        appMenu.addItem(withTitle: "About Run·Diff", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide Run·Diff", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Run·Diff", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        // Edit menu — the one that actually delivers clipboard shortcuts to the WebView
        let editItem = NSMenuItem(); main.addItem(editItem)
        let editMenu = NSMenu(title: "Edit"); editItem.submenu = editMenu
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        let redo = editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        // View menu — Reload
        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let viewMenu = NSMenu(title: "View"); viewItem.submenu = viewMenu
        viewMenu.addItem(withTitle: "Reload", action: #selector(AppDelegate.reload(_:)), keyEquivalent: "r")

        // Window menu
        let winItem = NSMenuItem(); main.addItem(winItem)
        let winMenu = NSMenu(title: "Window"); winItem.submenu = winMenu
        winMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        NSApp.windowsMenu = winMenu

        NSApp.mainMenu = main
    }

    // MARK: - File uploads (<input type=file>)

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.begin { resp in
            completionHandler(resp == .OK ? panel.urls : nil)
        }
    }

    // MARK: - Downloads (exports: <a download> on blob: URLs and backend CSV/JSON endpoints)

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 preferences: WKWebpagePreferences,
                 decisionHandler: @escaping (WKNavigationActionPolicy, WKWebpagePreferences) -> Void) {
        // anchors with a `download` attribute (our JSON/CSV exports) ask to download, not navigate
        decisionHandler(navigationAction.shouldPerformDownload ? .download : .allow, preferences)
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        // safety net: anything the WebView can't render (attachments) becomes a download
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first!
        completionHandler(uniqueDestination(dir.appendingPathComponent(suggestedFilename)))
    }
    func downloadDidFinish(_ download: WKDownload) { NSLog("[rundiff] download finished") }
    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        NSLog("[rundiff] download failed: \(error)")
    }

    // Don't clobber an existing file: "name.json" → "name (1).json", "name (2).json", …
    private func uniqueDestination(_ url: URL) -> URL {
        let fm = FileManager.default
        guard fm.fileExists(atPath: url.path) else { return url }
        let dir = url.deletingLastPathComponent()
        let ext = url.pathExtension
        let stem = url.deletingPathExtension().lastPathComponent
        var n = 1
        while true {
            let name = ext.isEmpty ? "\(stem) (\(n))" : "\(stem) (\(n)).\(ext)"
            let candidate = dir.appendingPathComponent(name)
            if !fm.fileExists(atPath: candidate.path) { return candidate }
            n += 1
        }
    }

    func spawnBackend() {
        let p = resolvePaths()
        try? FileManager.default.createDirectory(atPath: p.dataDir, withIntermediateDirectories: true)

        var env = ProcessInfo.processInfo.environment
        env["HOST"] = BIND_HOST
        env["PORT"] = String(PORT)
        env["TUTOR_DATA_DIR"] = p.dataDir
        if FileManager.default.fileExists(atPath: p.frontendDist) {
            env["TUTOR_FRONTEND_DIST"] = p.frontendDist
        }

        let proc = Process()
        proc.executableURL = URL(fileURLToPath: p.sidecar)
        proc.currentDirectoryURL = URL(fileURLToPath: p.backendCwd)
        proc.environment = env
        proc.terminationHandler = { _ in NSLog("[rundiff] backend exited") }
        NSLog("[rundiff] spawning backend: \(p.sidecar) (cwd=\(p.backendCwd))")
        do {
            try proc.run()
            backend = proc
        } catch {
            NSLog("[rundiff] failed to spawn backend: \(error)")
        }
    }

    func killBackend() {
        if let b = backend, b.isRunning {
            NSLog("[rundiff] killing spawned backend")
            b.terminate()
        }
        backend = nil
    }

    // Single-window desktop app: quit when the window closes.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { killBackend() }
}

// URL(fileURLWithPath:) is the canonical spelling; small alias for readability above.
extension URL {
    init(fileURLToPath path: String) { self.init(fileURLWithPath: path) }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
