#!/usr/bin/env bash
# Build canopy-device-key — this Mac's Secure Enclave device key for Canopy Vault
# (canopy#946). A plain CLI, not an app: compiled with swiftc (like runner/menubar) and
# installed to ~/.canopy/bin/canopy-device-key, where canopy's orchestrator/device_key.py
# looks for it. Ad-hoc signed; it needs no entitlement (the key lives as an enclave
# dataRepresentation blob in ~/.canopy/device-key/se.key, not in the keychain).
# Idempotent; re-run after editing Sources/main.swift.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${CANOPY_DEVICE_KEY_BIN:-$HOME/.canopy/bin/canopy-device-key}"

echo "==> compile canopy-device-key (Swift)"
BUILD="$HERE/.build"; mkdir -p "$BUILD"
swiftc -O "$HERE/Sources/main.swift" -o "$BUILD/canopy-device-key" -framework CryptoKit -framework Security

mkdir -p "$(dirname "$OUT")"
install -m 755 "$BUILD/canopy-device-key" "$OUT"
codesign --force --sign - "$OUT" 2>/dev/null || echo "  (codesign skipped)"
echo "==> installed $OUT"
"$OUT" info
