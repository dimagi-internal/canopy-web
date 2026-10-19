#!/usr/bin/env bash
# Smoke-test a built macOS release the way a person gets it: from the DMG.
#
#   runner/packaging/smoke/smoke-macos.sh <dist-dir> <arm64|x86_64>
#
# <dist-dir> holds build_app.sh's output (the .dmg, the .app.zip and macos-<arch>.json).
# The app is copied out of the DMG to a temp dir and exercised there with HOME
# pointed at a temp dir too: nothing here reads or writes the real ~/.canopy,
# ~/Applications or ~/Library/LaunchAgents, and no launchd job is touched — so it is
# safe on a machine that runs a live runner. `canopy-runner setup` is NOT run (it
# would install launchd jobs); its presence is checked with --help.
set -uo pipefail

DIST="$(cd "${1:?usage: smoke-macos.sh <dist-dir> <arm64|x86_64>}" && pwd)"
ARCH="${2:?arch}"
PLATFORM="macos-$ARCH"
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSIONS="$HERE/../versions.json"
DMG="$DIST/Canopy-Runner-$PLATFORM.dmg"
ZIP="$DIST/Canopy-Runner-$PLATFORM.app.zip"
RECORD="$DIST/$PLATFORM.json"

TMP="$(mktemp -d)"
MNT="$TMP/mnt"
cleanup() { hdiutil detach -quiet "$MNT" 2>/dev/null || true; rm -rf "$TMP"; }
trap cleanup EXIT

FAILS=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }
check() {  # <description> <command...>
  local desc="$1"; shift
  local out
  if out="$("$@" 2>&1)"; then pass "$desc${out:+ — $(echo "$out" | head -1 | cut -c1-140)}"
  else fail "$desc — $(echo "$out" | tail -3 | tr '\n' ' ' | cut -c1-300)"; fi
}
rec() { /usr/bin/plutil -extract "$1" raw -o - "$RECORD" 2>/dev/null; }

for f in "$DMG" "$ZIP" "$RECORD"; do [ -f "$f" ] || { echo "missing $f" >&2; exit 2; }; done
echo "== $PLATFORM: $(basename "$DMG") $(du -h "$DMG" | cut -f1), $(basename "$ZIP") $(du -h "$ZIP" | cut -f1)"
echo "== signed=$(rec signed) notarized=$(rec notarized)"

# The record's checksums must describe the files actually published.
check "installer sha256 matches the record" \
  test "$(shasum -a 256 "$DMG" | cut -d' ' -f1)" = "$(rec installer.sha256)"
check "update sha256 matches the record" \
  test "$(shasum -a 256 "$ZIP" | cut -d' ' -f1)" = "$(rec update.sha256)"

mkdir -p "$MNT" "$TMP/home" "$TMP/devkey"
hdiutil attach -quiet -nobrowse -readonly -noautoopen -mountpoint "$MNT" "$DMG" || { echo "cannot mount $DMG" >&2; exit 1; }
check "dmg holds the app and an Applications link" test -d "$MNT/Canopy Runner.app" -a -L "$MNT/Applications"
ditto "$MNT/Canopy Runner.app" "$TMP/Canopy Runner.app"
hdiutil detach -quiet "$MNT"
APP="$TMP/Canopy Runner.app"
RT="$APP/Contents/Resources/runtime"

SHA="$(/usr/bin/plutil -extract sha raw -o - "$RT/release.json")"
check "release.json names $PLATFORM" test "$(/usr/bin/plutil -extract platform raw -o - "$RT/release.json")" = "$PLATFORM"
check "release.json sha is the record's" test "$SHA" = "$(rec sha)"
check "CFBundleVersion is the short sha" \
  test "$(/usr/bin/plutil -extract CFBundleVersion raw -o - "$APP/Contents/Info.plist")" = "${SHA:0:12}"

