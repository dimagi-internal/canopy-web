# Canopy Runner (menu-bar app)

A native macOS menu-bar control surface for the runner daemon. Swift/AppKit, ~1 file.

## Why native (not Python/pyobjc)

The previous menu-bar app was pyobjc, launched via a bundle that `exec`'d framework
Python. macOS saw a generic **`Python.app`** identity and refused to host its status
item — the icon orphaned to the screen corner (x=0,y=0) instead of the menu bar. This
was proven from the app's own startup log: `applicationDidFinishLaunching` completed
cleanly, the item reported `isVisible=true`, but its window never entered the bar. A
real compiled Mach-O in a real signed bundle gets a real identity, so macOS hosts the
status item like any other app.

## What it does

- **Status tree**, tinted by runner state, read from `~/.canopy/` (launchctl, the
  `PAUSED` sentinel, the heartbeat file, and `server-status.json`: canopy's answer to the
  daemon's last heartbeat, written by `Client.heartbeat`). Green only while canopy is
  accepting the box; amber = paused; **red = stopped, or canopy refusing it** (a revoked
  or expired token, 401/403/404 — the menu says which and since when); orange = no local
  heartbeat, or canopy unreachable for 2 min.
- **Left-click → the fleet panel**: a `WKWebView` loading the deployed `/supervisor`
  React surface — the *same* app the phone PWA and desktop browser load, so there are
  zero duplicated components. Authenticated seamlessly by minting a session from the
  runner's PAT (the credential it shares with the daemon), so there's no separate login.
- **Right-click → local controls**: Pause/Resume (writes the `PAUSED` sentinel the
  daemon honors), Start/Stop daemon (launchctl), Open Supervisor in browser, Reload,
  Open Log, Quit.
- **Close sessions** (right-click submenu): lists this box's open sessions per project
  (`ace — 6 sessions…`) plus "All sessions", and after a confirm closes them with
  `canopy-runner close-sessions --project <p> | --all` — each one the same close as the
  phone's (the emdash task is deleted; transcripts and canopy's record are kept). The
  same command works from a terminal: `canopy-runner close-sessions --list`.
- **A new key on this Mac needs your approval…** (top of the menu, bold): shown while a
  device key on this box waits for a person — a new one, or an active one whose new ECDH
  key does. canopy says so in its heartbeat reply (`key_waiting`, copied into
  `server-status.json`); the item opens Settings → Your machines at that key. Canopy
  also emails and pushes it, but that is easy to miss.
- **Update runner now** (right-click): starts the updater job (`com.canopy.runner.updater`)
  instead of waiting up to 30 min for its timer — the same job, so it installs only what
  canopy has deployed and respects `~/.canopy/hold-updates`. Reads "Updating the runner…"
  while it runs; the result is in `~/.canopy/updater.log`.

## Passkeys open in your browser

The web view can use no passkey: WebKit gives WebAuthn only to an app holding Apple's
associated-domains entitlement (a Developer ID build with a provisioning profile, plus
an `apple-app-site-association` file on canopy's host), and the window's session is
minted from the runner's token, on which canopy refuses every step-up and approval by
design (`apps/passkeys/stepup.py`). Making WebAuthn work here would still fail on the
server. So the app says what it is — `CanopyRunner/<version>` in its user agent — and
the page hands each passkey moment to your default browser through `window.open`
(routed to `NSWorkspace` by the `WKUIDelegate`), then follows the result: an act-as
approval, a page's act-as-you permission (`/confirm/page-permission/<id>`), or a
step-up-gated action redone in the browser. `frontend/src/auth/passkeyHere.ts` holds
the logic; `/api/me/`'s `passkeys_here` covers an app built before the marker.

**Any other host of canopy's pages needs the same two things** — a Windows tray app on
WebView2 included: put `CanopyRunner/` in the user agent
(`CoreWebView2Settings.UserAgent`), and send new-window requests to the system browser
(`NewWindowRequested` → the default browser). Nothing else is platform-specific.

## Relationship to the daemon

Deliberately a **separate process** from the Python runner daemon (`canopy_runner`):
the daemon must run headless (launchd, cloud Fargate, SSH) — fusing a GUI in would kill
the cloud-runner path. They **share** the credential (`~/.canopy/runner.json` →
the workbench PAT) and the state dir (`~/.canopy/`). Shared data, separate processes.

## Build

```bash
bash runner/menubar/build.sh
```

Compiles `Sources/main.swift`, assembles `~/Applications/Canopy Runner.app` (icons
copied from the committed `assets/brand/`), ad-hoc signs it, and registers it with
Launch Services. Launch from Spotlight: **Canopy Runner**. Re-run after editing the
source. Requires the Xcode command-line tools (`swiftc`).

## Auto-start on login

The build also installs a user LaunchAgent (`~/Library/LaunchAgents/com.canopy.runner.menubar.plist`)
that `open`s the app at login — the durable fix for "the menu-bar icon disappeared."
`open` is idempotent (it activates a running instance instead of duplicating), and
the app respects Quit (no KeepAlive). It launches the app immediately on install too.

Opt out: `CANOPY_MENUBAR_AUTOSTART=0 bash build.sh`. Remove an installed agent:

```bash
launchctl bootout gui/$(id -u)/com.canopy.runner.menubar
rm ~/Library/LaunchAgents/com.canopy.runner.menubar.plist
```
