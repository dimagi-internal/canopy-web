"""What a host SIGNS: the visitor assertion and the ID-JAG, and the arrival
request that carries them to canopy.

Extracted from connect-labs ``connect_labs/labs/canopy.py`` (PR #2060) and
generalised. Two rules from there are load-bearing:

* ``sub`` is the HOST'S OWN id for its signed-in user and nothing else. The
  host is asserting "this is a real person here", and canopy believes it because
  it verified the host's signature. A subject taken from anything the browser
  sent would let any caller be anybody — so every function here takes the
  subject from the caller's server-side session, never from a request body.
* The ID-JAG's ``scope`` is decided server-side from the host's own route
  registry (``PageTokens``), never from the browser. An unregistered page gets no
  ID-JAG.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

from .. import contract
from ..jose import new_jti, sign
from .config import HostConfig, HostNotConfigured

log = logging.getLogger("canopy_sdk.host")

MINT_TIMEOUT_SECONDS = 10


class MintFailed(RuntimeError):
    """canopy refused the arrival or could not be reached. Carries canopy's own
    reason code where it gave one (``replayed``, ``bad_signature``, …) in the
    message, and ``status``: canopy's HTTP status for a refusal, ``None`` when
    canopy could not be reached or answered 200 without a token."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


def sign_visitor_assertion(config: HostConfig, subject: str, *, name: str = "", email: str = "",
                           email_verified: bool = False, extra: dict | None = None,
                           now: float | None = None) -> str:
    """A signed statement that ``subject`` is a real person on this host, now.

    ``email_verified=True`` is the host vouching for the address; canopy lets
    such a visitor in as their OWN canopy account only if exactly one canopy
    user holds that address verified and is a member of the site's workspace.
    Never claimed for an empty address.
    """
    if not config.assertions_enabled:
        raise HostNotConfigured("the canopy base URL and the host's app name are required")
    subject = str(subject or "").strip()
    if not subject:
        raise ValueError("an assertion needs the host's own id for the visitor")
    issued = int(time.time() if now is None else now)
    claims = {
        **(extra or {}),
        "iss": config.app_name,
        "sub": subject,
        "aud": config.audience,
        "iat": issued,
        "exp": issued + config.assertion_ttl,
        "jti": new_jti(),
        "name": name or "",
        "email": email or "",
        "email_verified": bool(email_verified and email),
    }
    # `kid` so canopy can pick this key out of the JWKS the host publishes;
    # without it a rotation has nothing to select on.
    return sign(claims, config.signing_key, headers={"kid": config.kid})


def issue_id_jag(config: HostConfig, subject: str, scopes, *, now: float | None = None) -> str:
    """An ID-JAG letting canopy act as ``subject`` at this host's MCP, within ``scopes``.

    The host is both issuer and audience (it grants for its own authorization
    server), canopy can only REDEEM it (authenticated as itself, holding a DPoP
    key), and it is signed with the same key as the visitor assertion so canopy
    needs no second key. ``sub`` MUST equal the assertion's ``sub``.

    Unknown scopes are dropped; none left is a ``ValueError``.
    """
    if not config.grant_enabled:
        raise HostNotConfigured("canopy_client_id, issuer, resource and token_endpoint are required")
    wanted = [s for s in contract.scopes_of(" ".join(scopes or ())) if s in config.scope_tools]
    if not wanted:
        raise ValueError("an ID-JAG needs at least one scope this server offers")
    subject = str(subject or "").strip()
    if not subject:
        raise ValueError("an ID-JAG needs the host's own id for the visitor")
    issued = int(time.time() if now is None else now)
    claims = {
        "iss": config.issuer,
        "aud": config.issuer,
        "sub": subject,
        "client_id": config.canopy_client_id,
        "resource": config.resource,
        "scope": " ".join(wanted),
        "iat": issued,
        "exp": issued + config.id_jag_ttl,
        "jti": new_jti(),
    }
    return sign(claims, config.signing_key,
                headers={"kid": config.kid, "typ": contract.ID_JAG_TYP})


def arrival_payload(config: HostConfig, subject: str, *, scopes=(), agent_slug: str = "",
                    **assertion_kwargs) -> dict:
    """The body a host POSTs to canopy's arrival endpoint.

    With ``scopes`` (the page's, from ``PageTokens.scopes``) and the grant on, the
    same request carries an ``id_jag``. Without either it is exactly the request
    it was before grants existed. A failed ID-JAG never fails the arrival: no
    grant is a working panel (the agent acts as itself); a failed mint is not.
    """
    payload = {
        contract.ARRIVAL_ASSERTION_FIELD: sign_visitor_assertion(config, subject, **assertion_kwargs),
        # Names which canopy tenant this is for; a site name is unique only per
        # canopy workspace.
        contract.ARRIVAL_AGENT_FIELD: agent_slug or "",
    }
    if scopes and config.grant_enabled:
        try:
            payload[contract.ARRIVAL_ID_JAG_FIELD] = issue_id_jag(config, subject, scopes)
        except (HostNotConfigured, ValueError):
            log.warning("could not issue an ID-JAG for this visitor", exc_info=True)
    return payload


def mint_contact_token(config: HostConfig, payload: dict, *, timeout: float = MINT_TIMEOUT_SECONDS,
                       opener=None) -> dict:
    """POST the arrival to canopy; returns canopy's whole response.

    Always carries ``token`` and ``expires_at``; ``kind`` (``"user"`` when the
    visitor arrives as their own canopy account, else ``"contact"``) and
    ``host_grant`` (whether an ID-JAG in ``payload`` was redeemed) default when
    an older canopy omits them. Everything else canopy returns (``contact_id``,
    ``display_name``, …) is passed through as-is — a host that routes on
    ``kind`` needs it, so dropping fields made every such host hand-roll the
    request. Pass only ``token`` / ``expires_at`` / ``kind`` on to a browser.
    """
    if not config.canopy_base_url:
        raise HostNotConfigured("the canopy base URL is required")
    request = urllib.request.Request(
        config.canopy_base_url + contract.ARRIVAL_PATH,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    open_ = opener or urllib.request.urlopen
    try:
        with open_(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        # canopy names the reason in the body; it is the only useful thing in
        # the failure. Never the assertion itself.
        detail = ""
        try:
            detail = exc.read().decode()[:500]
        except Exception:  # noqa: BLE001 - a body that cannot be read
            pass
        raise MintFailed(f"canopy returned {exc.code}: {detail}", status=exc.code) from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise MintFailed(f"canopy could not be reached: {type(exc).__name__}") from exc
    token = body.get("token") if isinstance(body, dict) else None
    if not token:
        raise MintFailed("canopy returned no token")
    out = dict(body)
    out.setdefault("expires_at", "")
    out.setdefault("kind", "contact")
    out.setdefault("host_grant", False)
    return out