check "codesign --verify --deep --strict (app)" codesign --verify --deep --strict --verbose=2 "$APP"
if [ "$(rec notarized)" = "true" ]; then
  check "spctl accepts the dmg (install)" spctl -a -vvv -t install "$DMG"
  check "spctl accepts the app (exec)" spctl -a -vvv -t exec "$APP"
  check "app has a stapled ticket" xcrun stapler validate "$APP"
  check "dmg has a stapled ticket" xcrun stapler validate "$DMG"
fi

for bin in "$APP/Contents/MacOS/CanopyRunner" "$RT/python/bin/python3.12" "$RT/node/bin/node" "$RT/bin/canopy-device-key"; do
  check "$(basename "$bin") is $ARCH" sh -c "lipo -archs '$bin' | tr ' ' '\n' | grep -qx '$ARCH'"
done

# Everything below runs as the shim runs it, in a throwaway HOME.
export HOME="$TMP/home"
export CANOPY_DEVICE_KEY_DIR="$TMP/devkey"
SHIM_PATH="$RT/node/bin:$RT/acp/bin:/usr/bin:/bin:/usr/sbin:/sbin"

check "canopy-runner --version names the stamped sha" \
  sh -c "'$RT/bin/canopy-runner' --version | tee /dev/stderr | grep -q 'rev ${SHA:0:12}'"
check "bundled python imports the runner and its deps" \
  "$RT/python/bin/python3" -I -B -c "import canopy_runner, canopy_acp, canopy_cron, canopy_transcript, cryptography, websocket; from canopy_runner import _build_info as b; assert b.SHA == '$SHA', b.SHA; print('ok', getattr(b, 'PLATFORM', '<no PLATFORM>'))"
check "_build_info.PLATFORM is $PLATFORM" \
  "$RT/python/bin/python3" -I -B -c "from canopy_runner import _build_info as b; assert getattr(b, 'PLATFORM', None) == '$PLATFORM'"
NODE_WANT="$(/usr/bin/plutil -extract node.version raw -o - "$VERSIONS")"
check "bundled node is $NODE_WANT" sh -c "test \"\$('$RT/node/bin/node' --version)\" = '$NODE_WANT'"
check "bundled npm runs" env PATH="$SHIM_PATH" "$RT/node/bin/npm" --version
CDP="$("$RT/python/bin/python3" -I -B -c 'import canopy_runner, pathlib; print(pathlib.Path(canopy_runner.__file__).parent / "cdp")')"
check "CDP sidecar deps installed (playwright-core imports)" \
  sh -c "cd '$CDP' && '$RT/node/bin/node' --input-type=module -e \"await import('playwright-core'); console.log('ok')\""
check "ACP adapter resolvable by canopy_acp" env PATH="$SHIM_PATH" \
  "$RT/python/bin/python3" -I -B -c "from canopy_acp.client import find_adapter; a = find_adapter(); assert a, a; print(a[-1])"
CLAUDE_BIN="$(find "$RT/acp" -type f -name claude -perm -u+x | head -1)"
if [ -n "$CLAUDE_BIN" ]; then
  check "agent SDK's claude binary runs" "$CLAUDE_BIN" --version
else
  fail "agent SDK's claude binary not found under runtime/acp"
fi
check "canopy-device-key info" "$RT/bin/canopy-device-key" info

# The runner agent's commands (setup / pair --browser / self-update): present?
for sub in setup self-update; do
  if "$RT/bin/canopy-runner" "$sub" --help >/dev/null 2>&1; then
    pass "canopy-runner $sub exists (not run: it installs launchd jobs)"
  else
    echo "SKIP  canopy-runner $sub is not in this build"
  fi
done

# Nothing above may have written into the bundle (a .pyc, an npm install): that
# would break the signature on a person's machine.
check "bundle still verifies after running it" codesign --verify --deep --strict "$APP"
check "nothing wrote to the throwaway HOME's ~/Library/LaunchAgents" test ! -e "$HOME/Library/LaunchAgents"

echo
if [ "$FAILS" -gt 0 ]; then echo "== $FAILS check(s) FAILED"; exit 1; fi
echo "== all checks passed"
