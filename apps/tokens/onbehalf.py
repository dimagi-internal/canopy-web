"""What canopy will say about WHO an agent is answering, to the host's own API.

Who-is-asking §7 / D4. An embedded agent answering a visitor usually has to read
something from the host product, and today it does that with the agent's OWN
credential on the runner — so every visitor effectively shares one identity, and
`docs/architecture/embedding-a-canopy-agent.md` §8a has to warn a host to give an
agent only access that is safe for *every* visitor who can reach it.

The spec's shape for closing that is "canopy forwards each call with a signed
on-behalf-of assertion". Canopy does not sit in that path — the runner calls the
host's MCP server directly — so forwarding would mean becoming a proxy for every
host tool, which is a much larger thing and would put canopy in the middle of
data it has no reason to see. What canopy uniquely holds is the ANSWER to "who
is being answered right now", and that is what this mints: a short, single-use
assertion the agent attaches to its own call, which the host verifies against
canopy's public key (`/api/tokens/on-behalf-of/jwks`) and runs as that person.

The same rules the INBOUND direction already applies (`assertions.py`), now that
canopy is the signer rather than the verifier:

* Asymmetric only. The host holds a public key, so a leak of what the host
  stores cannot mint anything.
* `aud` names ONE host — the site that vouched for this person. An assertion is
  not currency to be spent at whichever product the agent happens to call next.
* `exp` is 120 seconds, and `jti` is there for a host that wants single use.
* `sub` is the caller's id AT THE HOST, never canopy's id for them and never an
  email: the host asserted that id at arrival, so it is the one identifier both
  sides already agree on.

Fails closed in three ways, each of which is a real state rather than an error:
no signing key configured (the deployment simply cannot do this), no host
identity for the caller (an email contact has none — there is nobody for a host
to run as), and no caller at all (a turn the agent gave itself).
"""
from __future__ import annotations

import json
import time
import uuid

from django.conf import settings


class OnBehalfError(Exception):
    """Why canopy will not say this. The message is meant for the agent."""


#: Deliberately one algorithm, not `assertions.ALLOWED_ALGORITHMS`. That list is
#: what canopy ACCEPTS from hosts, which has to be broad enough for what they
#: already run; what canopy ISSUES is its own choice, and one algorithm means a
#: host verifying these has one thing to implement.
ALGORITHM = "EdDSA"

#: Two minutes, matching the inbound cap. It rides one call the agent is making
#: right now, so a longer life buys nothing and lengthens the window in which a
#: copy taken from a log is still spendable.
TTL_SECONDS = 120


def _retired_keys() -> list[str]:
    """Public keys canopy has signed with recently, still published.

    Rotation without this is an outage you schedule: the moment canopy starts
    signing with a new key, every assertion already in flight — and every host
    whose JWKS cache has not expired — is verifying against the old one. So the
    old PUBLIC half stays published for a window after the switch, then goes.
    Public halves only; they verify, they cannot sign.
    """
    raw = getattr(settings, "ONBEHALF_RETIRED_PUBLIC_KEYS", "") or ""
    return [k.strip() for k in raw.split("|") if k.strip() and k.strip() != "PLACEHOLDER"]


def _configured_key() -> str:
    """The key, or "" — where "" includes the CFN secret's birth value.

    A secret container is created holding "PLACEHOLDER" (the same convention
    `apps/slack/services.py` follows), and reading that as a key would be worse
    than reading it as absent: `configured()` would say yes and every signature
    would die inside the crypto library instead of producing the clean "this
    canopy cannot vouch" the agent is meant to get.
    """
    value = (getattr(settings, "ONBEHALF_SIGNING_KEY", "") or "").strip()
    return "" if value == "PLACEHOLDER" else value


def _private_key() -> str:
    key = _configured_key()
    if not key:
        raise OnBehalfError(
            "this canopy has no on-behalf-of signing key, so it cannot vouch for "
            "a caller to another product"
        )
    return key


def configured() -> bool:
    """Whether this deployment can sign at all — an unconfigured one is normal."""
    return bool(_configured_key())


def issuer() -> str:
    """Who the host will see as `iss`. The same name canopy verifies `aud`
    against inbound, so a host has ONE canopy identity, not two."""
    from .assertions import audience

    return audience()


