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
// canopy's answer to the daemon's last heartbeat (canopy_runner/server_status.py).
let serverStatusFile = canopyDir.appendingPathComponent("server-status.json")
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
        return "https://canopy.dimagi.com"
    }
    return url.hasSuffix("/") ? String(url.dropLast()) : url
}

// ── runner state (mirrors canopy_runner.menubar._runner_state) ────────────────────
// Green means canopy is accepting this box AND calls it ready, not merely that the
// loop is turning: a revoked token kept the loop turning (and the tree green) for
// hours while the server showed the runner offline and gave its work to other
// boxes, and a box whose emdash lost its debug port read green while Health read
// it degraded. The verdict is canopy's (its heartbeat reply), so the two agree.
enum RunnerState: String {
    case running, paused, degraded, stopped, stale, rejected, unreachable
    var color: NSColor {
        switch self {
        case .running: return NSColor(srgbRed: 0.40, green: 0.71, blue: 0.52, alpha: 1)  // green
        case .paused, .degraded: return NSColor(srgbRed: 0.89, green: 0.69, blue: 0.28, alpha: 1)  // amber
        case .stopped, .rejected: return NSColor(srgbRed: 0.85, green: 0.35, blue: 0.32, alpha: 1)  // red
        case .stale, .unreachable: return NSColor(srgbRed: 0.90, green: 0.57, blue: 0.27, alpha: 1)  // orange
        }
    }
    var label: String {
        switch self {
        case .running: return "Running"
        case .paused:  return "Paused"
        case .degraded: return "Degraded — not taking turns"
        case .stopped: return "Stopped"
        case .stale:   return "Stale (no recent heartbeat)"
        case .rejected: return "Offline — canopy is refusing this runner"
        case .unreachable: return "Offline — can't reach canopy"
        }
    }
}

// What canopy said to the last heartbeat. Absent on a daemon too old to write it,
// in which case the local loop is all we can go on.
struct ServerStatus {
    let ok: Bool
    let at: Date
    let lastOk: Date?
    let httpStatus: Int?
    let detail: String
    // Canopy's verdict on an accepted heartbeat: its status for the box, whether
    // it is ready, the box's own notes, and the fix for a known cause.
    let runnerStatus: String
    let ready: Bool
    let note: String
    let remedy: String

    static func load() -> ServerStatus? {
        guard let data = try? Data(contentsOf: serverStatusFile),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let ok = obj["ok"] as? Bool, let at = obj["at"] as? Double else { return nil }
        return ServerStatus(
            ok: ok, at: Date(timeIntervalSince1970: at),
            lastOk: (obj["last_ok_at"] as? Double).map { Date(timeIntervalSince1970: $0) },
            httpStatus: obj["status"] as? Int, detail: obj["detail"] as? String ?? "",
            runnerStatus: obj["runner_status"] as? String ?? "", ready: obj["ready"] as? Bool ?? true,
            note: obj["note"] as? String ?? "", remedy: obj["remedy"] as? String ?? "")
    }

    // Accepted, but canopy says the box cannot take work.
    var degraded: Bool { ok && (runnerStatus == "degraded" || !ready) }

    // A refusal canopy will repeat however often it is retried: the token is
    // revoked or expired (401), or no longer allowed this runner (403/404).
    var refusedByCanopy: Bool { !ok && [401, 403, 404].contains(httpStatus ?? 0) }

    // One line saying what canopy last said and since when.
    var summary: String {
        let since = lastOk.map { "last accepted \(ago($0))" } ?? "never accepted since this file began"
        if ok { return "canopy accepted the last heartbeat \(ago(at))" }
        if refusedByCanopy {
            let why = httpStatus == 401
                ? "the runner's token was rejected (401) — mint a new one in canopy Settings → Tokens"
                : "canopy refused it (\(httpStatus ?? 0))"
            return "\(why); \(since)"
        }
        return "can't reach canopy; \(since)"
    }
}

