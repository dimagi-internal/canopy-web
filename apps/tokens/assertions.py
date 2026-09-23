"""Verifying a signed assertion from a connected site.

**What this replaces and why.** A host used to prove itself with a long-lived
shared secret and then simply *say* who its visitor was
(`/api/auth/token-exchange`). Three things are wrong with that and only the
third is obvious: canopy has to store something that verifies the secret, so
compromising canopy's database yields an impersonation credential; the secret
is one static string that must be distributed and rotated by hand; and the
audit trail can only ever record "this app asserted X", never *prove* it,
because anyone holding the secret could have.

A signed assertion fixes all three. canopy stores only a public key. Each claim
is a separate, short-lived, single-use statement about one visitor. And the
trail becomes evidence: a specific key signed a specific claim at a specific
time.

**The verification is deliberately strict**, because every JWT vulnerability
worth the name comes from being lenient:

* The algorithm is chosen by US, never read from the token's header. `alg: none`
  and HMAC-with-the-public-key-as-secret are the two classic forgeries, and
  both work only against a verifier that lets the attacker pick.
* `aud` must match. Without it, an assertion minted for some other canopy
  deployment — or for the host's own purposes — replays here.
* `exp` is required AND capped. A host that issues a year-long assertion has
  recreated the standing secret.
* `jti` is single-use. Within the lifetime window a captured assertion is
  otherwise replayable, which matters most in a browser, where it travels
  through a page the visitor's own extensions can read.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

#: Signature algorithms canopy will accept. Asymmetric ONLY: with a symmetric
#: alg the verification key is the signing key, so publishing a "public" key
#: would publish the ability to sign.
ALLOWED_ALGORITHMS = ["EdDSA", "ES256", "RS256"]

#: The longest an assertion may live. Short because it is used once, at the
#: start of a session, and a longer window is only useful to someone who
#: captured it.
MAX_LIFETIME_SECONDS = 120

#: Tolerance for clock skew between the host and canopy, both directions.
LEEWAY_SECONDS = 30


class AssertionError_(Exception):
    """A refusal, with a code the caller can branch on without parsing prose."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def audience() -> str:
    """Who assertions must be addressed to.

    Defaults to the deployment's own base URL, which is the value a host
    integrator can work out without being told. Configurable because a
    deployment behind a rename would otherwise reject every assertion in
    flight.
    """
    # `SITE_BASE_URL` was named here and is defined nowhere, so every
    # deployment verified against the literal "canopy" while the handoff doc
    # told hosts to send the base URL — a host that followed the doc was
    # refused with `wrong_audience`. `CANOPY_PUBLIC_BASE_URL` is the setting
    # that actually holds it.
    explicit = (getattr(settings, "EMBED_ASSERTION_AUDIENCE", "") or "").strip()
    return explicit or (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")


def _unverified_issuer(token: str) -> str:
    """Read `iss` WITHOUT trusting it — it only selects which key to check.

    Safe, and necessarily so: you cannot verify a signature before knowing
    which key to verify against. Nothing else is read from the token until the
    signature has been checked.
    """
    import jwt

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:  # noqa: BLE001
        raise AssertionError_("malformed", f"not a readable assertion: {exc}") from exc
    iss = (claims.get("iss") or "").strip()
    if not iss:
        raise AssertionError_("no_issuer", "the assertion does not say which app issued it")
    return iss


def verify(token: str, *, app) -> dict:
    """Check an assertion against `app`'s registered keys. Returns its claims.

    Raises `AssertionError_` for every refusal. Never returns a partial result:
    a caller cannot accidentally use claims from a token that failed.
    """
    import jwt

    keys: list = [k.strip() for k in (app.public_keys or [])
                  if isinstance(k, str) and k.strip()]
    # A published JWKS is the preferred half of this: the site rotates on its
    # own and canopy follows, where a pasted PEM has to be re-pasted by hand.
    # Both are accepted at once so a site can move from one to the other
    # without a flag day — and `kid`, when the assertion carries one, is what
    # makes two live keys unambiguous.
    if getattr(app, "jwks_url", ""):
        from . import jwks as jwks_mod

        try:
            kid = str((jwt.get_unverified_header(token) or {}).get("kid") or "")
        except Exception:  # noqa: BLE001 - a header we cannot read is a bad token
            kid = ""
        try:
            keys = keys + jwks_mod.keys_for(app.jwks_url, kid=kid)
        except jwks_mod.JwksError as exc:
            raise AssertionError_(
                "keys_unreachable",
                f"could not read {app.name!r}'s published keys: {exc}",
            ) from exc
    if not keys:
        raise AssertionError_(
            "no_key",
            f"{app.name!r} has no signing key registered, so nothing it signs can be checked",
        )

    last_error: Exception | None = None
    for key in keys:
        try:
            claims = jwt.decode(
                token,
                key,
                # OURS, not the token's. This one argument is the difference
                # between a verifier and a forgery oracle.
                algorithms=ALLOWED_ALGORITHMS,
                audience=audience(),
                leeway=LEEWAY_SECONDS,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat", "jti"],
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iat": True,
                },
            )
            break
        except jwt.InvalidAudienceError as exc:
            raise AssertionError_(
                "wrong_audience",
                f"the assertion is addressed elsewhere; this canopy is {audience()!r}",
            ) from exc
        except jwt.ExpiredSignatureError as exc:
            raise AssertionError_("expired", "the assertion has expired") from exc
        except jwt.MissingRequiredClaimError as exc:
            raise AssertionError_("incomplete", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - try the next key
            last_error = exc
    else:
        # Every registered key rejected it. Rotation is why there is more than
        # one; exhausting them means this was not signed by this app.
        raise AssertionError_(
            "bad_signature",
            f"no registered key for {app.name!r} verifies this assertion ({last_error})",
        )

    # Lifetime cap. `exp` alone only proves the host chose an end; a host
    # issuing year-long assertions has rebuilt the standing secret this exists
    # to remove.
    lifetime = int(claims["exp"]) - int(claims["iat"])
    if lifetime > MAX_LIFETIME_SECONDS + LEEWAY_SECONDS:
        raise AssertionError_(
            "too_long",
            f"assertions may live at most {MAX_LIFETIME_SECONDS}s; this one lives {lifetime}s",
        )

    _spend_jti(app, claims)

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise AssertionError_("no_subject", "the assertion does not say who it is about")
    return claims


def _spend_jti(app, claims: dict) -> None:
    """Single-use, enforced in the cache for the assertion's remaining life.

    Cache rather than a table: the window is under two minutes, the volume is
    one row per session start, and a durable record of every nonce would be a
    table that only ever grows. The audit trail records the ACCEPTED assertion,
    which is the part worth keeping.

    Fails CLOSED on a cache that cannot answer — `cache.add` returning False is
    indistinguishable from a replay, and treating an unknown as fresh would
    turn a Redis blip into a replay window.
    """
    jti = str(claims.get("jti") or "").strip()
    if not jti:
        raise AssertionError_("no_jti", "the assertion has no id, so replay cannot be prevented")
    ttl = max(1, int(claims["exp"]) - int(claims["iat"]) + LEEWAY_SECONDS)
    if not cache.add(f"embed:jti:{app.pk}:{jti}", 1, timeout=ttl):
        raise AssertionError_("replayed", "this assertion has already been used")


def issuer_of(token: str):
    """The app an assertion CLAIMS to be from, before anything is verified.

    Separate from `verify` so a caller can rate-limit between the two: resolving
    the issuer is a base64 decode, verifying is a signature check, and a budget
    spent after the expensive half has not saved anything.

    The app returned is not yet trusted — only named. Nothing may act on it
    until `verify` succeeds.
    """
    from .models import AppCredential

    name = _unverified_issuer(token)
    app = AppCredential.objects.filter(name=name, revoked_at__isnull=True).first()
    if app is None:
        # Same wording whether the app is unknown or revoked: an app name is
        # not a secret, but distinguishing the two tells a prober which of
        # their guesses used to be real.
        raise AssertionError_("unknown_issuer", f"no connected site named {name!r}")
    return app


def verify_for_issuer(token: str):
    """Resolve the app from `iss`, then verify. Returns `(app, claims)`."""
    app = issuer_of(token)
    return app, verify(token, app=app)