def _thumbprint(jwk: dict) -> str:
    """RFC 7638 thumbprint — the key's `kid`, derived FROM the key.

    A fixed string ("canopy-on-behalf-of") was the bug: two different keys would
    carry the same `kid`, so a host could not tell them apart and rotation had
    nothing to select on. A thumbprint changes when the key does, needs no
    configuration, and is the same value any other library computes.
    """
    import base64
    import hashlib

    # Only the members RFC 7638 defines for this key type, lexicographic, no
    # whitespace — the canonical form is the whole point of a thumbprint.
    canonical = json.dumps({"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"]},
                           separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(canonical).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _jwk_for(public_key) -> dict:
    from jwt.algorithms import OKPAlgorithm

    jwk = OKPAlgorithm.to_jwk(public_key, as_dict=True)
    jwk.update({"use": "sig", "alg": ALGORITHM})
    jwk["kid"] = _thumbprint(jwk)
    return jwk


def _load_public(pem: str):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_public_key(pem.encode())


def active_kid() -> str:
    """The `kid` canopy stamps on what it signs right now."""
    return public_jwk()["kid"]


def public_jwk() -> dict:
    """The public half of the ACTIVE signing key, as a JWK.

    Derived from the private key rather than configured separately: two settings
    that must agree is a way to publish a key that verifies nothing, and the
    failure would appear at the host, days later, as "every assertion is
    invalid".
    """
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(_private_key().encode(), password=None)
    return _jwk_for(key.public_key())


def published_jwks() -> list[dict]:
    """Every key a host should currently accept: the active one, then any
    retired public halves still inside their rollover window."""
    out = [public_jwk()]
    seen = {out[0]["kid"]}
    for pem in _retired_keys():
        try:
            jwk = _jwk_for(_load_public(pem))
        except Exception:  # noqa: BLE001 - a bad retired key must not break the live one
            continue
        if jwk["kid"] not in seen:
            seen.add(jwk["kid"])
            out.append(jwk)
    return out


def public_jwk_pem() -> str:
    """The public half as PEM — what a host pins directly, and what canopy's own
    tests verify against, rather than re-deriving the key a second way."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_private_key(_private_key().encode(), password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def host_identity(turn) -> tuple[str, str]:
    """`(audience, sub)` — which host this caller belongs to, and their id there.

    A contact carries the app that vouched for them and that app's own id for
    them (`apps/contacts/models.py`), which is exactly the pair a host needs:
    it issued the id, so it can resolve it without canopy explaining anything.

    A caller with no such pair is refused rather than described some other way.
    An email contact's address is not a host identity — asserting it would ask
    the host to map an address to an account itself, which is the guess this is
    supposed to remove.
    """
    contact = turn.initiator_contact if turn.initiator_contact_id else None
    if contact is None:
        raise OnBehalfError(
            "this turn has no outside caller for canopy to vouch for"
        )
    if not contact.app_id or not (contact.external_id or "").strip():
        raise OnBehalfError(
            "this caller did not arrive from a connected site, so no host knows "
            "them by an id canopy could assert"
        )
    return contact.app.name, contact.external_id.strip()


def mint(turn, *, agent_slug: str) -> dict:
    """An assertion naming this turn's caller, for the site they came from.

    `act` is the agent — the host is told a machine is acting, and which one, so
    its own audit trail does not record the visitor as having clicked anything.
    """
    import jwt

    aud, sub = host_identity(turn)
    now = int(time.time())
    claims = {
        "iss": issuer(),
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + TTL_SECONDS,
        "jti": str(uuid.uuid4()),
        # Who is doing the asking on their behalf, in the shape RFC 8693 uses
        # for delegation. A host that ignores it still runs as the right
        # person; a host that reads it can say "the agent did this".
        "act": {"sub": f"agent:{agent_slug}"},
    }
    token = jwt.encode(claims, _private_key(), algorithm=ALGORITHM,
                       headers={"kid": active_kid()})
    return {
        "assertion": token,
        "audience": aud,
        "subject": sub,
        "expires_in": TTL_SECONDS,
        "issuer": claims["iss"],
    }
