#!/usr/bin/env python3
"""canopy-kms-broker: each runner's zero-knowledge ECDH key, for that runner only.

Spec docs/superpowers/specs/2026-10-19-runner-isolation-units-design.md §2. A machine
runs several runners, each its own Unix user. A runner's ECDH key (the key an agent's
vault key is wrapped to) lives in AWS KMS, one key per runner in the `<stack>-ecdh`
stack, so wraps survive an instance replacement. But a runner cannot call KMS: IMDS is
root-only (an iptables rule), so only root holds the instance role's credentials.

This root-owned daemon does the derivation for a runner, and only with THAT runner's key:

* a Unix socket at `/run/canopy-kms/sock`;
* the caller's uid from the kernel (`SO_PEERCRED`), never from the request, mapped to
  one KMS key by `/etc/canopy-runner/units.json` (written by `cloud_runner.py
  --setup-units`) — an unknown uid gets an error and no KMS call;
* one JSON line per connection, one JSON line back (canopy's `kms-broker` ECDH store,
  `orchestrator.device_key`):

      {"op": "public_key"}                     -> {"jwk": {"kty","crv","x","y"}}
      {"op": "derive", "peer_jwk": {...}}      -> {"secret_b64": "<Z, 32 bytes>"}
      anything else, or any failure            -> {"error": "<reason>"}

KMS is called with the JSON protocol, SigV4-signed by hand (stdlib only, like
cloud_runner.py's STS signer; pinned to botocore by runner/ec2/tests/test_kms_broker.py).
Credentials come from IMDSv2, live in memory, and are never logged. Neither Z nor a
credential is ever written anywhere.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.error
import urllib.request

SOCKET_PATH = os.environ.get("CANOPY_KMS_BROKER_SOCKET", "/run/canopy-kms/sock")
UNITS_FILE = os.environ.get("CANOPY_UNITS_FILE", "/etc/canopy-runner/units.json")
IMDS = (os.environ.get("CANOPY_AWS_IMDS_URL", "") or "http://169.254.169.254").rstrip("/")
#: One request line, at most. A JWK is ~200 bytes.
MAX_REQUEST = 16 * 1024
#: Connections served at once; more are answered "busy" and closed.
MAX_CONNECTIONS = 16
TIMEOUT = 15

#: SubjectPublicKeyInfo prefix of an uncompressed P-256 point (id-ecPublicKey, prime256v1).
P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} canopy-kms-broker: {msg}", flush=True)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def jwk_to_spki(jwk: dict) -> bytes:
    """A P-256 public JWK as the DER SubjectPublicKeyInfo KMS takes."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError("peer_jwk must be an EC P-256 public JWK")
    if "d" in jwk:
        raise ValueError("peer_jwk must be a PUBLIC key")
    x, y = b64u_decode(str(jwk.get("x", ""))), b64u_decode(str(jwk.get("y", "")))
    if len(x) != 32 or len(y) != 32:
        raise ValueError("peer_jwk coordinates must be 32 bytes")
    return P256_SPKI_PREFIX + b"\x04" + x + y


def spki_to_jwk(der: bytes) -> dict:
    """KMS GetPublicKey's DER (a P-256 SPKI ends in 04 || X || Y) as a public JWK."""
    if len(der) < 65 or der[-65] != 4:
        raise ValueError("not an uncompressed P-256 public key")
    return {"kty": "EC", "crv": "P-256", "x": b64u(der[-64:-32]), "y": b64u(der[-32:])}


# ── the instance role, and KMS ──────────────────────────────────────────────

def _imds(path: str, *, token: str = "", put: bool = False, headers: dict | None = None,
          opener=urllib.request.urlopen) -> str:
    req = urllib.request.Request(IMDS + path, method="PUT" if put else "GET", data=b"" if put else None)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if token:
        req.add_header("X-aws-ec2-metadata-token", token)
    with opener(req, timeout=2) as resp:
        return resp.read().decode()


