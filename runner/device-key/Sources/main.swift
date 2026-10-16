// canopy-device-key — this Mac's Secure Enclave device key for Canopy Vault (canopy#946).
//
// canopy-web can hand an agent's credentials only to an APPROVED device
// (Agent.require_device_binding, canopy-web#1538). The proof is a DPoP JWS signed by a
// P-256 key that never leaves this Mac's Secure Enclave. canopy's Python wrapper
// (orchestrator/device_key.py) builds the JWS and calls this helper to sign it.
//
//   canopy-device-key info                 {"secure_enclave_available": bool, "key_present": bool, ...}
//   canopy-device-key create               idempotent; prints {"kind","created","jwk"}
//   canopy-device-key public-jwk           {"kty":"EC","crv":"P-256","x":…,"y":…}
//   canopy-device-key sign <base64url>     raw r||s (64 bytes), base64url — ES256 over the input
//
// Zero-knowledge vault (canopy-web#1550) — a SEPARATE key-agreement key (enclave keys are
// per-purpose), which an agent's vault key is ECDH-ES-wrapped to:
//
//   canopy-device-key ecdh-create          idempotent; prints {"kind","created","jwk"}
//   canopy-device-key ecdh-public-jwk      {"kty":"EC","crv":"P-256","x":…,"y":…}
//   canopy-device-key ecdh-derive <epk>    Z, base64url: the 32-byte shared x-coordinate of
//                                          ECDH(this key, epk); epk = base64url X9.62
//                                          uncompressed point (65 bytes). canopy's Python
//                                          (orchestrator/vault_zk.py) does HKDF + AES-GCM.
//
// The ECDH key is stored the same way, as ~/.canopy/device-key/se-ecdh.key (0600), also
// with no presence requirement.
//
// The key is made WITHOUT a user-presence requirement: unattended runner turns must never
// prompt. Only its dataRepresentation is on disk (~/.canopy/device-key/se.key, 0600, dir
// 0700): an opaque blob the enclave can use and nothing else can — no keychain entitlement
// needed, which matters because an ad-hoc-signed CLI cannot use the data-protection keychain.
// $CANOPY_DEVICE_KEY_DIR overrides the directory (tests, a smoke test in a temp HOME).
//
// Exit codes: 0 ok · 1 error · 2 usage · 3 no Secure Enclave on this Mac.

import CryptoKit
import Foundation
import Security

func fail(_ message: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write(("canopy-device-key: " + message + "\n").data(using: .utf8)!)
    exit(code)
}

func printJSON(_ object: Any) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes]),
          let text = String(data: data, encoding: .utf8) else { fail("could not encode JSON") }
    print(text)
}

func b64url(_ data: Data) -> String {
    data.base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

func b64urlDecode(_ text: String) -> Data? {
    var s = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
    while s.count % 4 != 0 { s += "=" }
    return Data(base64Encoded: s)
}

func keyDir() -> URL {
    let env = ProcessInfo.processInfo.environment
    if let override = env["CANOPY_DEVICE_KEY_DIR"], !override.isEmpty {
        return URL(fileURLWithPath: (override as NSString).expandingTildeInPath, isDirectory: true)
    }
    let home = env["HOME"].flatMap { $0.isEmpty ? nil : $0 } ?? NSHomeDirectory()
    return URL(fileURLWithPath: home, isDirectory: true)
        .appendingPathComponent(".canopy/device-key", isDirectory: true)
}

func keyFile() -> URL { keyDir().appendingPathComponent("se.key") }

func ecdhKeyFile() -> URL { keyDir().appendingPathComponent("se-ecdh.key") }

/// The key's access control: privateKeyUsage only — no .userPresence / .biometryAny, an
/// unattended turn must never be asked for Touch ID. AfterFirstUnlock so a locked screen
/// does not stop it.
func keyAccess() -> SecAccessControl {
    var error: Unmanaged<CFError>?
    guard let access = SecAccessControlCreateWithFlags(
        kCFAllocatorDefault, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        .privateKeyUsage, &error) else {
        fail("could not build the key's access control: \(String(describing: error?.takeRetainedValue()))")
    }
    return access
}

/// Write an enclave dataRepresentation blob 0600 into the 0700 key directory, atomically.
func storeBlob(_ blob: Data, at file: URL) {
    let fm = FileManager.default
    do {
        try fm.createDirectory(at: keyDir(), withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: keyDir().path)
        let tmp = keyDir().appendingPathComponent(".\(file.lastPathComponent).\(getpid()).tmp")
        guard fm.createFile(atPath: tmp.path, contents: blob,
                            attributes: [.posixPermissions: 0o600]) else {
            fail("could not write \(tmp.path)")
        }
        if rename(tmp.path, file.path) != 0 {
            try? fm.removeItem(at: tmp)
            fail("could not move the key into \(file.path)")
        }
    } catch {
        fail("could not store the key: \(error)")
    }
}

func pointJWK(_ point: Data) -> [String: String] {
    // x963: 0x04 || X (32) || Y (32)
    ["kty": "EC", "crv": "P-256",
     "x": b64url(point.subdata(in: 1..<33)), "y": b64url(point.subdata(in: 33..<65))]
}

func loadECDHKey() -> SecureEnclave.P256.KeyAgreement.PrivateKey? {
    guard let blob = try? Data(contentsOf: ecdhKeyFile()) else { return nil }
    do {
        return try SecureEnclave.P256.KeyAgreement.PrivateKey(dataRepresentation: blob)
    } catch {
        fail("\(ecdhKeyFile().path) is not a key for this Mac's Secure Enclave (\(error))")
    }
}

func createECDH() -> (SecureEnclave.P256.KeyAgreement.PrivateKey, Bool) {
    if let existing = loadECDHKey() { return (existing, false) }
    guard SecureEnclave.isAvailable else { fail("this Mac has no Secure Enclave", 3) }
    let key: SecureEnclave.P256.KeyAgreement.PrivateKey
    do {
        key = try SecureEnclave.P256.KeyAgreement.PrivateKey(accessControl: keyAccess())
    } catch {
        fail("the Secure Enclave refused to make an ECDH key: \(error)")
    }
    storeBlob(key.dataRepresentation, at: ecdhKeyFile())
    return (key, true)
}

func loadKey() -> SecureEnclave.P256.Signing.PrivateKey? {
    guard let blob = try? Data(contentsOf: keyFile()) else { return nil }
    do {
        return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob)
    } catch {
        fail("\(keyFile().path) is not a key for this Mac's Secure Enclave (\(error))")
    }
}

