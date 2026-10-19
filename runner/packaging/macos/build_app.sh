#!/usr/bin/env bash
# Assemble, sign, notarize and package "Canopy Runner.app" for one arch.
#
#   runner/packaging/macos/build_app.sh <arm64|x86_64> <payload-dir> <out-dir>
#
# <payload-dir> is build_payload.py's output for macos-<arch>. Writes to <out-dir>:
#   Canopy Runner.app                       the assembled (signed) app
#   Canopy-Runner-macos-<arch>.dmg          the installer (a person downloads this)
#   Canopy-Runner-macos-<arch>.app.zip      the update artifact (self-update unpacks it)
#   macos-<arch>.json                       {platform, signed, notarized, installer, update}
#                                           — the publish job's input for manifest.json
#
# Signing runs only when its secrets are in the environment, and the record says
# what actually happened — an ad-hoc signature is recorded as signed=false:
#   MACOS_CERTIFICATE_P12_BASE64, MACOS_CERTIFICATE_PASSWORD, MACOS_SIGNING_IDENTITY
#     a "Developer ID Application" certificate + key exported as .p12
#   APPLE_API_KEY_P8_BASE64, APPLE_API_KEY_ID, APPLE_API_ISSUER_ID
#     an App Store Connect API key, for notarytool (needs signing too)
# With none of them the app is ad-hoc signed (codesign -s -) so it runs on the
# machine that built it, and the DMG is plain.
#
# Never touches ~/Applications or launchd: unlike runner/menubar/build.sh (the
# developer path, which installs and relaunches the app) this only builds files.
set -euo pipefail