class Credentials:
    """The instance role's temporary credentials, refreshed 5 minutes before expiry."""

    def __init__(self, opener=urllib.request.urlopen):
        self._opener = opener
        self._lock = threading.Lock()
        self._creds: dict = {}
        self.region = os.environ.get("CANOPY_KMS_REGION", "") or os.environ.get("AWS_REGION", "")

    def get(self) -> dict:
        with self._lock:
            if self._creds and self._creds["expires_at"] - 300 > time.time():
                return self._creds
            token = _imds("/latest/api/token", put=True, opener=self._opener,
                          headers={"X-aws-ec2-metadata-token-ttl-seconds": "21600"})
            if not self.region:
                self.region = _imds("/latest/meta-data/placement/region", token=token,
                                    opener=self._opener).strip()
            role = _imds("/latest/meta-data/iam/security-credentials/", token=token,
                         opener=self._opener).split()[0]
            doc = json.loads(_imds(f"/latest/meta-data/iam/security-credentials/{role}",
                                   token=token, opener=self._opener))
            try:
                exp = datetime.datetime.strptime(doc["Expiration"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except (KeyError, ValueError):
                exp = time.time() + 600
            self._creds = {"access_key": doc["AccessKeyId"], "secret_key": doc["SecretAccessKey"],
                           "token": doc.get("Token", ""), "expires_at": exp}
            return self._creds


def sigv4_headers(creds: dict, *, region: str, service: str, host: str, body: str,
                  headers: dict, amz_date: str, method: str = "POST", path: str = "/") -> dict:
    """`headers` + X-Amz-Date, X-Amz-Security-Token and Authorization (AWS SigV4) for
    a request with no query string — every given header signed."""
    out = dict(headers)
    out["X-Amz-Date"] = amz_date
    if creds.get("token"):
        out["X-Amz-Security-Token"] = creds["token"]
    canon = {k.lower().strip(): " ".join(str(v).split()) for k, v in out.items()}
    canon.setdefault("host", host)
    signed = ";".join(sorted(canon))
    request = "\n".join([method, path, "", "".join(f"{k}:{canon[k]}\n" for k in sorted(canon)),
                         signed, hashlib.sha256(body.encode()).hexdigest()])
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(request.encode()).hexdigest()])
    key = ("AWS4" + creds["secret_key"]).encode()
    for part in (amz_date[:8], region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    out["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={creds['access_key']}/{scope}, "
                            f"SignedHeaders={signed}, Signature={sig}")
    return out


class Kms:
    """The two KMS calls a runner needs, with the instance role."""

    def __init__(self, creds: Credentials, opener=urllib.request.urlopen):
        self.creds = creds
        self._opener = opener
        self._public: dict[str, dict] = {}

    def call(self, op: str, body: dict) -> dict:
        creds = self.creds.get()
        region = self.creds.region
        host = os.environ.get("CANOPY_KMS_HOST", "") or f"kms.{region}.amazonaws.com"
        data = json.dumps(body, separators=(",", ":"))
        headers = sigv4_headers(
            creds, region=region, service="kms", host=host, body=data,
            amz_date=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            headers={"Content-Type": "application/x-amz-json-1.1", "Host": host,
                     "X-Amz-Target": f"TrentService.{op}"})
        req = urllib.request.Request(f"https://{host}/", data=data.encode(), method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with self._opener(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                kind = str(json.loads(exc.read() or b"{}").get("__type") or "").rsplit("#", 1)[-1]
            except ValueError:
                kind = ""
            raise RuntimeError(f"KMS {op} answered {exc.code} {kind}".strip()) from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"KMS unreachable ({exc.reason})") from None

    def public_key(self, key_id: str) -> dict:
        if key_id not in self._public:
            out = self.call("GetPublicKey", {"KeyId": key_id})
            if out.get("KeySpec") != "ECC_NIST_P256" or out.get("KeyUsage") != "KEY_AGREEMENT":
                raise RuntimeError(f"{key_id} is not a P-256 key-agreement key")
            self._public[key_id] = spki_to_jwk(base64.b64decode(out["PublicKey"]))
        return self._public[key_id]

    def derive(self, key_id: str, peer_jwk: dict) -> bytes:
        out = self.call("DeriveSharedSecret", {
            "KeyId": key_id, "KeyAgreementAlgorithm": "ECDH",
            "PublicKey": base64.b64encode(jwk_to_spki(peer_jwk)).decode()})
        z = base64.b64decode(out.get("SharedSecret", ""))
        if len(z) != 32:
            raise RuntimeError("KMS returned no 32-byte shared secret")
        return z


# ── who is asking, and what they may have ───────────────────────────────────

def load_keys(path: str = UNITS_FILE) -> dict[int, str]:
    """{uid: KMS key id} from units.json — only runners with both."""
    try:
        doc = json.loads(open(path).read())
    except (OSError, ValueError):
        return {}
    out = {}
    for unit in doc.get("units") or []:
        uid, key = unit.get("uid"), unit.get("kms_key")
        if isinstance(uid, int) and uid > 0 and key:
            out[uid] = str(key)
    return out


def peer_uid(sock: socket.socket) -> int:
    """The connecting process's uid, from the kernel (Linux SO_PEERCRED)."""
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


def answer(line: bytes, uid: int, keys: dict[int, str], kms) -> dict:
    """The reply to ONE request line from `uid`. Never raises."""
    key = keys.get(uid)
    if key is None:
        return {"error": f"uid {uid} is not a runner on this machine"}
    try:
        req = json.loads(line.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {"error": "the request is not one JSON line"}
    if not isinstance(req, dict):
        return {"error": "the request is not a JSON object"}
    try:
        if req.get("op") == "public_key":
            return {"jwk": kms.public_key(key)}
        if req.get("op") == "derive":
            return {"secret_b64": base64.b64encode(kms.derive(key, req.get("peer_jwk"))).decode()}
    except Exception as exc:  # noqa: BLE001 — every failure is an answer, never a crash
        return {"error": str(exc)[:300]}
    return {"error": f"unknown op {req.get('op')!r}"}


class Broker(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, kms, keys_loader=load_keys, uid_of=peer_uid,
                 max_connections: int = MAX_CONNECTIONS, deadline: float | None = None):
        self.kms, self.keys_loader, self.uid_of = kms, keys_loader, uid_of
        self.deadline = CONNECTION_DEADLINE_SECONDS if deadline is None else deadline
        # Any local process may connect: a bound on threads, so a flood of idle
        # connections cannot exhaust root's broker for every runner.
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(path, _Handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b'{"error": "busy: too many connections"}\n')
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


#: The WHOLE connection — reading the request, KMS, the reply — from accept. A
#: per-read timeout would let a client drip one byte every few seconds and hold a
#: slot forever; this deadline does not reset.
CONNECTION_DEADLINE_SECONDS = 5.0


class ConnectionDeadline(Exception):
    """The client did not send one whole request line in time."""


def read_request(sock, deadline: float) -> bytes | None:
    """One request line by `deadline` (a monotonic time). None when it is too long;
    raises ConnectionDeadline when time runs out first."""
    buf = b""
    while b"\n" not in buf:
        if len(buf) > MAX_REQUEST:
            return None
        left = deadline - time.monotonic()
        if left <= 0:
            raise ConnectionDeadline()
        sock.settimeout(left)
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, TimeoutError):
            raise ConnectionDeadline() from None
        if not chunk:
            break
        buf += chunk
    line = buf.split(b"\n", 1)[0]
    return None if len(line) > MAX_REQUEST else line


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: Broker = self.server  # type: ignore[assignment]
        deadline = time.monotonic() + server.deadline
        try:
            uid = server.uid_of(self.request)
        except OSError:
            uid = -1
        try:
            line = read_request(self.request, deadline)
        except ConnectionDeadline:
            _log(f"uid {uid}: no whole request within {server.deadline:g}s — closed")
            return
        if line is None:
            reply = {"error": "request too long"}
        else:
            # units.json is re-read per request: a runner added by --setup-units is
            # served without restarting the broker.
            reply = answer(line.strip(), uid, server.keys_loader(), server.kms)
        if "error" in reply:
            _log(f"uid {uid}: {reply['error']}")
        try:
            self.request.settimeout(max(0.5, deadline - time.monotonic()))
            self.request.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass


def serve(path: str = SOCKET_PATH) -> None:
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    kms = Kms(Credentials())
    server = Broker(path, kms)
    # Any local process may CONNECT; only a runner's uid is ever answered with a key.
    os.chmod(path, 0o666)
    _log(f"listening on {path} for {len(load_keys())} runner(s)")
    server.serve_forever()


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("canopy-kms-broker runs as root (it holds the instance role)")
    serve()
