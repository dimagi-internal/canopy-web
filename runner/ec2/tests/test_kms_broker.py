"""canopy-kms-broker: a runner's ECDH key, for that runner's uid only (spec §2).

Pinned here: the uid→key mapping (an unknown uid gets an error and NO KMS call),
the one-line request framing over a real Unix socket, the JWK↔SPKI conversion KMS
needs, the hand-rolled SigV4 against botocore's, and that the file stays stdlib-only
(it runs on the box's system python3, as root).
"""
from __future__ import annotations

import ast
import base64
import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "kms_broker.py"


@pytest.fixture
def kb():
    spec = importlib.util.spec_from_file_location("kms_broker", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _p256_jwk():
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    nums = key.public_key().public_numbers()
    b64u = lambda n: base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()  # noqa: E731
    return key, {"kty": "EC", "crv": "P-256", "x": b64u(nums.x), "y": b64u(nums.y)}


class FakeKms:
    def __init__(self):
        self.calls = []

    def public_key(self, key_id):
        self.calls.append(("public_key", key_id))
        return {"kty": "EC", "crv": "P-256", "x": "x-" + key_id, "y": "y"}

    def derive(self, key_id, peer_jwk):
        self.calls.append(("derive", key_id, peer_jwk["x"]))
        return b"Z" * 32


KEYS = {2001: "alias/canopy-runner-ecdh/role", 2002: "alias/canopy-runner-ecdh/role/agent-eva"}


def test_a_runner_gets_only_its_own_key(kb):
    kms = FakeKms()
    assert kb.answer(b'{"op":"public_key"}', 2002, KEYS, kms) == {
        "jwk": {"kty": "EC", "crv": "P-256", "x": "x-alias/canopy-runner-ecdh/role/agent-eva", "y": "y"}}
    _, peer = _p256_jwk()
    out = kb.answer(json.dumps({"op": "derive", "peer_jwk": peer}).encode(), 2001, KEYS, kms)
    assert base64.b64decode(out["secret_b64"]) == b"Z" * 32
    assert kms.calls[-1][:2] == ("derive", "alias/canopy-runner-ecdh/role")


def test_an_unknown_uid_is_refused_without_a_kms_call(kb):
    kms = FakeKms()
    for uid in (0, 1000, 4242, -1):
        out = kb.answer(b'{"op":"public_key"}', uid, KEYS, kms)
        assert "error" in out and "is not a runner on this machine" in out["error"]
    assert kms.calls == []


@pytest.mark.parametrize("line,why", [
    (b"not json", "not one JSON line"),
    (b"[1]", "not a JSON object"),
    (b'{"op":"sign"}', "unknown op"),
])
def test_bad_requests_get_an_error_line(kb, line, why):
    assert why in kb.answer(line, 2001, KEYS, FakeKms())["error"]


def test_a_failure_is_an_answer_never_a_crash(kb):
    class Broken(FakeKms):
        def derive(self, key_id, peer_jwk):
            raise RuntimeError("KMS DeriveSharedSecret answered 400 AccessDeniedException")

    out = kb.answer(b'{"op":"derive","peer_jwk":{"kty":"EC"}}', 2001, KEYS, Broken())
    assert "AccessDenied" in out["error"]


def test_runners_json_maps_uids_to_keys(kb, tmp_path):
    doc = {"box": "cloud-ec2-9", "runners": [
        {"id": "dimagi", "uid": 2001, "kms_key": KEYS[2001]},
        {"id": "agent-eva", "uid": 2002, "kms_key": KEYS[2002]},
        {"id": "broken", "uid": 0, "kms_key": "alias/x"},       # root is never a runner
        {"id": "nokey", "uid": 2003, "kms_key": ""},
    ]}
    (tmp_path / "runners.json").write_text(json.dumps(doc))
    assert kb.load_keys(str(tmp_path / "runners.json")) == KEYS
    assert kb.load_keys(str(tmp_path / "missing.json")) == {}


def test_a_box_not_yet_moved_is_read_from_units_json(kb, tmp_path, monkeypatch):
    """Until update_runner.sh moves it, the list is units.json, under `units` (remove in the follow-up)."""
    doc = {"box": "cloud-ec2-9", "units": [{"id": "dimagi", "uid": 2001, "kms_key": KEYS[2001]},
                                           {"id": "agent-eva", "uid": 2002, "kms_key": KEYS[2002]}]}
    (tmp_path / "units.json").write_text(json.dumps(doc))
    monkeypatch.setattr(kb, "RUNNERS_FILE", str(tmp_path / "runners.json"))
    monkeypatch.setattr(kb, "OLD_RUNNERS_FILE", str(tmp_path / "units.json"))
    assert kb.load_keys() == KEYS
    # Once it is moved, the new file is read.
    (tmp_path / "runners.json").write_text(json.dumps({"runners": doc["units"][:1]}))
    assert kb.load_keys() == {2001: KEYS[2001]}


def test_jwk_spki_round_trip_is_what_kms_and_cryptography_agree_on(kb):
    from cryptography.hazmat.primitives import serialization

    key, jwk = _p256_jwk()
    der = kb.jwk_to_spki(jwk)
    assert der == key.public_key().public_bytes(serialization.Encoding.DER,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
    assert kb.spki_to_jwk(der) == jwk
    with pytest.raises(ValueError):
        kb.jwk_to_spki({**jwk, "d": "c2VjcmV0"})
    with pytest.raises(ValueError):
        kb.jwk_to_spki({"kty": "OKP", "crv": "Ed25519", "x": "a"})


def test_sigv4_matches_botocore_for_a_kms_call(kb):
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    creds = {"access_key": "AKIAFAKE", "secret_key": "fake-secret", "token": "tok"}
    body = json.dumps({"KeyId": "alias/x"}, separators=(",", ":"))
    host = "kms.us-east-1.amazonaws.com"
    base = {"Content-Type": "application/x-amz-json-1.1", "Host": host,
            "X-Amz-Target": "TrentService.GetPublicKey"}
    req = AWSRequest(method="POST", url=f"https://{host}/", data=body, headers=dict(base))
    SigV4Auth(Credentials("AKIAFAKE", "fake-secret", "tok"), "kms", "us-east-1").add_auth(req)
    # botocore signs at its own "now"; sign ours at that same second and compare.
    ours = kb.sigv4_headers(creds, region="us-east-1", service="kms", host=host, body=body,
                            headers=base, amz_date=req.headers["X-Amz-Date"])
    assert ours["X-Amz-Security-Token"] == req.headers["X-Amz-Security-Token"]
    assert ours["Authorization"] == req.headers["Authorization"]


def test_one_line_in_one_line_out_over_a_real_socket(kb):
    """The canopy CLI's kms-broker store speaks this framing (orchestrator.device_key)."""
    d = tempfile.mkdtemp(prefix="kb", dir="/tmp")
    path = os.path.join(d, "sock")
    kms = FakeKms()
    server = kb.Broker(path, kms, keys_loader=lambda: KEYS, uid_of=lambda sock: 2002)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with socket.socket(socket.AF_UNIX) as c:
            c.connect(path)
            c.sendall(b'{"op": "public_key"}\n')
            reply = c.makefile().readline()
        assert json.loads(reply)["jwk"]["x"] == "x-alias/canopy-runner-ecdh/role/agent-eva"
        with socket.socket(socket.AF_UNIX) as c:
            c.connect(path)
            c.sendall(b"x" * (kb.MAX_REQUEST + 10) + b"\n")
            assert "too long" in json.loads(c.makefile().readline())["error"]
    finally:
        server.shutdown()
        server.server_close()
        os.unlink(path)
        os.rmdir(d)


def test_concurrent_connections_are_bounded(kb):
    """Any local process may connect: idle connections past the bound are told
    `busy` and closed, never given a thread each."""
    d = tempfile.mkdtemp(prefix="kb", dir="/tmp")
    path = os.path.join(d, "sock")
    server = kb.Broker(path, FakeKms(), keys_loader=lambda: KEYS, uid_of=lambda sock: 2001, max_connections=1)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    idle = socket.socket(socket.AF_UNIX)
    try:
        idle.connect(path)             # holds the one slot, sends nothing
        import time
        time.sleep(0.2)
        with socket.socket(socket.AF_UNIX) as c:
            c.connect(path)
            # The broker answers `busy` and closes without reading, so on Linux
            # the write can race the close (EPIPE); the reply is already ours.
            try:
                c.sendall(b'{"op": "public_key"}\n')
            except BrokenPipeError:
                pass
            assert "busy" in json.loads(c.makefile().readline())["error"]
        idle.close()                   # the slot comes back
        time.sleep(0.2)
        with socket.socket(socket.AF_UNIX) as c:
            c.connect(path)
            c.sendall(b'{"op": "public_key"}\n')
            assert "jwk" in json.loads(c.makefile().readline())
    finally:
        idle.close()
        server.shutdown()
        server.server_close()
        os.unlink(path)
        os.rmdir(d)


def test_a_slow_drip_client_is_cut_at_the_connection_deadline(kb):
    """One byte at a time, each well inside any per-read timeout, never finishes a
    request: the deadline counts from accept and does not reset, so the slot frees."""
    import time

    d = tempfile.mkdtemp(prefix="kb", dir="/tmp")
    path = os.path.join(d, "sock")
    server = kb.Broker(path, FakeKms(), keys_loader=lambda: KEYS, uid_of=lambda sock: 2001,
                       max_connections=1, deadline=0.6)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        drip = socket.socket(socket.AF_UNIX)
        drip.connect(path)
        start = time.monotonic()
        closed = False
        for ch in b'{"op": "public_key"':      # never the newline
            try:
                drip.sendall(bytes([ch]))
            except OSError:
                closed = True
                break
            time.sleep(0.1)
            if time.monotonic() - start > 1.5:
                break
        drip.settimeout(1)
        try:
            closed = closed or drip.recv(100) == b""
        except OSError:
            closed = True
        drip.close()
        assert closed, "the drip connection was held past the deadline"
        time.sleep(0.1)
        with socket.socket(socket.AF_UNIX) as c:     # the slot came back
            c.connect(path)
            c.sendall(b'{"op": "public_key"}\n')
            assert "jwk" in json.loads(c.makefile().readline())
    finally:
        server.shutdown()
        server.server_close()
        os.unlink(path)
        os.rmdir(d)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SO_PEERCRED is Linux")
def test_peer_uid_is_the_kernels_word(kb):
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert kb.peer_uid(a) == os.getuid()
    finally:
        a.close()
        b.close()


def test_stays_stdlib_only(kb):
    tree = ast.parse(_PATH.read_text())
    names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
             for alias in node.names}
    names |= {(node.module or "").split(".")[0] for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom) and not node.level}
    names.discard("__future__")
    assert names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names)
    res = subprocess.run([sys.executable, "-I", "-S", "-c",
                          "import runpy, sys; runpy.run_path(sys.argv[1], run_name='check')", str(_PATH)],
                         capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