func jwk(_ key: SecureEnclave.P256.Signing.PrivateKey) -> [String: String] {
    // x963: 0x04 || X (32) || Y (32)
    let point = key.publicKey.x963Representation
    return ["kty": "EC", "crv": "P-256",
            "x": b64url(point.subdata(in: 1..<33)), "y": b64url(point.subdata(in: 33..<65))]
}

func create() -> (SecureEnclave.P256.Signing.PrivateKey, Bool) {
    if let existing = loadKey() { return (existing, false) }
    guard SecureEnclave.isAvailable else { fail("this Mac has no Secure Enclave", 3) }
    let key: SecureEnclave.P256.Signing.PrivateKey
    do {
        key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: keyAccess())
    } catch {
        fail("the Secure Enclave refused to make a key: \(error)")
    }
    storeBlob(key.dataRepresentation, at: keyFile())
    return (key, true)
}

let args = CommandLine.arguments.dropFirst()
switch args.first {
case "info":
    printJSON(["secure_enclave_available": SecureEnclave.isAvailable,
               "key_present": FileManager.default.fileExists(atPath: keyFile().path),
               "path": keyFile().path, "kind": "secure-enclave",
               "ecdh_key_present": FileManager.default.fileExists(atPath: ecdhKeyFile().path),
               "ecdh_path": ecdhKeyFile().path])
case "create":
    let (key, created) = create()
    printJSON(["kind": "secure-enclave", "created": created, "jwk": jwk(key)])
case "public-jwk":
    guard let key = loadKey() else { fail("no key at \(keyFile().path) — run `create`") }
    printJSON(jwk(key))
case "sign":
    guard args.count == 2, let input = b64urlDecode(args[args.startIndex + 1]) else {
        fail("usage: canopy-device-key sign <base64url-input>", 2)
    }
    guard let key = loadKey() else { fail("no key at \(keyFile().path) — run `create`") }
    do {
        // signature(for:) hashes with SHA-256 — exactly ES256 over the JWS signing input.
        print(b64url(try key.signature(for: input).rawRepresentation))
    } catch {
        fail("the Secure Enclave refused to sign: \(error)")
    }
case "ecdh-create":
    let (key, created) = createECDH()
    printJSON(["kind": "secure-enclave", "created": created,
               "jwk": pointJWK(key.publicKey.x963Representation)])
case "ecdh-public-jwk":
    guard let key = loadECDHKey() else { fail("no ECDH key at \(ecdhKeyFile().path) — run `ecdh-create`") }
    printJSON(pointJWK(key.publicKey.x963Representation))
case "ecdh-derive":
    guard args.count == 2, let epk = b64urlDecode(args[args.startIndex + 1]), epk.count == 65 else {
        fail("usage: canopy-device-key ecdh-derive <base64url X9.62 uncompressed P-256 point>", 2)
    }
    guard let key = loadECDHKey() else { fail("no ECDH key at \(ecdhKeyFile().path) — run `ecdh-create`") }
    do {
        // x963Representation validates the point is on P-256.
        let peer = try P256.KeyAgreement.PublicKey(x963Representation: epk)
        let secret = try key.sharedSecretFromKeyAgreement(with: peer)
        // The raw shared secret IS the 32-byte big-endian x-coordinate (Z); no KDF here.
        print(b64url(secret.withUnsafeBytes { Data($0) }))
    } catch {
        fail("ECDH failed: \(error)")
    }
default:
    fail("usage: canopy-device-key info | create | public-jwk | sign <base64url-input> | "
         + "ecdh-create | ecdh-public-jwk | ecdh-derive <base64url-point>", 2)
}