ARCH="${1:?usage: build_app.sh <arm64|x86_64> <payload-dir> <out-dir>}"
PAYLOAD="$(cd "${2:?payload dir}" && pwd)"
OUT="${3:?out dir}"
case "$ARCH" in arm64|x86_64) ;; *) echo "arch must be arm64 or x86_64" >&2; exit 2 ;; esac
PLATFORM="macos-$ARCH"

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
BRAND="$REPO/assets/brand"
ENTITLEMENTS="$HERE/runtime.entitlements"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
APP="$OUT/Canopy Runner.app"
DMG="$OUT/Canopy-Runner-$PLATFORM.dmg"
ZIP="$OUT/Canopy-Runner-$PLATFORM.app.zip"
WORK="$(mktemp -d)"
KEYCHAIN=""
cleanup() {
  [ -n "$KEYCHAIN" ] && security delete-keychain "$KEYCHAIN" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

json() { /usr/bin/plutil -extract "$1" raw -o - "$PAYLOAD/release.json"; }
[ "$(json platform)" = "$PLATFORM" ] || { echo "payload is $(json platform), not $PLATFORM" >&2; exit 1; }
SHA="$(json sha)"
COMMITTED_AT="$(json committed_at)"
# Monotonic in the runner's commit order and the same for every build of one sha,
# which is what LaunchServices and a human comparing two copies both want.
SHORT_VERSION="1.0.$COMMITTED_AT"

echo "==> compile ($ARCH) menu-bar app + canopy-device-key"
swiftc -O -target "$ARCH-apple-macos13" "$REPO/runner/menubar/Sources/main.swift" \
  -o "$WORK/CanopyRunner" -framework AppKit -framework WebKit
swiftc -O -target "$ARCH-apple-macos13" "$REPO/runner/device-key/Sources/main.swift" \
  -o "$WORK/canopy-device-key" -framework CryptoKit -framework Security

echo "==> assemble $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$WORK/CanopyRunner" "$APP/Contents/MacOS/CanopyRunner"
cp "$BRAND/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
cp "$BRAND"/menubar-mark*.png "$APP/Contents/Resources/"
# ditto keeps the payload's symlinks (python3 -> python3.12, node's bin/npm) as links.
ditto "$PAYLOAD" "$APP/Contents/Resources/runtime"
install -m 755 "$WORK/canopy-device-key" "$APP/Contents/Resources/runtime/bin/canopy-device-key"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Canopy Runner</string>
  <key>CFBundleDisplayName</key><string>Canopy Runner</string>
  <key>CFBundleIdentifier</key><string>com.canopy.runner.menubar</string>
  <key>CFBundleVersion</key><string>${SHA:0:12}</string>
  <key>CFBundleShortVersionString</key><string>$SHORT_VERSION</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>CanopyRunner</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSUIElement</key><false/>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.developer-tools</string>
</dict>
</plist>
PLIST
echo "APPL????" > "$APP/Contents/PkgInfo"
plutil -lint "$APP/Contents/Info.plist" >/dev/null
# Finder info / quarantine detritus makes codesign refuse the bundle.
xattr -cr "$APP"

SIGNED=false
NOTARIZED=false

# Every Mach-O in the bundle, deepest first (nested code is signed before what
# contains it). Found by content, not by name: python's .so, node's .node addons,
# dylibs, and the executables npm packages vendor (the Claude Code binary, ripgrep)
# carry no reliable extension.
macho_files() {
  find "$APP/Contents" -type f ! -path "$APP/Contents/MacOS/CanopyRunner" -print0 \
    | xargs -0 file --mime-type -N -F $'\t' \
    | awk -F'\t' '$2 ~ /x-mach-binary/ { print $1 }' \
    | sed 's/ *$//' \
    | awk -F/ '{ print NF "\t" $0 }' | sort -rn -k1,1 | cut -f2-
}

if [ -n "${MACOS_CERTIFICATE_P12_BASE64:-}" ] && [ -n "${MACOS_CERTIFICATE_PASSWORD:-}" ] \
   && [ -n "${MACOS_SIGNING_IDENTITY:-}" ]; then
  echo "==> import the Developer ID certificate into a temporary keychain"
  KEYCHAIN="$WORK/signing.keychain-db"
  KC_PASS="$(uuidgen)"
  echo "$MACOS_CERTIFICATE_P12_BASE64" | base64 --decode > "$WORK/cert.p12"
  security create-keychain -p "$KC_PASS" "$KEYCHAIN"
  security set-keychain-settings -lut 21600 "$KEYCHAIN"
  security unlock-keychain -p "$KC_PASS" "$KEYCHAIN"
  security import "$WORK/cert.p12" -k "$KEYCHAIN" -P "$MACOS_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KC_PASS" "$KEYCHAIN" >/dev/null
  # Prepend to the search list (codesign finds the identity there); the system
  # keychains stay so the Apple intermediates resolve.
  EXISTING=()
  while IFS= read -r kc; do EXISTING+=("$kc"); done < <(security list-keychains -d user | sed -e 's/^ *"//' -e 's/" *$//')
  security list-keychains -d user -s "$KEYCHAIN" ${EXISTING[@]+"${EXISTING[@]}"}
  rm -f "$WORK/cert.p12"
  SIGN=(codesign --force --timestamp --options runtime --keychain "$KEYCHAIN" --sign "$MACOS_SIGNING_IDENTITY")

  echo "==> sign every Mach-O in the runtime (hardened runtime + runtime.entitlements)"
  macho_files > "$WORK/macho.txt"
  echo "    $(wc -l < "$WORK/macho.txt" | tr -d ' ') files"
  # The device key needs no exception; everything else in the runtime gets the
  # runtime entitlements (see the file for why each one).
  grep -v '/runtime/bin/canopy-device-key$' "$WORK/macho.txt" | tr '\n' '\0' \
    | xargs -0 -n 40 "${SIGN[@]}" --entitlements "$ENTITLEMENTS"
  "${SIGN[@]}" "$APP/Contents/Resources/runtime/bin/canopy-device-key"
  echo "==> sign the app"
  "${SIGN[@]}" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
  SIGNED=true
else
  echo "==> no signing secrets — ad-hoc signature only (this build is NOT signed)"
  codesign --force --sign - "$APP/Contents/Resources/runtime/bin/canopy-device-key"
  codesign --force --sign - "$APP"
  codesign --verify --strict "$APP"
fi

notarize() {  # <file> — submit, wait, fail loudly with Apple's log on anything but Accepted
  local file="$1" out id status
  out="$(xcrun notarytool submit "$file" --key "$WORK/AuthKey.p8" --key-id "$APPLE_API_KEY_ID" \
           --issuer "$APPLE_API_ISSUER_ID" --wait --timeout 45m --output-format json)"
  echo "$out"
  id="$(/usr/bin/plutil -extract id raw -o - - <<<"$out")"
  status="$(/usr/bin/plutil -extract status raw -o - - <<<"$out")"
  if [ "$status" != "Accepted" ]; then
    xcrun notarytool log "$id" --key "$WORK/AuthKey.p8" --key-id "$APPLE_API_KEY_ID" \
      --issuer "$APPLE_API_ISSUER_ID" || true
    echo "::error::notarization of $(basename "$file") ended $status" >&2
    return 1
  fi
}

if [ "$SIGNED" = true ] && [ -n "${APPLE_API_KEY_P8_BASE64:-}" ] && [ -n "${APPLE_API_KEY_ID:-}" ] \
   && [ -n "${APPLE_API_ISSUER_ID:-}" ]; then
  echo "$APPLE_API_KEY_P8_BASE64" | base64 --decode > "$WORK/AuthKey.p8"
  echo "==> notarize the app"
  ditto -c -k --keepParent "$APP" "$WORK/notarize.zip"
  notarize "$WORK/notarize.zip"
  xcrun stapler staple "$APP"
  NOTARIZED=true
else
  echo "==> not notarized (no App Store Connect API key, or the app is not signed)"
fi

echo "==> update artifact $(basename "$ZIP")"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

echo "==> installer $(basename "$DMG")"
STAGE="$WORK/dmg"
mkdir -p "$STAGE"
ditto "$APP" "$STAGE/Canopy Runner.app"
# A drag target for people who expect one; the app moves itself to ~/Applications
# on first launch either way.
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -quiet -volname "Canopy Runner" -srcfolder "$STAGE" -fs HFS+ -format UDZO -ov "$DMG"
if [ "$SIGNED" = true ]; then
  codesign --force --timestamp --keychain "$KEYCHAIN" --sign "$MACOS_SIGNING_IDENTITY" "$DMG"
fi
if [ "$NOTARIZED" = true ]; then
  echo "==> notarize the dmg"
  notarize "$DMG"
  xcrun stapler staple "$DMG"
fi

record() {  # <file> -> {"file","sha256","size"}
  printf '{"file": "%s", "sha256": "%s", "size": %s}' "$(basename "$1")" \
    "$(shasum -a 256 "$1" | cut -d' ' -f1)" "$(stat -f %z "$1")"
}
cat > "$OUT/$PLATFORM.json" <<JSON
{"platform": "$PLATFORM", "sha": "$SHA",
 "installer": $(record "$DMG"),
 "update": $(record "$ZIP"),
 "signed": $SIGNED, "notarized": $NOTARIZED}
JSON
plutil -lint "$OUT/$PLATFORM.json" >/dev/null 2>&1 || python3 -m json.tool "$OUT/$PLATFORM.json" >/dev/null
echo "==> done: $(cat "$OUT/$PLATFORM.json")"
ls -lh "$DMG" "$ZIP"
