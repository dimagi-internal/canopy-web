"""The AWS IAM identity proof — an EC2 cloud runner's device binding (Canopy
Vault Phase 1b, canopy-web#1549). The HashiCorp Vault AWS-auth pattern.

An EC2 box has no Secure Enclave; its strongest machine identity is its
instance ROLE. So instead of a DPoP proof it sends a signed-but-unsent
`sts:GetCallerIdentity` request, and canopy-web sends it to STS itself: AWS
answers with the ARN of whoever signed it, which no one without that role's
credentials can forge.

The contract (the runner half, canopy's CLI, builds against it):

* Header `X-Canopy-Aws-Proof` on `GET /api/agents/{slug}/credentials/resolve`
  (and the `proof` field of `POST /api/runner-device-keys/` with
  `kind: "aws-iam"`): base64 of the JSON `{method, url, headers, body}` of a
  SigV4-signed (botocore) `POST https://sts.amazonaws.com/` with body
  `Action=GetCallerIdentity&Version=2011-06-15`. `headers` is an object of
  name → value (a list of values is joined with ",").
* The signature must COVER `X-Canopy-Proof-Htu` (the canopy URL being called;
  any query string is ignored) and `X-Canopy-Proof-Iat` (unix seconds, within
  ±120 s), so a captured proof is no good for another endpoint or later.
* Each proof is single-use: its signature is remembered for the window. So
  clients also sign a random `X-Canopy-Proof-Jti`: without it a proof is a
  pure function of (credentials, htu, second), and two honest resolves of one
  URL in the same second collided here as a "replay" (2026-10-17). It is not
  required — any extra signed header is checked by STS, not by us.

Server side, nothing about WHERE the request goes comes from the client: it is
always sent to `STS_URL` (pinned here, not a setting), and the client's own
`url` must name that endpoint or the proof is refused. `GetCallerIdentity`
needs no IAM permission, so canopy-web's task role needs nothing for this.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit
from xml.etree import ElementTree

from django.core.cache import cache

from . import device_keys
from .models import RunnerDeviceKey, RunnerFleet

PROOF_HEADER = "HTTP_X_CANOPY_AWS_PROOF"
#: The one STS endpoint a proof is ever sent to. Pinned: a client-chosen URL
#: would let a caller point canopy-web at a server that answers any ARN.
STS_URL = "https://sts.amazonaws.com/"
STS_HOST = "sts.amazonaws.com"
STS_TIMEOUT_S = 8
HTU_HEADER = "x-canopy-proof-htu"
IAT_HEADER = "x-canopy-proof-iat"
#: Headers never forwarded (hop-by-hop, or recomputed by the HTTP client).
_DROP = {"content-length", "connection", "transfer-encoding", "accept-encoding"}

_ASSUMED_ROLE = re.compile(r"^arn:aws:sts::(\d{12}):assumed-role/([\w+=,.@-]{1,64})/[\w+=,.@-]{1,64}$")
_ROLE = re.compile(r"^arn:aws:iam::(\d{12}):role/(?:[\x21-\x7e]*/)?([\w+=,.@-]{1,64})$")


class AwsProofError(ValueError):
    """The proof is malformed, stale, replayed, for another URL, or STS refused it."""


def canonical_role_arn(arn: str) -> str:
    """`arn:aws:iam::<acct>:role/<name>` for an IAM role ARN (any path dropped)
    or an STS assumed-role ARN; raises AwsProofError for anything else.

    Paths are dropped, conservatively, because the assumed-role ARN STS returns
    never carries one — `arn:aws:iam::1:role/svc/runner` signs as
    `arn:aws:sts::1:assumed-role/runner/i-0abc` — and a role NAME is unique
    within its account whatever its path, so account + name is exact. Only the
    commercial `aws` partition (the pinned global STS endpoint's) is accepted;
    IAM users and federated sessions are not machine identities and are refused."""
    arn = (arn or "").strip()
    m = _ASSUMED_ROLE.match(arn) or _ROLE.match(arn)
    if not m:
        raise AwsProofError("not an IAM role or assumed-role ARN in the aws partition")
    account, name = m.group(1), m.group(2)
    return f"arn:aws:iam::{account}:role/{name}"


def role_jkt(role_arn: str) -> str:
    """The `jkt` of an aws-iam key: sha256 hex of its canonical role ARN."""
    return hashlib.sha256(canonical_role_arn(role_arn).encode()).hexdigest()


#: An EC2 instance id — what STS names an instance profile's session.
INSTANCE_ID = re.compile(r"^i-[0-9a-f]{8,17}$")


def member_jkt(role_arn: str, instance_id: str) -> str:
    """The `jkt` of a FLEET member's aws-iam key: its role AND its instance
    (`fleets.py`). Every member signs with the one role, so the role alone would
    make them one machine; the instance id STS returns as the session name is
    what tells them apart."""
    return hashlib.sha256(f"{canonical_role_arn(role_arn)}/{instance_id}".encode()).hexdigest()


def session_name(arn: str) -> str:
    """The session name of an STS assumed-role ARN (for an EC2 instance profile,
    the instance id), or "" for anything else."""
    m = _ASSUMED_ROLE_SESSION.match((arn or "").strip())
    return m.group(1) if m else ""


_ASSUMED_ROLE_SESSION = re.compile(r"^arn:aws:sts::\d{12}:assumed-role/[\w+=,.@-]{1,64}/([\w+=,.@-]{1,64})$")


def _decode(raw: str) -> dict:
    try:
        padded = raw.strip() + "=" * (-len(raw.strip()) % 4)
        try:
            data = base64.b64decode(padded, validate=True)
        except binascii.Error:
            data = base64.urlsafe_b64decode(padded)
        obj = json.loads(data)
    except (ValueError, binascii.Error) as exc:
        raise AwsProofError("the proof is not base64 JSON") from exc
    if not isinstance(obj, dict):
        raise AwsProofError("the proof must be a JSON object")
    return obj


def _headers(obj: dict) -> dict[str, str]:
    raw = obj.get("headers")
    if not isinstance(raw, dict):
        raise AwsProofError("the proof's headers must be an object")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            v = ",".join(str(x) for x in v)
        if not isinstance(k, str) or not isinstance(v, (str, int, float)):
            raise AwsProofError("the proof's headers must map names to strings")
        out[k.lower()] = str(v)
    return out


def _signed_headers(authorization: str) -> set[str]:
    if not authorization.startswith("AWS4-HMAC-SHA256 "):
        raise AwsProofError("the proof is not SigV4-signed")
    m = re.search(r"SignedHeaders=([^,\s]+)", authorization)
    if not m:
        raise AwsProofError("the proof's signature names no signed headers")
    return {h.strip().lower() for h in m.group(1).split(";") if h.strip()}


def _signature(authorization: str) -> str:
    m = re.search(r"Signature=([0-9a-fA-F]+)", authorization)
    if not m:
        raise AwsProofError("the proof has no signature")
    return m.group(1).lower()


def _body(obj: dict) -> str:
    body = obj.get("body", "")
    if isinstance(body, bytes):  # pragma: no cover - JSON never yields bytes
        body = body.decode()
    if not isinstance(body, str):
        raise AwsProofError("the proof's body must be a string")
    params = parse_qs(body, keep_blank_values=True)
    if params.get("Action") != ["GetCallerIdentity"] or set(params) - {"Action", "Version"}:
        raise AwsProofError("the proof must be an sts:GetCallerIdentity request")
    return body


def _check_url(obj: dict) -> None:
    try:
        parts = urlsplit(str(obj.get("url", "")))
        port = parts.port
    except ValueError as exc:  # a malformed port
        raise AwsProofError(f"the proof must be signed for {STS_URL}") from exc
    if parts.scheme != "https" or (parts.hostname or "").lower() != STS_HOST \
            or parts.path not in ("", "/") or parts.query or port not in (None, 443):
        raise AwsProofError(f"the proof must be signed for {STS_URL}")
    if str(obj.get("method", "")).upper() != "POST":
        raise AwsProofError("the proof must be a POST")


def _htu_without_query(htu: str) -> str:
    p = urlsplit(htu)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def _call_sts(headers: dict[str, str], body: str) -> str:
    """POST the signed request to the pinned endpoint; the caller's ARN."""
    import requests

    send = {k: v for k, v in headers.items() if k not in _DROP}
    try:
        res = requests.post(STS_URL, headers=send, data=body.encode(), timeout=STS_TIMEOUT_S,
                            allow_redirects=False)
    except requests.RequestException as exc:
        raise AwsProofError(f"STS could not be reached: {type(exc).__name__}") from exc
    if res.status_code != 200:
        raise AwsProofError(f"STS refused the proof (HTTP {res.status_code})")
    return _arn_from_response(res.text, res.headers.get("Content-Type", ""))


def _arn_from_response(text: str, content_type: str) -> str:
    if "json" in content_type:
        try:
            doc = json.loads(text)
            return str(doc["GetCallerIdentityResponse"]["GetCallerIdentityResult"]["Arn"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AwsProofError("STS answered without an Arn") from exc
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise AwsProofError("STS answered with something that is not XML") from exc
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "Arn" and el.text:
            return el.text.strip()
    raise AwsProofError("STS answered without an Arn")


def verify(raw: str, request, *, now: float | None = None) -> str:
    """Verify an `X-Canopy-Aws-Proof` value for THIS request. Returns the
    canonical role ARN of the signer. Consumes the proof (single use).

    Raises AwsProofError with the reason; never returns for a proof that did
    not verify."""
    return canonical_role_arn(verify_caller(raw, request, now=now))


def verify_caller(raw: str, request, *, now: float | None = None) -> str:
    """`verify`, returning the signer's ARN exactly as STS named it — for an EC2
    box `arn:aws:sts::<acct>:assumed-role/<role>/<instance id>`, which `known_key`
    needs to tell one fleet member from another."""
    obj = _decode(raw)
    _check_url(obj)
    headers = _headers(obj)
    body = _body(obj)
    authorization = headers.get("authorization", "")
    signed = _signed_headers(authorization)
    for required in ("host", HTU_HEADER, IAT_HEADER):
        if required not in signed:
            raise AwsProofError(f"the signature must cover {required}")
    # botocore signs `host` from the URL without always copying it into the
    # request's headers; absent is the pinned host, anything else is refused.
    if headers.setdefault("host", STS_HOST).lower() != STS_HOST:
        raise AwsProofError(f"the proof must be signed for host {STS_HOST}")
    if not device_keys.htu_matches(_htu_without_query(headers.get(HTU_HEADER, "")), request):
        raise AwsProofError("the proof is for a different URL")
    try:
        iat = float(headers.get(IAT_HEADER, ""))
    except ValueError as exc:
        raise AwsProofError("the proof's iat is not a number") from exc
    current = time.time() if now is None else now
    if abs(current - iat) > device_keys.IAT_WINDOW_SECONDS:
        raise AwsProofError("the proof is not fresh")
    # Single use, consumed BEFORE the STS round trip so two concurrent replays
    # cannot both get through. add() is atomic: False = seen already.
    sig = hashlib.sha256(_signature(authorization).encode()).hexdigest()
    if not cache.add(f"aws-proof:{sig}", 1, timeout=device_keys.JTI_TTL_SECONDS):
        raise AwsProofError("the proof has already been used")
    arn = _call_sts(headers, body)
    canonical_role_arn(arn)  # refuses an IAM user or a federated session
    return arn


def caller_jkt(caller_arn: str) -> str:
    """The `jkt` a verified caller signs in as: a fleet member's (role + instance)
    when its role is a fleet's, else the role's own. A fleet's role on its own is
    nobody — no key carries its plain `role_jkt` (`fleets.py` refuses one)."""
    role = canonical_role_arn(caller_arn)
    instance = session_name(caller_arn)
    if instance and RunnerFleet.objects.filter(role_jkt=role_jkt(role)).exists():
        return member_jkt(role, instance)
    return role_jkt(role)


def caller_machine(caller_arn: str) -> str:
    """`Runner.machine_id` for a verified caller: a fleet member's role and
    instance (each member is its own machine), else the canonical role."""
    role = canonical_role_arn(caller_arn)
    instance = session_name(caller_arn)
    if instance and RunnerFleet.objects.filter(role_jkt=role_jkt(role)).exists():
        return f"{role}/{instance}"
    return role


def known_key(caller_arn: str):
    """The ACTIVE aws-iam device key for this verified caller (`verify_caller`),
    whichever agents it serves. Given a plain role ARN, the role's own key."""
    return (RunnerDeviceKey.objects.select_related("runner")
            .filter(jkt=caller_jkt(caller_arn), kind=RunnerDeviceKey.AWS_IAM,
                    status=RunnerDeviceKey.ACTIVE).first())


def active_key_for_agent(agent, caller_arn: str):
    """The ACTIVE aws-iam device key for this caller, if its runner serves `agent`."""
    from django.utils import timezone

    key = known_key(caller_arn)
    if key is None or not device_keys.runner_serves_agent(key.runner, agent):
        return None
    RunnerDeviceKey.objects.filter(pk=key.pk).update(last_used_at=timezone.now())
    return key
