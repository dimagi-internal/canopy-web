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

from canopy_sdk import consumer, contract
from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)

# The contract's values live in the SDK (`canopy_sdk.contract`), which a host
# builds against too — so canopy and every host read ONE definition. Re-exported
# under the names this module has always had.

#: Signature algorithms canopy will accept. Asymmetric ONLY: with a symmetric
#: alg the verification key is the signing key, so publishing a "public" key
#: would publish the ability to sign.
ALLOWED_ALGORITHMS = list(contract.ASSERTION_ALGORITHMS)

#: The longest an assertion may live. Short because it is used once, at the
#: start of a session, and a longer window is only useful to someone who
#: captured it.
MAX_LIFETIME_SECONDS = contract.ASSERTION_MAX_LIFETIME

#: Tolerance for clock skew between the host and canopy, both directions.
LEEWAY_SECONDS = contract.LEEWAY_SECONDS

#: A refusal, with a code the caller can branch on without parsing prose. The
#: SDK's class, so a refusal raised inside `consumer.verify_visitor_assertion`
#: and one raised here are the same type.
AssertionError_ = consumer.AssertionRefused


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
    # The identity base, not the visited address: a site signs for the audience
    # it was configured with, and that does not move when canopy's address does.
    from .client_identity import public_base

    return explicit or public_base()


def _unverified_issuer(token: str) -> str:
    """Read `iss` WITHOUT trusting it — it only selects which key to check.

    Safe, and necessarily so: you cannot verify a signature before knowing
    which key to verify against. Nothing else is read from the token until the
    signature has been checked.
    """
    return consumer.unverified_issuer(token)


def keys_for_app(app, token: str) -> list:
    """Every key that may verify `token` for `app`: its pasted PEMs plus its
    published JWKS (narrowed by the token's `kid`). Raises `AssertionError_`
    when there are none, or the JWKS cannot be read.

    Shared by the arrival assertion and the host's ID-JAG (`host_grants.py`),
    which the contract signs with the SAME key — one set of rules for whose
    signature canopy accepts from a site.
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
    return keys


def verify(token: str, *, app) -> dict:
    """Check an assertion against `app`'s registered keys. Returns its claims.

    Raises `AssertionError_` for every refusal. Never returns a partial result:
    a caller cannot accidentally use claims from a token that failed. The checks
    themselves (our algorithm list, `aud`, the lifetime cap, `sub`) are the
    SDK's `consumer.verify_visitor_assertion` — the same code a host's CI runs
    its assertions through — and the single-use spend is ours, below.
    """
    keys = keys_for_app(app, token)
    return consumer.verify_visitor_assertion(
        token, keys,
        audience=audience(),
        label=app.name,
        spend_jti=lambda claims: _spend_jti(app, claims),
        algorithms=ALLOWED_ALGORITHMS,
        leeway=LEEWAY_SECONDS,
        max_lifetime=MAX_LIFETIME_SECONDS,
    )


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


def issuer_of(token: str, *, agent_slug: str = ""):
    """The app an assertion CLAIMS to be from, before anything is verified.

    Separate from `verify` so a caller can rate-limit between the two: resolving
    the issuer is a base64 decode, verifying is a signature check, and a budget
    spent after the expensive half has not saved anything.

    `iss` names a site only within a tenant, so the tenant comes from the agent
    the host names (`embed_apps.resolve_site`). The app returned is not yet
    trusted — only named. Nothing may act on it until `verify` succeeds, and it
    is verified against THAT tenant's registered keys.
    """
    from .embed_apps import AmbiguousSite, resolve_site

    name = _unverified_issuer(token)
    try:
        app = resolve_site(name, agent_slug)
    except AmbiguousSite as exc:
        raise AssertionError_("ambiguous_issuer", str(exc)) from exc
    if app is None:
        # Same wording whether the app is unknown, revoked, or registered only
        # by some other tenant: an app name is not a secret, but distinguishing
        # them tells a prober which of their guesses are real somewhere.
        raise AssertionError_("unknown_issuer", f"no connected site named {name!r}"
                              + (f" for agent {agent_slug!r}" if agent_slug else ""))
    return app


def verify_for_issuer(token: str, *, agent_slug: str = ""):
    """Resolve the app from `iss` (and the named agent), then verify.
    Returns `(app, claims)`."""
    app = issuer_of(token, agent_slug=agent_slug)
    return app, verify(token, app=app)
