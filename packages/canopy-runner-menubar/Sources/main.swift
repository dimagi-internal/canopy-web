// Canopy Runner — native macOS menu-bar control surface for the runner daemon.
//
// Replaces the pyobjc menu-bar app, which macOS refused to host: launched as generic
// framework "Python.app", its status item orphaned to the screen corner instead of the
// menu bar (proven 2026-07-16 via the app's own startup log). A real compiled Mach-O
// inside a real .app bundle gets a real identity, so macOS hosts the status item like
// any other app (superwhisper, etc.).
//
// Thin surface, no state of its own: runner state is read from the runner's own files
// (the PAUSED sentinel, the heartbeat file, launchctl); pausing writes the same PAUSED
// sentinel the daemon already honors. The status tree is tinted per state.

import AppKit
import Foundation
import WebKit

// ── paths + config (shared with the daemon) ──────────────────────────────────────
let home = FileManager.default.homeDirectoryForCurrentUser
let canopyDir = home.appendingPathComponent(".canopy")
let pauseFile = canopyDir.appendingPathComponent("PAUSED")
let heartbeatFile = canopyDir.appendingPathComponent("heartbeat")
let configFile = canopyDir.appendingPathComponent("runner.json")
let logFile = canopyDir.appendingPathComponent("runner.log")
let launchLabel = "com.canopy.runner"
// The plist launchd ACTUALLY loaded, not the repo's copy of one. Pointing at the
// checkout was a silent breakage: "Take one turn" reads ProgramArguments + env
// from here (see takeOneTurn) so it can't drift from how the daemon runs — but
// the repo copy listed a single PYTHONPATH entry while the runner imports three
// packages, so every invocation died on ModuleNotFoundError, unreported, because
// the launch is fire-and-forget. install-runner.sh generates this file.
let plistPath = home.appendingPathComponent("Library/LaunchAgents/com.canopy.runner.plist")

func baseURL() -> String {
    guard let data = try? Data(contentsOf: configFile),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let url = obj["base_url"] as? String else {
        return "https://labs.connect.dimagi.com/canopy"
    }
    return url.hasSuffix("/") ? String(url.dropLast()) : url
}

// ── runner state (mirrors canopy_runner.menubar._runner_state) ────────────────────
enum RunnerState: String {
    case running, paused, stopped, stale
    var color: NSColor {
        switch self {
        case .running: return NSColor(srgbRed: 0.40, green: 0.71, blue: 0.52, alpha: 1)  // green
        case .paused:  return NSColor(srgbRed: 0.89, green: 0.69, blue: 0.28, alpha: 1)  // amber
        case .stopped: return NSColor(srgbRed: 0.85, green: 0.35, blue: 0.32, alpha: 1)  // red
        case .stale:   return NSColor(srgbRed: 0.90, green: 0.57, blue: 0.27, alpha: 1)  // orange
        }
    }
    var label: String {
        switch self {
        case .running: return "Running"
        case .paused:  return "Paused"
        case .stopped: return "Stopped"
        case .stale:   return "Stale (no recent heartbeat)"
        }
    }
}

@discardableResult
func run(_ args: [String]) -> (code: Int32, out: String) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/bin/sh")
    p.arguments = ["-c", args.joined(separator: " ")]
    let pipe = Pipe()
    p.standardOutput = pipe
    p.standardError = pipe
    do { try p.run() } catch { return (-1, "") }
    p.waitUntilExit()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
}

func daemonLoaded() -> Bool {
    run(["launchctl", "print", "gui/\(getuid())/\(launchLabel)", ">/dev/null", "2>&1"]).code == 0
}

func heartbeatAgeSeconds() -> Int? {
    guard let attrs = try? FileManager.default.attributesOfItem(atPath: heartbeatFile.path),
          let m = attrs[.modificationDate] as? Date else { return nil }
    return Int(Date().timeIntervalSince(m))
}

func currentState() -> RunnerState {
    if !daemonLoaded() { return .stopped }
    if FileManager.default.fileExists(atPath: pauseFile.path) { return .paused }
    if let age = heartbeatAgeSeconds(), age < 120 { return .running }
    return .stale
}

// ── the app ───────────────────────────────────────────────────────────────────────
final class Controller: NSObject, NSApplicationDelegate {
    let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var baseTree: NSImage?
    var timer: Timer?
    var window: NSWindow?
    var web: WKWebView!
    var authed = false  // have we minted + injected the session cookie this launch?

    func applicationDidFinishLaunching(_ n: Notification) {
        baseTree = loadTree()
        buildWindow()
        buildMainMenu()
        // Left-click opens the shared web fleet UI; right-click shows local controls.
        if let btn = statusItem.button {
            btn.target = self
            btn.action = #selector(statusClicked)
            btn.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        rebuild()
        timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.rebuild()
        }
    }

