# Runner Menu-Bar Dock Icon + Shared Window — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Canopy Runner menu-bar app a Dock icon (an always-visible "it's running" signal) and make both the Dock-icon click and the menu-bar tree click open one shared resizable window hosting the existing `/supervisor` WKWebView.

**Architecture:** Two files in `packages/canopy-runner-menubar/`. Flip the app from `.accessory`/`LSUIElement` to a standard `.regular` app so a Dock icon appears; replace the menu-bar `NSPopover` with a single reusable `NSWindow` that both entry points show/focus; keep the app alive when that window closes (it's still a background runner controller).

**Tech Stack:** Native Swift / AppKit / WebKit. Built by `build.sh` (invokes `swiftc`); no unit-test harness — verification is compiling + driving the real app.

**Spec:** `docs/superpowers/specs/2026-07-17-runner-menubar-dock-icon-design.md` — read it before Task 1.

## Global Constraints

- **Two files only:** `packages/canopy-runner-menubar/Sources/main.swift` and `packages/canopy-runner-menubar/build.sh`. Do not touch the daemon, the schedule system, or the web `/supervisor` surface.
- **Preserve** the 5-second status poll that tints the menu-bar tree (`rebuild()`), the right-click local-controls menu (`buildMenu`/`currentState`), the shared-PAT auth (`authenticateThenLoad`/`loadSupervisor`), the `.default()` WKWebView data store, and single-instance behavior.
- **The window is reused, not recreated** — set `isReleasedWhenClosed = false`, or reopening after a close crashes on a deallocated window.
- **Closing the window must not quit the app.** `applicationShouldTerminateAfterLastWindowClosed` returns `false`.
- Build needs the Swift toolchain / Xcode Command Line Tools (`xcode-select -p` must resolve; `swiftc --version` must work).

---

### Task 1: Dock icon + shared window (the code change)

**Files:**
- Modify: `packages/canopy-runner-menubar/build.sh` (the Info.plist `LSUIElement` line)
- Modify: `packages/canopy-runner-menubar/Sources/main.swift`
- Test: compile via `./build.sh` (there is no unit harness; a clean `swiftc` build + assembled `.app` is the automatable gate; behavior is verified in Task 2)

**Interfaces:**
- Consumes (unchanged, already in the file): `web: WKWebView!`, `authenticateThenLoad()`, `loadSupervisor()`, `statusItem`, `buildMenu(_:)`, `currentState()`, `rebuild()`, `loadTree()`, `@objc func quit()`.
- Produces: `@objc func showWindow()`, `func buildWindow()`, `var window: NSWindow?`, and the two `NSApplicationDelegate` methods below.

- [ ] **Step 1: Confirm the toolchain + a clean baseline build**

Run:
```bash
cd packages/canopy-runner-menubar
swiftc --version && xcode-select -p
./build.sh
```
Expected: `swiftc` prints a version, `xcode-select -p` resolves a path, and `build.sh` completes, (re)assembling `~/Applications/Canopy Runner.app`. If the toolchain is missing, STOP and report — nothing else in this task can be verified without a compile.

- [ ] **Step 2: Flip `LSUIElement` in build.sh**

In `packages/canopy-runner-menubar/build.sh`, change the Info.plist line (currently `build.sh:42`):

```
  <key>LSUIElement</key><true/>
```
to:
```
  <key>LSUIElement</key><false/>
```

- [ ] **Step 3: Switch the activation policy to `.regular`**

In `Sources/main.swift`, the bootstrap tail (currently line 341):

```swift
app.setActivationPolicy(.accessory)  // menu-bar only, no dock icon
```
becomes:
```swift
app.setActivationPolicy(.regular)  // Dock icon = always-visible "it's running" signal;
                                   // the notch clips the menu-bar tree, so the Dock icon
                                   // is the reliable running signal. Standard app: Dock +
                                   // Cmd-Tab + an app menu when focused.
```

- [ ] **Step 4: Replace the popover with a reusable window**

In `Controller`, replace the popover property (currently line 94):

```swift
    let popover = NSPopover()
```
with:
```swift
    var window: NSWindow?
```

Then replace `buildPopover()` (the whole method, currently lines ~116-126) with `buildWindow()`:

```swift
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
        // Reused across opens: closing HIDES it (see the terminate delegate below), so it
        // must not be deallocated on close or the next open dereferences a freed window.
        win.isReleasedWhenClosed = false
        win.center()
        window = win
    }
```

- [ ] **Step 5: Replace `togglePopover()` with `showWindow()`**

Replace the whole `togglePopover()` method (currently lines ~140-149) with:

```swift
    @objc func showWindow() {
        // ALWAYS open on the supervisor home, never wherever the last session navigated to.
        // First open mints + injects the session cookie; after that we just reload
        // /supervisor (cheap, no re-mint).
        if authed { loadSupervisor() } else { authenticateThenLoad() }
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }
```

- [ ] **Step 6: Point the launch + menu-bar left-click at the new methods**

In `applicationDidFinishLaunching`, change `buildPopover()` (line ~100) to `buildWindow()`.

In `statusClicked`, the non-right-click branch (line ~135) `togglePopover()` → `showWindow()`:

```swift
        } else {
            showWindow()
        }
```

- [ ] **Step 7: Add the Dock-reopen + no-quit-on-close delegate methods, and a minimal main menu**

Add these three methods to `Controller` (e.g. right after `applicationDidFinishLaunching`):

```swift
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
```

And call `buildMainMenu()` from `applicationDidFinishLaunching` (after `buildWindow()`).

- [ ] **Step 8: Build — the compile gate**

Run:
```bash
cd packages/canopy-runner-menubar && ./build.sh
```
Expected: clean `swiftc` build (no errors), `.app` re-assembled. If it references `popover` anywhere still (a missed rename) the compile fails — grep `grep -n popover Sources/main.swift` should return nothing.

- [ ] **Step 9: Commit**

```bash
git add packages/canopy-runner-menubar/Sources/main.swift packages/canopy-runner-menubar/build.sh
git commit -m "feat(runner): Dock icon + shared window for the menu-bar app"
```

---

### Task 2: Deploy to the running app + verify behavior

**Files:** none (operational — rebuild is in Task 1; this replaces the running instance and verifies).

**Interfaces:** consumes the `.app` produced by Task 1's `build.sh`.

- [ ] **Step 1: Replace the running instance**

The app auto-starts on login (#272) and one instance is running. Quit it, then relaunch the freshly built bundle:

```bash
osascript -e 'quit app "Canopy Runner"' 2>/dev/null; sleep 2
open "$HOME/Applications/Canopy Runner.app"
sleep 3
pgrep -fl "MacOS/CanopyRunner" | grep -v grep | head -1
```
Expected: a single `CanopyRunner` process running.

- [ ] **Step 2: Verify the Dock icon + shared window (drive the real app)**

Confirm, in order:
1. **Dock icon is present** while the app runs (the "it's running" signal).
2. **Click the Dock icon** → a window opens showing `/supervisor`, authenticated (no Google login prompt — the PAT-mint path ran).
3. **Close that window, then click the menu-bar tree icon** → the *same* window reopens on `/supervisor` (one window, not a second).
4. **Close the window again** → the app stays running: Dock icon remains, the menu-bar tree remains, `pgrep -fl MacOS/CanopyRunner` still shows the process.
5. **Right-click the menu-bar tree** → the local-controls menu still appears; **Quit** terminates the app (Dock icon disappears, process gone).
6. Relaunch and confirm the tree icon still tints on the 5-second poll (status still live).

Report the result of each. If any fails, it's a Task 1 defect — fix there, rebuild, redeploy.

- [ ] **Step 3: (No commit)** — Task 2 changes no tracked files; it verifies the deploy.

---

## Final Verification

- [ ] `grep -n popover packages/canopy-runner-menubar/Sources/main.swift` → no output (popover fully removed).
- [ ] `build.sh` compiles clean and assembles the `.app`.
- [ ] All six behavior checks in Task 2 Step 2 pass on the running app.
- [ ] `git status` clean; the only changed tracked files are `Sources/main.swift` + `build.sh` (+ the spec/plan docs).

## Non-goals

Persisting window size/position across launches (center-on-open is enough), Cmd-Tab suppression, and any change to the daemon / schedules / `/supervisor` web surface.