func ago(_ d: Date) -> String {
    let s = max(0, Int(Date().timeIntervalSince(d)))
    if s < 90 { return "\(s)s ago" }
    if s < 5400 { return "\(s / 60) min ago" }
    return "\(s / 3600) h ago"
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
    guard let age = heartbeatAgeSeconds(), age < 120 else { return .stale }
    // The loop is turning; now ask what canopy made of it.
    guard let server = ServerStatus.load(), Date().timeIntervalSince(server.at) < 120 else {
        return .running
    }
    if server.ok { return server.degraded ? .degraded : .running }
    if server.refusedByCanopy { return .rejected }
    // A blip is not an outage: only orange once nothing has been accepted for 2 min.
    if let lastOk = server.lastOk, Date().timeIntervalSince(lastOk) < 120 { return .running }
    return .unreachable
}

// The runner CLI exactly as launchd runs it: ProgramArguments + EnvironmentVariables
// from the plist it actually loaded (see plistPath), so the menu can never drift from
// the daemon's interpreter, venv or config.
struct RunnerCLI {
    let argv: [String]
    let env: [String: String]

    static func load() -> RunnerCLI? {
        guard let data = try? Data(contentsOf: plistPath),
              let plist = try? PropertyListSerialization.propertyList(from: data, format: nil)
                  as? [String: Any],
              let argv = plist["ProgramArguments"] as? [String], !argv.isEmpty else {
            return nil
        }
        var env = ProcessInfo.processInfo.environment
        for (k, v) in plist["EnvironmentVariables"] as? [String: String] ?? [:] { env[k] = v }
        return RunnerCLI(argv: argv, env: env)
    }

    // The --config the daemon runs with, or nil to take the CLI's default.
    var config: String? {
        guard let i = argv.firstIndex(of: "--config"), i + 1 < argv.count else { return nil }
        return argv[i + 1]
    }

    func process(_ args: [String]) -> Process {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: argv[0])
        proc.arguments = args
        proc.environment = env
        return proc
    }

    // A subcommand with the daemon's config, run to completion. Blocking.
    func run(_ subcommand: String, _ args: [String]) -> (code: Int32, out: String) {
        let proc = process([subcommand] + (config.map { ["--config", $0] } ?? []) + args)
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        do { try proc.run() } catch { return (-1, "") }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        return (proc.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }
}

// ── the app ───────────────────────────────────────────────────────────────────────
final class Controller: NSObject, NSApplicationDelegate, NSMenuDelegate, NSWindowDelegate {
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
        refreshOpensOn()
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

