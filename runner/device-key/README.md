# canopy-device-key (Secure Enclave device key)

A tiny Swift CLI that holds this Mac's **device key** for Canopy Vault
(dimagi-internal/canopy#946, canopy-web#1538). canopy-web can require that an agent's
credentials be read only from an approved device (`Agent.require_device_binding`); the
proof is an RFC 9449 DPoP JWS on `GET /credentials/resolve`, signed by this key.

```
canopy-device-key info                  # {"secure_enclave_available", "key_present", "path", "kind"}
canopy-device-key create                # idempotent; {"kind", "created", "jwk"}
canopy-device-key public-jwk            # {"kty":"EC","crv":"P-256","x","y"}
canopy-device-key sign <base64url>      # raw r||s (64 bytes) as base64url — ES256
```

- CryptoKit `SecureEnclave.P256.Signing.PrivateKey`, access control `.privateKeyUsage`
  only (no user presence — unattended turns must never prompt), `AfterFirstUnlockThisDeviceOnly`.
- Only the key's `dataRepresentation` is on disk: `~/.canopy/device-key/se.key` (0600, dir
  0700). It works only inside this Mac's enclave and needs no keychain entitlement — an
  ad-hoc-signed CLI cannot use the data-protection keychain.
- `$CANOPY_DEVICE_KEY_DIR` overrides the directory (tests, a smoke test in a temp dir).

The JWS itself is built by canopy (`orchestrator/device_key.py`), which calls this helper
to sign and falls back to a software P-256 key (`sw.pem`, `kind=software`) where there is
no enclave. Enrollment (`POST /api/runner-device-keys/`) is `canopy vault device --enroll`,
which the laptop runner runs at startup.

## Build

`bash runner/device-key/build.sh` → `~/.canopy/bin/canopy-device-key`
(`$CANOPY_DEVICE_KEY_BIN` overrides). `install-runner.sh` does this on macOS whenever the
source changes, beside the menu-bar app. Requires the Xcode command-line tools (`swiftc`).
