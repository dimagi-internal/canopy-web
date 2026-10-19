"""The runner's own device key — canopy's key files, canopy's formats, a proof canopy-web
accepts. A box installed from the download has no canopy CLI, so the runner makes and uses
the key itself; if it disagreed with canopy about where or what the key is, a later
`canopy vault …` on the same box would make a second one."""
from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess

import pytest

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import (  # noqa: E402
    Prehashed, decode_dss_signature, encode_dss_signature)

from canopy_runner import device_proof, device_proof_tpm  # noqa: E402


def verify_dpop(jws: str, *, method: str, url: str) -> dict:
    """Check a DPoP proof the way canopy-web does: the signature by the header's JWK,
    htm/htu. Returns the header (for its jwk)."""
    h64, c64, s64 = jws.split(".")
    header = json.loads(device_proof.b64url_decode(h64))
    claims = json.loads(device_proof.b64url_decode(c64))
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    assert claims["htm"] == method and claims["htu"] == url and claims["jti"]
    jwk = header["jwk"]
    pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(device_proof.b64url_decode(jwk["x"]), "big"),
        int.from_bytes(device_proof.b64url_decode(jwk["y"]), "big"), ec.SECP256R1()).public_key()
    sig = device_proof.b64url_decode(s64)
    assert len(sig) == 64
    der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
    pub.verify(der, f"{h64}.{c64}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
    return header


def _jwk_of(key: ec.EllipticCurvePrivateKey) -> dict:
    n = key.public_key().public_numbers()
    return device_proof.public_jwk_from_point(n.x.to_bytes(32, "big"), n.y.to_bytes(32, "big"))


# --- encoding -------------------------------------------------------------------


def test_thumbprint_matches_the_rfc_9449_example():
    jwk = {"kty": "EC", "x": "l8tFrhx-34tV3hRICRDY9zCkDlpBhF42UQUfWVAWBFs",
           "y": "9VE4jf_Ok_o64zbTTlcuNJajHmt6v9TDVrU0CdvGRDA", "crv": "P-256"}
    assert device_proof.jwk_thumbprint(jwk) == "0ZcOCORZNYy-DWpqq30jZyJGHTN0d2HglBV3uiguA4I"


def test_htu_drops_query_and_fragment():
    assert device_proof.normalize_htu("https://c.test/api/x/?a=1#f") == "https://c.test/api/x/"


# --- software ---------------------------------------------------------------------


def test_software_key_round_trip_and_its_proof_verifies():
    key, created = device_proof.ensure(allow_software=True)
    assert created and key.kind == "software"
    pem = device_proof.sw_path()
    assert pem.is_file() and oct(pem.stat().st_mode & 0o777) == "0o600"
    again, created2 = device_proof.ensure(allow_software=True)
    assert not created2 and again.public_jwk == key.public_jwk
    jws = device_proof.dpop_proof(again, "post", "https://c.test/api/runner-sessions/?x=1")
    header = verify_dpop(jws, method="POST", url="https://c.test/api/runner-sessions/")
    assert device_proof.jwk_thumbprint(header["jwk"]) == key.jkt


def test_software_ecdh_key_derives_the_same_secret_as_its_peer():
    ecdh, created = device_proof.ensure_ecdh("software")
    assert created and device_proof.ecdh_pem_path().is_file()
    peer = ec.generate_private_key(ec.SECP256R1())
    point = peer.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    mine = ecdh.public_jwk
    mine_pub = ec.EllipticCurvePublicNumbers(
        int.from_bytes(device_proof.b64url_decode(mine["x"]), "big"),
        int.from_bytes(device_proof.b64url_decode(mine["y"]), "big"), ec.SECP256R1()).public_key()
    assert ecdh.derive(point) == peer.exchange(ec.ECDH(), mine_pub)
    assert device_proof.ensure_ecdh("software")[1] is False


def test_a_mac_without_the_helper_refuses_unless_software_is_allowed():
    with pytest.raises(device_proof.DeviceKeyError, match="no canopy-device-key helper"):
        device_proof.ensure(system=lambda: "Darwin", which=lambda _n: None)
    assert not device_proof.sw_path().exists()
    key, _ = device_proof.ensure(system=lambda: "Darwin", which=lambda _n: None,
                                 allow_software=True)
    assert key.kind == "software"


def test_windows_without_a_tpm_gets_a_software_key():
    key, _ = device_proof.ensure(system=lambda: "Windows", tpm_available=lambda: False)
    assert key.kind == "software"


def test_a_cloud_marker_is_never_shadowed_by_a_second_key():
    device_proof._ensure_dir()
    (device_proof.key_dir() / "aws-iam.json").write_text('{"role_arn": "arn:x"}')
    with pytest.raises(device_proof.DeviceKeyError, match="cloud tooling"):
        device_proof.ensure(allow_software=True)


# --- proof_headers (canopy's rule, plus the runner) ---------------------------------


def _registered(runner="r1", status="active"):
    key, _ = device_proof.ensure(allow_software=True)
    device_proof.write_status(device_proof.registered_status(
        key, key_id=7, runner_id=runner, status=status))
    return key


def test_status_json_uses_canopys_field_names():
    key = _registered()
    st = json.loads(device_proof.status_path().read_text())
    assert set(st) >= {"id", "jkt", "status", "approve_url", "kind", "runner", "checked_at"}
    assert st["jkt"] == key.jkt and st["runner"] == "r1" and st["status"] == "active"


def test_proof_headers_only_for_a_registered_key_of_this_runner():
    url = "https://c.test/api/runner-sessions/"
    assert device_proof.proof_headers("POST", url) == {}  # no key, no status
    _registered()
    hdr = device_proof.proof_headers("POST", url, runner_id="r1")
    verify_dpop(hdr["DPoP"], method="POST", url=url)
    assert device_proof.proof_headers("POST", url, runner_id="other") == {}
    st = device_proof.read_status()
    device_proof.write_status({**st, "jkt": "someone-elses"})
    assert device_proof.proof_headers("POST", url, runner_id="r1") == {}
    device_proof.write_status({**st, "status": "revoked"})
    assert device_proof.proof_headers("POST", url, runner_id="r1") == {}


# --- Secure Enclave, through a fake helper ---------------------------------------


class FakeHelper:
    """`canopy-device-key` with an in-memory key per purpose; records its argv."""

    def __init__(self, *, enclave=True, ecdh=True):
        self.enclave, self.ecdh = enclave, ecdh
        self.sign_key = self.ecdh_key = None
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv[1:])
        assert kw["env"][device_proof.DIR_ENV] == str(device_proof.key_dir())
        cmd = argv[1]
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        if cmd == "info":
            return ok(json.dumps({"secure_enclave_available": self.enclave}))
        if cmd == "create":
            self.sign_key = ec.generate_private_key(ec.SECP256R1())
            device_proof.se_path().write_bytes(b"opaque-enclave-blob")
            return ok()
        if cmd == "public-jwk":
            return ok(json.dumps({**_jwk_of(self.sign_key), "kid": "x"}))
        if cmd == "sign":
            data = device_proof.b64url_decode(argv[2])
            r, s = decode_dss_signature(self.sign_key.sign(data, ec.ECDSA(hashes.SHA256())))
            return ok(device_proof.b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big")))
        if cmd.startswith("ecdh") and not self.ecdh:
            return subprocess.CompletedProcess(argv, 2, "", "unknown command")
        if cmd == "ecdh-create":
            self.ecdh_key = ec.generate_private_key(ec.SECP256R1())
            device_proof.se_ecdh_path().write_bytes(b"opaque-ecdh-blob")
            return ok()
        if cmd == "ecdh-public-jwk":
            return ok(json.dumps(_jwk_of(self.ecdh_key)))
        raise AssertionError(argv)


@pytest.fixture
def helper_bin(tmp_path, monkeypatch):
    path = tmp_path / "bin" / "canopy-device-key"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 99\n")
    path.chmod(0o755)
    monkeypatch.setenv(device_proof.HELPER_ENV, str(path))
    return path


def test_secure_enclave_key_through_the_helper(helper_bin):
    fake = FakeHelper()
    key, created = device_proof.ensure(system=lambda: "Darwin", runner=fake)
    assert created and key.kind == "secure-enclave"
    assert ["create"] in fake.calls and not device_proof.sw_path().exists()
    url = "https://c.test/api/harness/runner-pairings/"
    verify_dpop(device_proof.dpop_proof(key, "POST", url), method="POST", url=url)
    # Loaded again from se.key, with no create.
    fake.calls.clear()
    again = device_proof.load(runner=fake)
    assert again.public_jwk == key.public_jwk and ["create"] not in fake.calls
    ecdh, made = device_proof.ensure_ecdh("secure-enclave", runner=fake)
    assert made and ecdh.kind == "secure-enclave"


def test_an_old_helper_without_ecdh_is_unsupported_not_fatal(helper_bin):
    fake = FakeHelper(ecdh=False)
    device_proof.ensure(system=lambda: "Darwin", runner=fake)
    with pytest.raises(device_proof.EcdhUnsupported):
        device_proof.ensure_ecdh("secure-enclave", runner=fake)


# --- TPM, through a fake ncrypt ---------------------------------------------------


class FakeNcrypt:
    """Just enough of ncrypt.dll for device_proof_tpm, backed by `cryptography` keys."""

    def __init__(self):
        self.keys: dict[str, tuple[str, ec.EllipticCurvePrivateKey]] = {}
        self.handles: dict[int, str] = {}
        self._next = 100

    def _h(self, name):
        self._next += 1
        self.handles[self._next] = name
        return self._next

    def NCryptOpenStorageProvider(self, pprov, name, flags):  # noqa: N802
        assert name == device_proof_tpm.PROVIDER
        pprov._obj.value = 1
        return 0

    def NCryptCreatePersistedKey(self, prov, pkey, alg, name, ls, flags):  # noqa: N802
        if name in self.keys:
            return device_proof_tpm.NTE_EXISTS
        self.keys[name] = (alg, ec.generate_private_key(ec.SECP256R1()))
        pkey._obj.value = self._h(name)
        return 0

    def NCryptSetProperty(self, *a):  # noqa: N802
        return 0

    def NCryptFinalizeKey(self, key, flags):  # noqa: N802
        return 0

    def NCryptOpenKey(self, prov, pkey, name, ls, flags):  # noqa: N802
        if name not in self.keys:
            return device_proof_tpm.NTE_BAD_KEYSET
        pkey._obj.value = self._h(name)
        return 0

    def NCryptExportKey(self, key, _e, blob_type, _p, buf, size, psize, flags):  # noqa: N802
        alg, k = self.keys[self.handles[int(key.value if hasattr(key, "value") else key)]]
        magic = (device_proof_tpm.P256_PUBLIC_MAGIC if alg == "ECDSA_P256"
                 else device_proof_tpm.P256_ECDH_PUBLIC_MAGIC)
        n = k.public_key().public_numbers()
        blob = struct.pack("<II", magic, 32) + n.x.to_bytes(32, "big") + n.y.to_bytes(32, "big")
        if buf is not None:
            ctypes.memmove(buf, blob, len(blob))
        psize._obj.value = len(blob)
        return 0

    def NCryptSignHash(self, key, _p, hbuf, n, out, outlen, pn, flags):  # noqa: N802
        _, k = self.keys[self.handles[int(key.value if hasattr(key, "value") else key)]]
        digest = hbuf.raw[:n]
        r, s = decode_dss_signature(k.sign(digest, ec.ECDSA(Prehashed(hashes.SHA256()))))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        ctypes.memmove(out, sig, 64)
        pn._obj.value = 64
        return 0

    def NCryptFreeObject(self, h):  # noqa: N802
        return 0


def test_tpm_key_made_marked_and_proving():
    lib = FakeNcrypt()
    key, created = device_proof.ensure(system=lambda: "Windows", tpm_available=lambda: True,
                                       tpm_lib=lib)
    assert created and key.kind == "tpm"
    marker = json.loads(device_proof.tpm_path().read_text())
    assert marker == {"provider": "Microsoft Platform Crypto Provider",
                      "name": "canopy-device-key"}
    url = "https://c.test/api/runner-sessions/"
    verify_dpop(device_proof.dpop_proof(key, "POST", url), method="POST", url=url)
    again = device_proof.load(tpm_lib=lib)
    assert again.kind == "tpm" and again.public_jwk == key.public_jwk
    ecdh, made = device_proof.ensure_ecdh("tpm", tpm_lib=lib)
    assert made and ecdh.kind == "tpm"
    assert json.loads(device_proof.tpm_ecdh_path().read_text())["name"] == "canopy-device-ecdh"


def test_tpm_is_unavailable_off_windows():
    if os.name != "nt":
        assert device_proof_tpm.available() is False
