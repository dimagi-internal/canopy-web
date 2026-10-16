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
    // privateKeyUsage only: no .userPresence / .biometryAny — an unattended turn must
    // never be asked for Touch ID. AfterFirstUnlock so a locked screen does not stop it.
    var error: Unmanaged<CFError>?
    guard let access = SecAccessControlCreateWithFlags(
        kCFAllocatorDefault, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        .privateKeyUsage, &error) else {
        fail("could not build the key's access control: \(String(describing: error?.takeRetainedValue()))")
    }
    let key: SecureEnclave.P256.Signing.PrivateKey
    do {
        key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
    } catch {
        fail("the Secure Enclave refused to make a key: \(error)")
    }
    let fm = FileManager.default
    do {
        try fm.createDirectory(at: keyDir(), withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: keyDir().path)
        let tmp = keyDir().appendingPathComponent(".se.key.\(getpid()).tmp")
        guard fm.createFile(atPath: tmp.path, contents: key.dataRepresentation,
                            attributes: [.posixPermissions: 0o600]) else {
            fail("could not write \(tmp.path)")
        }
        if rename(tmp.path, keyFile().path) != 0 {
            try? fm.removeItem(at: tmp)
            fail("could not move the key into \(keyFile().path)")
        }
    } catch {
        fail("could not store the key: \(error)")
    }
    return (key, true)
}

let args = CommandLine.arguments.dropFirst()
switch args.first {
case "info":
    printJSON(["secure_enclave_available": SecureEnclave.isAvailable,
               "key_present": FileManager.default.fileExists(atPath: keyFile().path),
               "path": keyFile().path, "kind": "secure-enclave"])
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
default:
    fail("usage: canopy-device-key info | create | public-jwk | sign <base64url-input>", 2)
}