    // A Dock-icon click (and any re-open when no window is visible) routes here — open the
    // SAME shared window the menu-bar icon opens.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showWindow()
        return true
    }

    // Closing the window must NOT quit: this is still the background runner controller
    // (menu-bar tree, status poll, daemon control). Quit is explicit (right-click menu /
    // Cmd-Q).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    // A .regular app needs a main menu for a sane focused experience (Cmd-Q, the app menu).
    // Minimal: an app menu with Show + Quit.
    func buildMainMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Show Canopy Runner", action: #selector(showWindow), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Canopy Runner", action: #selector(quit), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = main
    }

    // The fleet UI is the DEPLOYED /supervisor React surface — the SAME app the phone PWA
    // and desktop browser load (CLAUDE.md: "loaded by three consumers"). Hosting it in a
    // WKWebView keeps the menu bar DRY with web + mobile: zero duplicated components, and
    // a persistent data store so the Google login persists across opens.
    func buildWindow() {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 420, height: 640), configuration: cfg)
        let win = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 640),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        win.title = "Canopy Runner"
        win.contentView = web
        // Reused across opens: closing HIDES it (see applicationShouldTerminateAfterLastWindowClosed),
        // so it must not be deallocated on close or the next open dereferences a freed window.
        win.isReleasedWhenClosed = false
        win.center()
        window = win
    }

    @objc func statusClicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            // Show the local-controls menu, then clear it so left-click still opens the panel.
            statusItem.menu = buildMenu(currentState())
            statusItem.button?.performClick(nil)
            DispatchQueue.main.async { self.statusItem.menu = nil }
        } else {
            showWindow()
        }
    }

    @objc func showWindow() {
        // ALWAYS open on the supervisor home, never wherever the last session navigated
        // to. First open mints + injects the session cookie; after that the cookie is set,
        // so we just reload /supervisor (cheap, no re-mint).
        if authed { loadSupervisor() } else { authenticateThenLoad() }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    // SHARED AUTH: the panel is authenticated with the SAME PAT the daemon uses (both read
    // ~/.canopy/runner.json → the workbench token). We POST it to /api/debug/mint-session/,
    // inject the returned session cookie into the WebView, then load /supervisor already
    // signed in — no separate Google login. Falls back to interactive login if minting fails.
    func authenticateThenLoad() {
        guard let token = readToken(),
              let mintURL = URL(string: baseURL() + "/api/debug/mint-session/"),
              let host = URL(string: baseURL())?.host else {
            loadSupervisor(); return
        }
        var req = URLRequest(url: mintURL)
        req.httpMethod = "POST"
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = #"{"ttl_seconds":604800}"#.data(using: .utf8)  // 7 days
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self else { return }
            guard let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let name = obj["cookie_name"] as? String,
                  let value = obj["cookie_value"] as? String,
                  let cookie = HTTPCookie(properties: [
                      .domain: host, .path: "/", .name: name, .value: value, .secure: "TRUE",
                  ]) else {
                DispatchQueue.main.async { self.loadSupervisor() }  // fall back to login
                return
            }
            DispatchQueue.main.async {
                self.web.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) {
                    self.authed = true
                    self.loadSupervisor()
                }
            }
        }.resume()
    }

    func loadSupervisor() {
        if let url = URL(string: baseURL() + "/supervisor") { web.load(URLRequest(url: url)) }
    }

    // The workbench PAT, shared with the daemon (runner.json "token", possibly an @file ref).
    func readToken() -> String? {
        guard let data = try? Data(contentsOf: configFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              var tok = obj["token"] as? String else { return nil }
        if tok.hasPrefix("@") {
            let path = NSString(string: String(tok.dropFirst())).expandingTildeInPath
            tok = (try? String(contentsOfFile: path, encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        }
        return tok.isEmpty ? nil : tok
    }

    // Load the committed monochrome menu-bar tree (bundled from assets/brand), sized for
    // the menu bar. Falls back to an SF Symbol so the app never launches icon-less.
    func loadTree() -> NSImage {
        for name in ["menubar-tree@2x", "menubar-tree"] {
            if let path = Bundle.main.path(forResource: name, ofType: "png"),
               let img = NSImage(contentsOfFile: path) {
                img.size = NSSize(width: 18, height: 18)
                return img
            }
        }
        return NSImage(systemSymbolName: "tree", accessibilityDescription: "Canopy") ?? NSImage()
    }

    func tinted(_ base: NSImage, _ color: NSColor) -> NSImage {
        let img = base.copy() as! NSImage
        img.lockFocus()
        color.set()
        NSRect(origin: .zero, size: img.size).fill(using: .sourceAtop)
        img.unlockFocus()
        img.isTemplate = false
        return img
    }

    func rebuild() {
        let state = currentState()
        if let btn = statusItem.button, let tree = baseTree {
            btn.image = tinted(tree, state.color)
            btn.toolTip = "Canopy Runner — \(state.label)"
        }
        // Note: we do NOT assign statusItem.menu here — a permanent menu would swallow the
        // left-click we use to open the web panel. The menu is shown on demand (right-click).
    }

    func buildMenu(_ state: RunnerState) -> NSMenu {
        let menu = NSMenu()

        let header = NSMenuItem(title: "Runner: \(state.label)", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        if let age = heartbeatAgeSeconds() {
            let hb = NSMenuItem(title: "  last heartbeat \(age)s ago", action: nil, keyEquivalent: "")
            hb.isEnabled = false
            menu.addItem(hb)
        }
        menu.addItem(.separator())

        // Pause / Resume — the whole point: works with or without this UI, since it just
        // writes the sentinel file the daemon already checks each cycle.
        if state == .paused {
            add(menu, "Resume runner", #selector(resume))
        } else {
            add(menu, "Pause runner", #selector(pause))
        }

        // Start / Stop the daemon via launchctl.
        if state == .stopped {
            add(menu, "Start daemon", #selector(startDaemon))
        } else {
            add(menu, "Stop daemon", #selector(stopDaemon))
        }
        // Take exactly ONE queued turn without running the whole daemon — works even
        // while paused. Dispatch a turn (composer / a session Continue), then tap this.
        add(menu, "Take one turn", #selector(takeOneTurn))
        menu.addItem(.separator())

        add(menu, "Open Supervisor in browser…", #selector(openSupervisor))
        add(menu, "Reload panel", #selector(reloadPanel))
        add(menu, "Force refresh (clear cache)", #selector(forceRefresh))
        add(menu, "Open Log", #selector(openLog))
        menu.addItem(.separator())
        add(menu, "Quit Canopy Runner", #selector(quit))
        return menu
    }

    func add(_ menu: NSMenu, _ title: String, _ sel: Selector) {
        let item = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
    }

    // -- actions --
    @objc func pause() {
        FileManager.default.createFile(
            atPath: pauseFile.path,
            contents: ISO8601DateFormatter().string(from: Date()).data(using: .utf8))
        rebuild()
    }
    @objc func resume() {
        try? FileManager.default.removeItem(at: pauseFile)
        rebuild()
    }
    @objc func startDaemon() {
        run(["launchctl", "bootstrap", "gui/\(getuid())", "'\(plistPath.path)'"])
        run(["launchctl", "kickstart", "gui/\(getuid())/\(launchLabel)"])
        rebuild()
    }
    @objc func stopDaemon() {
        run(["launchctl", "bootout", "gui/\(getuid())/\(launchLabel)"])
        rebuild()
    }

    // Run the runner CLI ONCE with --drain-one: claim + execute a single queued turn,
    // then exit. We reuse the daemon's OWN launchd plist (same interpreter, PYTHONPATH,
    // config) so this can't drift from how the daemon runs — just with --drain-one and no
    // KeepAlive loop. Fire-and-forget on a background queue so the menu stays responsive.
    @objc func takeOneTurn() {
        guard let data = try? Data(contentsOf: plistPath),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil)
                  as? [String: Any],
              let argv = plist["ProgramArguments"] as? [String], !argv.isEmpty else {
            return
        }
        let extraEnv = plist["EnvironmentVariables"] as? [String: String] ?? [:]
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: argv[0])
        proc.arguments = Array(argv.dropFirst()) + ["--drain-one"]
        var env = ProcessInfo.processInfo.environment
        for (k, v) in extraEnv { env[k] = v }
        proc.environment = env
        DispatchQueue.global(qos: .userInitiated).async {
            try? proc.run()
            proc.waitUntilExit()
            DispatchQueue.main.async { self.rebuild() }
        }
    }
    @objc func openSupervisor() {
        if let url = URL(string: baseURL() + "/supervisor") { NSWorkspace.shared.open(url) }
    }
    @objc func reloadPanel() {
        authenticateThenLoad()  // re-mint the session cookie, then reload /supervisor
    }
    // The bulletproof hard-refresh, WITHOUT logging you out. "Reload panel" is a
    // normal navigation, so a stale service worker keeps serving the cached bundle.
    // This purges the service-worker registration + every CACHE so the next load must
    // fetch the current /supervisor and register the fresh (auto-updating) SW — but it
    // KEEPS cookies (the session) and localStorage (theme/prefs), so the reload comes
    // straight back signed in. No re-mint, no Google login. ("Reload panel" remains the
    // re-auth path for when the session itself needs refreshing.)
    @objc func forceRefresh() {
        let store = web.configuration.websiteDataStore
        var types = WKWebsiteDataStore.allWebsiteDataTypes()
        types.remove(WKWebsiteDataTypeCookies)
        types.remove(WKWebsiteDataTypeLocalStorage)
        store.removeData(ofTypes: types, modifiedSince: Date(timeIntervalSince1970: 0)) { [weak self] in
            self?.loadSupervisor()  // cookie preserved -> already authed -> fresh bundle
        }
    }
    @objc func openLog() {
        NSWorkspace.shared.open(logFile)
    }
    @objc func quit() { NSApplication.shared.terminate(nil) }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)  // Dock icon = always-visible "it's running" signal;
                                   // the notch clips the menu-bar tree, so the Dock icon
                                   // is the reliable running signal. Standard app: Dock +
                                   // Cmd-Tab + an app menu when focused.
let controller = Controller()
app.delegate = controller
app.run()
