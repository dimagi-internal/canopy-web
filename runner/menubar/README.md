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

- **Status tree**, tinted by runner state (green=running, amber=paused, red=stopped),
  read from `~/.canopy/` (the `PAUSED` sentinel, the heartbeat file, launchctl).
- **Left-click → the fleet panel**: a `WKWebView` loading the deployed `/supervisor`
  React surface — the *same* app the phone PWA and desktop browser load, so there are
  zero duplicated components. Authenticated seamlessly by minting a session from the
  runner's PAT (the credential it shares with the daemon), so there's no separate login.
- **Right-click → local controls**: Pause/Resume (writes the `PAUSED` sentinel the
  daemon honors), Start/Stop daemon (launchctl), Open Supervisor in browser, Reload,
  Open Log, Quit.

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
