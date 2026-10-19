"""This machine's device key, made and used by the runner itself when there is no canopy CLI.

A developer's box signs in through canopy's `canopy vault runner-session` (see
`runner_token`), and canopy owns the key. A box installed from the downloadable
installer has no canopy CLI — no git, no `uv`, nothing to install it with — so the
runner must make the key, register it (by pairing, `browser_pair`) and prove it holds it
on every token exchange on its own.

It does that EXACTLY where and how canopy's ``orchestrator/device_key.py`` does, because
the two must never disagree about which key this machine holds: a person who later
installs canopy and runs ``canopy vault …`` on the same box has to find the key the
runner paired with, and the runner has to find a key canopy made. So the paths, file
formats, helper calls, marker JSON and the ``status.json`` field names below are canopy's,
copied rather than reinvented:

* ``~/.canopy/device-key/`` (``$CANOPY_DEVICE_KEY_DIR`` overrides it);
* **secure-enclave** — ``se.key``, made and used by the Swift helper ``canopy-device-key``
  (``$CANOPY_DEVICE_KEY_BIN``, else ``~/.canopy/bin/canopy-device-key``, which `setup`
  links to the copy inside the app bundle). Its ECDH key is ``se-ecdh.key``;
* **tpm** — the CNG keys ``canopy-device-key`` / ``canopy-device-ecdh`` in the Microsoft
  Platform Crypto Provider (`device_proof_tpm`), with markers ``tpm.json`` /
  ``tpm-ecdh.json``;
* **software** — 0600 PEMs ``sw.pem`` / ``ecdh.pem``. These need ``cryptography``, which
  the runner does not otherwise depend on: it is the ``keys`` extra, which the bundle
  installs, imported only when a software key is actually made or used.

A Mac without the helper REFUSES rather than quietly making a software key (as canopy
does): a software key made there would sit beside the enclave key the installer meant to
make, and the first one to register would win. ``allow_software=True`` overrides that.
Windows with no TPM gets a software key.

The cloud-only kinds (aws-iam, a KMS or broker ECDH key) are not here: a laptop never has
them, and a marker for one means this box belongs to canopy's cloud tooling — the runner
refuses to make a second key beside it.

Nothing here ever logs or prints a private key or a signature.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

KIND_SE = "secure-enclave"
KIND_SOFTWARE = "software"
KIND_TPM = "tpm"
KINDS = (KIND_SE, KIND_TPM, KIND_SOFTWARE)

#: The helper's location override, as canopy reads it.
HELPER_ENV = "CANOPY_DEVICE_KEY_BIN"
#: The key directory override, as canopy reads it.
DIR_ENV = "CANOPY_DEVICE_KEY_DIR"
#: Statuses for which a proof is attached (canopy's `PROOF_STATUSES`): `pending` too,
#: because the approval happens in a browser and this cache lags it.
PROOF_STATUSES = ("active", "pending")

Runner = Callable[..., subprocess.CompletedProcess]


class DeviceKeyError(Exception):
    """No usable device key; the message says what to do."""


class EcdhUnsupported(DeviceKeyError):
    """This machine's backend cannot make an ECDH key (e.g. an old helper); the key is
    registered without one."""


# --- paths (canopy's) ----------------------------------------------------------


def key_dir() -> Path:
    override = os.environ.get(DIR_ENV, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".canopy" / "device-key"


def se_path() -> Path:
    return key_dir() / "se.key"


def sw_path() -> Path:
    return key_dir() / "sw.pem"


def status_path() -> Path:
    return key_dir() / "status.json"


def tpm_path() -> Path:
    return key_dir() / "tpm.json"


def se_ecdh_path() -> Path:
    return key_dir() / "se-ecdh.key"


def ecdh_pem_path() -> Path:
    return key_dir() / "ecdh.pem"


def tpm_ecdh_path() -> Path:
    return key_dir() / "tpm-ecdh.json"


#: canopy's cloud-only markers. Present = not a key this runner may make or use.
_CLOUD_MARKERS = ("aws-iam.json",)
_CLOUD_ECDH_MARKERS = ("kms-ecdh.json",)


def helper_path(which: Callable[[str], Optional[str]] = shutil.which) -> Optional[str]:
    """The Secure Enclave helper: $CANOPY_DEVICE_KEY_BIN, ~/.canopy/bin/canopy-device-key
    (canopy's places), then the copy inside this runner's bundle, then PATH.

    The bundle copy is only a fallback for the moment before `setup` has linked
    ~/.canopy/bin/canopy-device-key: pairing runs first on a fresh install."""
    override = os.environ.get(HELPER_ENV, "").strip()
    if override:
        return override if os.access(override, os.X_OK) else None
    installed = Path.home() / ".canopy" / "bin" / "canopy-device-key"
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    try:
        from . import bundle

        root = bundle.root()
    except Exception:  # noqa: BLE001 — no bundle is the developer case
        root = None
    if root is not None:
        cand = root / "bin" / "canopy-device-key"
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return which("canopy-device-key")


def _ensure_dir() -> Path:
    d = key_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _write_private(path: Path, data: bytes) -> None:
    """Write 0600, atomically. `os.fchmod` is POSIX-only before Python 3.13, so it is
    skipped where it does not exist (Windows, where the profile directory's ACL is
    what keeps the file the user's)."""
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        if hasattr(os, "fchmod") and os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- encoding ------------------------------------------------------------------


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _compact_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=False).encode("utf-8")


def jwk_thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint of an EC public JWK — the `jkt` canopy-web keys device keys by."""
    required = {k: jwk[k] for k in ("crv", "kty", "x", "y")}
    canonical = json.dumps(required, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return b64url(hashlib.sha256(canonical).digest())


def public_jwk_from_point(x: bytes, y: bytes) -> dict:
    return {"kty": "EC", "crv": "P-256", "x": b64url(x), "y": b64url(y)}


# --- the signing key -----------------------------------------------------------


@dataclass
class DeviceKey:
    kind: str
    public_jwk: dict
    _sign: Callable[[bytes], bytes]

    @property
    def jkt(self) -> str:
        return jwk_thumbprint(self.public_jwk)

    def sign(self, data: bytes) -> bytes:
        """Raw ES256 signature (r‖s, 64 bytes) over `data`."""
        sig = self._sign(data)
        if len(sig) != 64:
            raise DeviceKeyError(f"{self.kind} key returned a {len(sig)}-byte signature, not 64")
        return sig


def _run_helper(helper: str, *args: str, runner: Runner = subprocess.run
                ) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env[DIR_ENV] = str(key_dir())
    return runner([helper, *args], capture_output=True, text=True, timeout=30, env=env)


def _helper_fail(what: str, p: subprocess.CompletedProcess) -> DeviceKeyError:
    err = ((p.stderr or "").strip().splitlines() or [f"exit {p.returncode}"])[-1][:300]
    return DeviceKeyError(f"canopy-device-key {what} failed: {err}")


def helper_info(helper: str, *, runner: Runner = subprocess.run) -> dict:
    p = _run_helper(helper, "info", runner=runner)
    if p.returncode != 0:
        raise _helper_fail("info", p)
    try:
        return json.loads(p.stdout or "{}")
    except ValueError as e:
        raise DeviceKeyError(f"canopy-device-key info printed no JSON ({e})")


def _se_key(helper: str, runner: Runner) -> DeviceKey:
    p = _run_helper(helper, "public-jwk", runner=runner)
    if p.returncode != 0:
        raise _helper_fail("public-jwk", p)
    try:
        jwk = json.loads(p.stdout)
    except ValueError as e:
        raise DeviceKeyError(f"canopy-device-key public-jwk printed no JSON ({e})")

    def sign(data: bytes) -> bytes:
        s = _run_helper(helper, "sign", b64url(data), runner=runner)
        if s.returncode != 0:
            raise _helper_fail("sign", s)
        return b64url_decode((s.stdout or "").strip())

    return DeviceKey(KIND_SE, {k: jwk[k] for k in ("kty", "crv", "x", "y")}, sign)


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    except ImportError:
        raise DeviceKeyError("a software device key needs the `cryptography` package — "
                             "install canopy-runner with its `keys` extra") from None
    return hashes, serialization, ec, decode_dss_signature


def _sw_key(pem: bytes) -> DeviceKey:
    hashes, serialization, ec, decode_dss_signature = _crypto()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise DeviceKeyError(f"{sw_path()} is not a P-256 private key")
    nums = key.public_key().public_numbers()
    jwk = public_jwk_from_point(nums.x.to_bytes(32, "big"), nums.y.to_bytes(32, "big"))

    def sign(data: bytes) -> bytes:
        r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return DeviceKey(KIND_SOFTWARE, jwk, sign)


def _new_p256_pem() -> bytes:
    _, serialization, ec, _ = _crypto()
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _create_software() -> DeviceKey:
    pem = _new_p256_pem()
    _write_private(sw_path(), pem)
    return _sw_key(pem)


def _read_marker(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _tpm_key(marker: dict, lib=None) -> DeviceKey:
    from . import device_proof_tpm as tpm
    key = tpm.open_key(str(marker.get("name") or tpm.KEY_NAME), lib)
    return DeviceKey(KIND_TPM, key.public_jwk(), key.sign)


def _create_tpm(lib=None) -> DeviceKey:
    from . import device_proof_tpm as tpm
    key = tpm.create_key(tpm.KEY_NAME, lib)
    _write_private(tpm_path(), json.dumps({"provider": tpm.PROVIDER, "name": tpm.KEY_NAME},
                                          indent=1).encode("utf-8"))
    return DeviceKey(KIND_TPM, key.public_jwk(), key.sign)


def _tpm_available(lib=None) -> bool:
    try:
        from . import device_proof_tpm as tpm
        return tpm.available(lib)
    except Exception:  # noqa: BLE001 — no TPM is the common answer, not an error
        return False


def _refuse_cloud_markers(names) -> None:
    for name in names:
        if (key_dir() / name).is_file():
            raise DeviceKeyError(f"{key_dir() / name} belongs to canopy's cloud tooling — this "
                                 "runner will not make or use a second key beside it")


def load(*, runner: Runner = subprocess.run,
         which: Callable[[str], Optional[str]] = shutil.which, tpm_lib=None
         ) -> Optional[DeviceKey]:
    """The existing device key, or None when this machine has none. Never creates.
    Same precedence as canopy: tpm, then secure-enclave, then software."""
    _refuse_cloud_markers(_CLOUD_MARKERS)
    if tpm_path().is_file():
        try:
            return _tpm_key(_read_marker(tpm_path()), tpm_lib)
        except Exception as e:  # noqa: BLE001 — name it as a device-key problem
            raise DeviceKeyError(f"the TPM key named in {tpm_path()} could not be opened ({e})")
    if se_path().is_file():
        helper = helper_path(which)
        if not helper:
            raise DeviceKeyError(
                f"{se_path()} is a Secure Enclave key but canopy-device-key is not installed "
                "— re-run the Canopy Runner installer")
        return _se_key(helper, runner)
    if sw_path().is_file():
        return _sw_key(sw_path().read_bytes())
    return None


def ensure(*, allow_software: Optional[bool] = None, runner: Runner = subprocess.run,
           which: Callable[[str], Optional[str]] = shutil.which,
           system: Callable[[], str] = platform.system,
           tpm_available: Callable[[], bool] = _tpm_available, tpm_lib=None
           ) -> tuple[DeviceKey, bool]:
    """(the device key, whether it was made now). Idempotent; an existing key always wins.

    A Mac uses the Secure Enclave and REFUSES without the helper unless
    `allow_software=True`; Windows uses the TPM, else software; anything else software.
    `allow_software=False` forbids a software key anywhere."""
    existing = load(runner=runner, which=which, tpm_lib=tpm_lib)
    if existing is not None:
        return existing, False
    os_name = system()
    if allow_software is True:
        return _create_software(), True
    if os_name == "Windows":
        if tpm_available():
            return _create_tpm(tpm_lib), True
    elif os_name == "Darwin":
        helper = helper_path(which)
        if helper:
            info = helper_info(helper, runner=runner)
            if info.get("secure_enclave_available"):
                _ensure_dir()
                p = _run_helper(helper, "create", runner=runner)
                if p.returncode != 0:
                    raise _helper_fail("create", p)
                return _se_key(helper, runner), True
        else:
            raise DeviceKeyError(
                "this Mac has no canopy-device-key helper to make a Secure Enclave key — "
                "reinstall Canopy Runner (it carries the helper), or pass --allow-software "
                "to register a software key instead")
    if allow_software is False:
        raise DeviceKeyError("no hardware-bound key could be made here and software keys "
                             "were not allowed")
    return _create_software(), True


# --- the ECDH key (zero-knowledge vault) ---------------------------------------


@dataclass
class EcdhKey:
    kind: str
    public_jwk: dict
    _derive: Callable[[bytes], bytes]

    @property
    def jkt(self) -> str:
        return jwk_thumbprint(self.public_jwk)

    def derive(self, peer_point: bytes) -> bytes:
        z = self._derive(peer_point)
        if len(z) != 32:
            raise DeviceKeyError(f"{self.kind} ECDH key returned a {len(z)}-byte secret, not 32")
        return z


def _sw_ecdh_key(pem: bytes) -> EcdhKey:
    _, serialization, ec, _ = _crypto()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
        raise DeviceKeyError(f"{ecdh_pem_path()} is not a P-256 private key")
    nums = key.public_key().public_numbers()
    jwk = public_jwk_from_point(nums.x.to_bytes(32, "big"), nums.y.to_bytes(32, "big"))

    def derive(peer_point: bytes) -> bytes:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_point)
        return key.exchange(ec.ECDH(), peer)

    return EcdhKey(KIND_SOFTWARE, jwk, derive)


def _se_ecdh_key(helper: str, runner: Runner) -> EcdhKey:
    p = _run_helper(helper, "ecdh-public-jwk", runner=runner)
    if p.returncode == 2:
        raise EcdhUnsupported("canopy-device-key predates ECDH — reinstall Canopy Runner")
    if p.returncode != 0:
        raise _helper_fail("ecdh-public-jwk", p)
    try:
        jwk = json.loads(p.stdout)
    except ValueError as e:
        raise DeviceKeyError(f"canopy-device-key ecdh-public-jwk printed no JSON ({e})")

    def derive(peer_point: bytes) -> bytes:
        d = _run_helper(helper, "ecdh-derive", b64url(peer_point), runner=runner)
        if d.returncode != 0:
            raise _helper_fail("ecdh-derive", d)
        return b64url_decode((d.stdout or "").strip())

    return EcdhKey(KIND_SE, {k: jwk[k] for k in ("kty", "crv", "x", "y")}, derive)


def _tpm_ecdh_key(marker: dict, lib=None) -> EcdhKey:
    from . import device_proof_tpm as tpm
    key = tpm.open_ecdh_key(str(marker.get("name") or tpm.ECDH_KEY_NAME), lib)
    return EcdhKey(KIND_TPM, key.public_jwk(), key.derive)


def load_ecdh(*, runner: Runner = subprocess.run,
              which: Callable[[str], Optional[str]] = shutil.which, tpm_lib=None
              ) -> Optional[EcdhKey]:
    """This machine's ECDH key, or None. Never creates."""
    _refuse_cloud_markers(_CLOUD_ECDH_MARKERS)
    if tpm_ecdh_path().is_file():
        try:
            return _tpm_ecdh_key(_read_marker(tpm_ecdh_path()), tpm_lib)
        except Exception as e:  # noqa: BLE001
            raise DeviceKeyError(f"the TPM ECDH key named in {tpm_ecdh_path()} could not be "
                                 f"opened ({e})")
    if se_ecdh_path().is_file():
        helper = helper_path(which)
        if not helper:
            raise DeviceKeyError(f"{se_ecdh_path()} is a Secure Enclave key but "
                                 "canopy-device-key is not installed")
        return _se_ecdh_key(helper, runner)
    if ecdh_pem_path().is_file():
        return _sw_ecdh_key(ecdh_pem_path().read_bytes())
    return None


def ensure_ecdh(signing_kind: str, *, runner: Runner = subprocess.run,
                which: Callable[[str], Optional[str]] = shutil.which, tpm_lib=None
                ) -> tuple[EcdhKey, bool]:
    """(this machine's ECDH key, whether it was made now), on the signing key's backend.
    Raises EcdhUnsupported where that backend cannot make one."""
    existing = load_ecdh(runner=runner, which=which, tpm_lib=tpm_lib)
    if existing is not None:
        return existing, False
    if signing_kind == KIND_SE:
        helper = helper_path(which)
        if not helper:
            raise EcdhUnsupported("no canopy-device-key helper for a Secure Enclave ECDH key")
        _ensure_dir()
        p = _run_helper(helper, "ecdh-create", runner=runner)
        if p.returncode == 2:
            raise EcdhUnsupported("canopy-device-key predates ECDH — reinstall Canopy Runner")
        if p.returncode != 0:
            raise _helper_fail("ecdh-create", p)
        return _se_ecdh_key(helper, runner), True
    if signing_kind == KIND_TPM:
        from . import device_proof_tpm as tpm
        try:
            key = tpm.create_ecdh_key(tpm.ECDH_KEY_NAME, tpm_lib)
            jwk = key.public_jwk()
        except Exception as e:  # noqa: BLE001 — a TPM that will not make one
            raise EcdhUnsupported(f"the TPM would not make an ECDH key ({e})")
        _write_private(tpm_ecdh_path(), json.dumps({"provider": tpm.PROVIDER,
                                                    "name": tpm.ECDH_KEY_NAME},
                                                   indent=1).encode("utf-8"))
        return EcdhKey(KIND_TPM, jwk, key.derive), True
    pem = _new_p256_pem()
    _write_private(ecdh_pem_path(), pem)
    return _sw_ecdh_key(pem), True


# --- DPoP proof (RFC 9449) -----------------------------------------------------


def normalize_htu(url: str) -> str:
    """scheme://host/path — no query, no fragment (the contract's `htu`)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def dpop_proof(key: DeviceKey, method: str, url: str, *, now: Optional[float] = None,
               jti: Optional[str] = None) -> str:
    header = {"typ": "dpop+jwt", "alg": "ES256", "jwk": key.public_jwk}
    claims = {"htm": method.upper(), "htu": normalize_htu(url),
              "iat": int(time.time() if now is None else now),
              "jti": jti or str(uuid.uuid4())}
    signing_input = f"{b64url(_compact_json(header))}.{b64url(_compact_json(claims))}"
    return f"{signing_input}.{b64url(key.sign(signing_input.encode('ascii')))}"


def proof_headers(method: str, url: str, *, runner_id: str = "",
                  runner: Runner = subprocess.run,
                  which: Callable[[str], Optional[str]] = shutil.which, tpm_lib=None) -> dict:
    """``{"DPoP": jws}`` when this machine holds a REGISTERED key (status.json says
    active/pending, for `runner_id` when given, with the loaded key's thumbprint), else
    ``{}``. Never raises — canopy's `proof_headers`, plus the runner check."""
    try:
        st = read_status()
        if st.get("status") not in PROOF_STATUSES:
            return {}
        if runner_id and str(st.get("runner") or "") != runner_id:
            return {}
        key = load(runner=runner, which=which, tpm_lib=tpm_lib)
        if key is None:
            return {}
        if not st.get("jkt") or st["jkt"] != key.jkt:
            return {}
        return {"DPoP": dpop_proof(key, method, url)}
    except Exception:  # noqa: BLE001 — a proof is an addition, never a new failure
        return {}


# --- enrollment status (beside the key; never a secret) ------------------------


def read_status() -> dict:
    try:
        data = json.loads(status_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(data: dict) -> None:
    """canopy's format: {id, jkt, status, approve_url, kind, runner, checked_at,
    ecdh_jkt?, ecdh_kind?} — so `canopy vault status` reads what pairing wrote."""
    _write_private(status_path(), json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))


def registered_status(key: DeviceKey, *, key_id, runner_id: str, status: str = "active",
                      ecdh: Optional[EcdhKey] = None, approve_url: str = "") -> dict:
    """The status.json a pairing writes once canopy-web has registered `key`."""
    st = {"id": key_id, "jkt": key.jkt, "status": status, "approve_url": approve_url,
          "kind": key.kind, "runner": runner_id, "checked_at": time.time()}
    if ecdh is not None:
        st.update(ecdh_jkt=ecdh.jkt, ecdh_kind=ecdh.kind)
    return st
