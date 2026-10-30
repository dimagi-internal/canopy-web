# canopy-device-key (Secure Enclave device key)

A tiny Swift CLI that holds this Mac's **device key** for Canopy Vault
(dimagi-internal/canopy#946, canopy-web#1538). canopy-web hands an agent's
credentials only to an approved device (a zero-knowledge read needs its proof); the
proof is an RFC 9449 DPoP JWS on `GET /credentials/resolve`, signed by this key.

```
canopy-device-key info                  # {"secure_enclave_available", "key_present", "path", "kind"}
canopy-device-key create                # idempotent; {"kind", "created", "jwk"}
canopy-device-key public-jwk            # {"kty":"EC","crv":"P-256","x","y"}
canopy-device-key sign <base64url>      # raw r||s (64 bytes) as base64url — ES256
canopy-device-key ecdh-create           # idempotent; {"kind", "created", "jwk"} — the ZK key
canopy-device-key ecdh-public-jwk       # its public JWK
canopy-device-key ecdh-derive <epk>     # Z (32-byte shared x-coordinate), base64url;
                                        # epk = base64url X9.62 uncompressed P-256 point
```

**Zero-knowledge vault (canopy-web#1550).** A second, separate enclave key —
`SecureEnclave.P256.KeyAgreement.PrivateKey`, same access control, stored as
`~/.canopy/device-key/se-ecdh.key` (0600) — that a zero-knowledge agent's vault key is
ECDH-ES-wrapped to. It only answers `ecdh-derive`: Z for a wrap's ephemeral point; canopy's
`orchestrator/vault_zk.py` does the HKDF + AES-GCM unwrap. Smoke-tested on a real enclave
by wrapping a random key with `docs/vault/zk-v1/zk_v1_reference.py` to its public key and
unwrapping through `ecdh-derive`.

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