    // The fleet UI is the DEPLOYED React app — the SAME one the phone PWA
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
        win.delegate = self
        win.center()
        window = win
    }

    @objc func statusClicked() {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || event?.modifierFlags.contains(.control) == true {
            // Show the local-controls menu, then clear it so left-click still opens the panel.
            refreshOpensOn()  // lands before the "Opens on" submenu is opened
            statusItem.menu = buildMenu(currentState())
            statusItem.button?.performClick(nil)
            DispatchQueue.main.async { self.statusItem.menu = nil }
        } else {
            showWindow()
        }
    }

    @objc func showWindow() {
        // ALWAYS open on the home page, never wherever the last session navigated
        // to. First open mints + injects the session cookie; after that the cookie is set,
        // so we just reload it (cheap, no re-mint).
        if authed { loadHome() } else { authenticateThenLoad() }
        if window?.isVisible != true { applyFrame() }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    // SHARED AUTH: the panel is authenticated with the SAME PAT the daemon uses (both read
    // ~/.canopy/runner.json → the workbench token). We POST it to /api/debug/mint-session/,
    // inject the returned session cookie into the WebView, then load the home page already
    // signed in — no separate Google login. Falls back to interactive login if minting fails.
    func authenticateThenLoad() {
        guard let token = readToken(),
              let mintURL = URL(string: baseURL() + "/api/debug/mint-session/"),
              let host = URL(string: baseURL())?.host else {
            loadHome(); return
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
                DispatchQueue.main.async { self.loadHome() }  // fall back to login
                return
            }
            DispatchQueue.main.async {
                self.web.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) {
                    self.authed = true
                    self.loadHome()
                }
            }
        }.resume()
    }

    // The home page is the one the person chose in canopy's Settings ("Canopy Runner app
    // opens on": supervisor, IDE, …). /runner-app/ redirects there, so a new choice needs
    // no change here.
    func loadHome() {
        if let url = URL(string: baseURL() + "/runner-app/") { web.load(URLRequest(url: url)) }
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

    // Load the committed monochrome menu-bar mark (bundled from assets/brand), sized for
    // the menu bar. The mark is wide and low, so its image is landscape (24x16pt, the
    // MENUBAR_PT in assets/brand/generate.py). Falls back to an SF Symbol so the app
    // never launches icon-less.
    func loadTree() -> NSImage {
        for name in ["menubar-mark@2x", "menubar-mark"] {
            if let path = Bundle.main.path(forResource: name, ofType: "png"),
               let img = NSImage(contentsOfFile: path) {
                img.size = NSSize(width: 24, height: 16)
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
            var tip = "Canopy Runner — \(state.label)"
            if state == .rejected || state == .unreachable, let server = ServerStatus.load() {
                tip += "\n\(server.summary)"
            }
            if state == .degraded, let server = ServerStatus.load() {
                if !server.note.isEmpty { tip += "\n\(server.note)" }
                if !server.remedy.isEmpty { tip += "\nTo fix: \(server.remedy)" }
            }
            btn.toolTip = tip
        }
        // Note: we do NOT assign statusItem.menu here — a permanent menu would swallow the
        // left-click we use to open the web panel. The menu is shown on demand (right-click).
    }

    func buildMenu(_ state: RunnerState) -> NSMenu {
        let menu = NSMenu()

        let header = NSMenuItem(title: "Runner: \(state.label)", action: nil, keyEquivalent: "")
        header.isEnabled = false
        menu.addItem(header)
        if let server = ServerStatus.load() {
            let line = NSMenuItem(title: "  \(server.summary)", action: nil, keyEquivalent: "")
            line.isEnabled = false
            menu.addItem(line)
            if state == .degraded {
                for text in [server.note, server.remedy.isEmpty ? "" : "To fix: \(server.remedy)"] where !text.isEmpty {
                    let item = NSMenuItem(title: "  \(text)", action: nil, keyEquivalent: "")
                    item.isEnabled = false
                    menu.addItem(item)
                }
            }
        } else if let age = heartbeatAgeSeconds() {
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
        let closeItem = NSMenuItem(title: "Close sessions", action: nil, keyEquivalent: "")
        let sub = NSMenu(title: "Close sessions")
        sub.delegate = self
        closeMenu = sub
        closeItem.submenu = sub
        menu.addItem(closeItem)
        menu.addItem(.separator())

        let opensItem = NSMenuItem(title: "Opens on", action: nil, keyEquivalent: "")
        let opens = NSMenu(title: "Opens on")
        opens.delegate = self
        opensOnMenu = opens
        opensItem.submenu = opens
        menu.addItem(opensItem)

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
        guard let cli = RunnerCLI.load() else { return }
        let proc = cli.process(Array(cli.argv.dropFirst()) + ["--drain-one"])
        DispatchQueue.global(qos: .userInitiated).async {
            try? proc.run()
            proc.waitUntilExit()
            DispatchQueue.main.async { self.rebuild() }
        }
    }

    // -- close sessions --
    // The submenu lists this box's open sessions per project ("ace — 6"), read when it
    // opens (menuNeedsUpdate) rather than on every right-click, since it shells out to
    // the runner CLI. Closing deletes the emdash tasks exactly as a close from the phone
    // does — transcripts and canopy's record of the conversation are kept.
    var closeMenu: NSMenu?

    func menuNeedsUpdate(_ menu: NSMenu) {
        if menu === opensOnMenu { fillOpensOn(menu); return }
        guard menu === closeMenu else { return }
        menu.removeAllItems()
        func note(_ title: String) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
        }
        guard let cli = RunnerCLI.load() else { note("Runner is not installed"); return }
        let (code, out) = cli.run("close-sessions", ["--list", "--json"])
        if code == 2 { note("Needs a newer runner — update it first"); return }
        guard code == 0,
              let obj = try? JSONSerialization.jsonObject(with: Data(out.utf8)) as? [String: Any],
              let projects = obj["projects"] as? [String: Int],
              let total = obj["total"] as? Int else {
            note("Could not read emdash's sessions"); return
        }
        if total == 0 { note("No open sessions"); return }
        // The CLI sends them busiest-first; a JSON object does not keep that order.
        for (project, n) in projects.sorted(by: { ($0.value, $1.key) > ($1.value, $0.key) }) {
            // Listed so the counts add up to the total, but not closable on their
            // own: with no project a delete would be by name alone, which can take
            // another agent's session. "All sessions" reports each one to canopy,
            // where Supervisor says why it stayed open.
            if project.isEmpty {
                note("(no project) — \(n) session\(n == 1 ? "" : "s")")
                continue
            }
            let item = NSMenuItem(title: "\(project) — \(n) session\(n == 1 ? "" : "s")…",
                                  action: #selector(closeProjectSessions(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = ["project": project, "count": n]
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let all = NSMenuItem(title: "All sessions — \(total)…",
                             action: #selector(closeProjectSessions(_:)), keyEquivalent: "")
        all.target = self
        all.representedObject = ["count": total]
        menu.addItem(all)
    }

    @objc func closeProjectSessions(_ sender: NSMenuItem) {
        guard let info = sender.representedObject as? [String: Any],
              let count = info["count"] as? Int, let cli = RunnerCLI.load() else { return }
        let project = info["project"] as? String
        let what = project.map { "\(count) of \($0)'s session\(count == 1 ? "" : "s")" }
            ?? "all \(count) open session\(count == 1 ? "" : "s")"
        let confirm = NSAlert()
        confirm.messageText = "Close \(what)?"
        confirm.informativeText = "Each session's emdash task is deleted, as a close from the "
            + "phone does. Anything still running in them stops. Transcripts and canopy's "
            + "record of each conversation are kept."
        confirm.addButton(withTitle: "Close")
        confirm.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard confirm.runModal() == .alertFirstButtonReturn else { return }
        let scope = project.map { ["--project", $0] } ?? ["--all"]
        DispatchQueue.global(qos: .userInitiated).async {
            let (code, out) = cli.run("close-sessions", scope)
            DispatchQueue.main.async {
                let done = NSAlert()
                done.messageText = code == 0 ? "Sessions closed" : "Some sessions did not close"
                done.informativeText = out.isEmpty ? "The runner reported nothing (exit \(code))."
                    : out.trimmingCharacters(in: .whitespacesAndNewlines)
                NSApp.activate(ignoringOtherApps: true)
                done.runModal()
            }
        }
    }
    // -- opens on --
    // The page the window opens on is a canopy setting (Settings → Runner app), not a
    // local one, so it follows the person to every box. The menu lists the pages canopy
    // offers (GET /api/me/runner-app/ → choices), so a new page needs no change here.
    var opensOnMenu: NSMenu?
    var opensOn: (current: String, choices: [(value: String, label: String)])?

    func refreshOpensOn() {
        runnerAppRequest("GET", body: nil) { [weak self] pref in
            guard let self, let pref else { return }
            let changed = pref.current != self.opensOn?.current
            self.opensOn = pref
            // The window opened before canopy answered (first launch) or the choice
            // changed on another box: size it for the page it is actually showing.
            if changed && self.window?.isVisible == true { self.applyFrame() }
        }
    }

    func fillOpensOn(_ menu: NSMenu) {
        menu.removeAllItems()
        guard let pref = opensOn else {
            let item = NSMenuItem(title: "Open the window and sign in first", action: nil, keyEquivalent: "")
            item.isEnabled = false
            menu.addItem(item)
            return
        }
        for choice in pref.choices {
            let item = NSMenuItem(title: choice.label, action: #selector(setOpensOn(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = choice.value
            item.state = choice.value == pref.current ? .on : .off
            menu.addItem(item)
        }
    }

    @objc func setOpensOn(_ sender: NSMenuItem) {
        guard let value = sender.representedObject as? String,
              let body = try? JSONSerialization.data(withJSONObject: ["opens_on": value]) else { return }
        runnerAppRequest("PATCH", body: body) { [weak self] pref in
            guard let self, let pref else { return }
            self.saveFrame()  // keep the old page's size before switching
            self.opensOn = pref
            if self.window?.isVisible == true {  // show the new page now, at its own size
                self.applyFrame()
                self.loadHome()
            }
        }
    }

    // -- window size per page --
    // Each page keeps its own frame: the supervisor is phone-sized, the IDE wants a big
    // window. The frame is saved as you move or resize it and restored the next time the
    // window opens on that page; the first time, it gets the page's default size.
    var framePage: String { opensOn?.current ?? "supervisor" }

    func applyFrame() {
        guard let win = window else { return }
        let page = framePage
        if win.setFrameUsingName("CanopyRunner.\(page)") { return }
        let screen = (win.screen ?? NSScreen.main)?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let size = page == "supervisor"
            ? NSSize(width: 420, height: 640)
            : NSSize(width: (screen.width * 0.85).rounded(), height: (screen.height * 0.85).rounded())
        win.setFrame(NSRect(x: screen.midX - size.width / 2, y: screen.midY - size.height / 2,
                            width: size.width, height: size.height), display: true)
    }

    func saveFrame() {
        guard let win = window, win.isVisible else { return }
        win.saveFrame(usingName: "CanopyRunner.\(framePage)")
    }

    func windowDidEndLiveResize(_ n: Notification) { saveFrame() }
    func windowDidMove(_ n: Notification) { saveFrame() }
    func windowWillClose(_ n: Notification) { saveFrame() }

    // GET/PATCH /api/me/runner-app/ as the person: with the runner PAT when the box has
    // one, else with the window's own signed-in session (its cookies, plus the CSRF
    // token Django wants on a PATCH). Completes on the main queue; nil on any failure.
    func runnerAppRequest(_ method: String, body: Data?,
                          done: @escaping ((current: String, choices: [(value: String, label: String)])?) -> Void) {
        guard let url = URL(string: baseURL() + "/api/me/runner-app/") else { done(nil); return }
        var req = URLRequest(url: url, timeoutInterval: 10)
        req.httpMethod = method
        req.httpBody = body
        req.httpShouldHandleCookies = false
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        func send() {
            URLSession.shared.dataTask(with: req) { data, resp, _ in
                var pref: (current: String, choices: [(value: String, label: String)])?
                if (resp as? HTTPURLResponse)?.statusCode == 200, let data,
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let current = obj["opens_on"] as? String,
                   let choices = obj["choices"] as? [[String: Any]] {
                    pref = (current, choices.compactMap { c in
                        guard let v = c["value"] as? String, let l = c["label"] as? String else { return nil }
                        return (v, l)
                    })
                }
                DispatchQueue.main.async { done(pref) }
            }.resume()
        }
        if let token = readToken() {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            send(); return
        }
        guard let host = url.host else { done(nil); return }
        web.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in
            let mine = cookies.filter { c in
                let domain = c.domain.hasPrefix(".") ? String(c.domain.dropFirst()) : c.domain
                return host == domain || host.hasSuffix("." + domain)
            }
            for (k, v) in HTTPCookie.requestHeaderFields(with: mine) { req.setValue(v, forHTTPHeaderField: k) }
            if let csrf = mine.first(where: { $0.name.hasPrefix("csrftoken") }) {
                req.setValue(csrf.value, forHTTPHeaderField: "X-CSRFToken")
            }
            req.setValue("\(url.scheme ?? "https")://\(host)", forHTTPHeaderField: "Origin")
            req.setValue(baseURL() + "/", forHTTPHeaderField: "Referer")
            send()
        }
    }

    @objc func openSupervisor() {
        if let url = URL(string: baseURL() + "/supervisor") { NSWorkspace.shared.open(url) }
    }
    @objc func reloadPanel() {
        authenticateThenLoad()  // re-mint the session cookie, then reload the home page
    }
    // The bulletproof hard-refresh, WITHOUT logging you out. "Reload panel" is a
    // normal navigation, so a stale service worker keeps serving the cached bundle.
    // This purges the service-worker registration + every CACHE so the next load must
    // fetch the current page and register the fresh (auto-updating) SW — but it
    // KEEPS cookies (the session) and localStorage (theme/prefs), so the reload comes
    // straight back signed in. No re-mint, no Google login. ("Reload panel" remains the
    // re-auth path for when the session itself needs refreshing.)
    @objc func forceRefresh() {
        let store = web.configuration.websiteDataStore
        var types = WKWebsiteDataStore.allWebsiteDataTypes()
        types.remove(WKWebsiteDataTypeCookies)
        types.remove(WKWebsiteDataTypeLocalStorage)
        store.removeData(ofTypes: types, modifiedSince: Date(timeIntervalSince1970: 0)) { [weak self] in
            self?.loadHome()  // cookie preserved -> already authed -> fresh bundle
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
