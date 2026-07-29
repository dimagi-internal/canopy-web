#!/usr/bin/env bash
# Build "Canopy Runner.app" — a NATIVE (Swift/AppKit) menu-bar control surface.
#
# Why native: the previous pyobjc app launched as generic framework "Python.app" and
# macOS refused to host its status item (it orphaned to the screen corner). A compiled
# Mach-O inside a real bundle gets a real identity, so the status item is hosted like
# any signed app. Icons come from the committed shared assets (assets/brand/) — nothing
# is rendered here. Idempotent; re-run after editing Sources/main.swift.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
BRAND="$REPO/assets/brand"
APP="${CANOPY_RUNNER_APP:-$HOME/Applications/Canopy Runner.app}"

[ -f "$BRAND/AppIcon.icns" ] || { echo "ERROR: run assets/brand/generate.py first (missing AppIcon.icns)" >&2; exit 1; }

echo "==> compile (Swift)"
BUILD="$HERE/.build"; mkdir -p "$BUILD"
swiftc -O "$HERE/Sources/main.swift" -o "$BUILD/CanopyRunner" -framework AppKit -framework WebKit

echo "==> assemble bundle ($APP)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/CanopyRunner" "$APP/Contents/MacOS/CanopyRunner"
cp "$BRAND/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$BRAND"/menubar-tree*.png "$APP/Contents/Resources/"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Canopy Runner</string>
  <key>CFBundleDisplayName</key><string>Canopy Runner</string>
  <key>CFBundleIdentifier</key><string>com.canopy.runner.menubar</string>
  <key>CFBundleVersion</key><string>2.0</string>
  <key>CFBundleShortVersionString</key><string>2.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>CanopyRunner</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><false/>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
</dict>
</plist>
PLIST
echo "APPL????" > "$APP/Contents/PkgInfo"

# Ad-hoc sign so the compiled binary is a proper, LaunchServices-hostable app.
codesign --force --sign - "$APP" 2>/dev/null || echo "  (codesign skipped)"

# Nudge LaunchServices/Spotlight to pick up the icon + registration.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$APP" 2>/dev/null || true

# Auto-start on login (the durable fix for "the menu-bar icon disappeared"). A user
# LaunchAgent that `open`s the app at login — `open` is idempotent, so it activates an
# already-running instance rather than spawning a duplicate, and the app respects Quit
# (no KeepAlive). Bootstrapping it also launches the app NOW, in the Aqua session.
# Opt out with CANOPY_MENUBAR_AUTOSTART=0.
AGENT="$HOME/Library/LaunchAgents/com.canopy.runner.menubar.plist"
if [ "${CANOPY_MENUBAR_AUTOSTART:-1}" = "1" ]; then
  echo "==> auto-start on login (LaunchAgent)"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.canopy.runner.menubar</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>$APP</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
PLIST
  # Reload so it's active now and at every login (bootout is harmless if not loaded).
  launchctl bootout "gui/$(id -u)/com.canopy.runner.menubar" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENT" 2>/dev/null \
    || echo "    (could not bootstrap now — it will still load at next login)"
  echo "    launches at login. Remove with:"
  echo "      launchctl bootout gui/\$(id -u)/com.canopy.runner.menubar; rm \"$AGENT\""
else
  echo "==> auto-start skipped (CANOPY_MENUBAR_AUTOSTART=0)"
fi

echo "==> done. Launch from Spotlight: 'Canopy Runner'  (or: open \"$APP\")"
