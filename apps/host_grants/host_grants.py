"""Redeeming a host-issued grant for one visitor (host grant contract v1, §1–§2).

At arrival a host that signed its visitor in may send, beside the usual signed
assertion, an **ID-JAG** (`draft-ietf-oauth-identity-assertion-authz-grant`): a
short JWT, signed by the host's own key, saying "canopy may get a token for MY
MCP server, as this visitor, with this page's scope". canopy redeems it here:

1. **Check it before spending anything.** Signature against the site's own keys
   (the same ones the arrival assertion verifies with), `typ`, `iss` = `aud` =
   the site's issuer, `client_id` = canopy, `resource` = the site's MCP URL,
   `sub` = the arrival assertion's `sub`, and a lifetime of at most 300s. The
   host checks all of this again; canopy checking first means a grant naming
   somebody else, or another resource, is never even sent.
2. **Discover** the host's token endpoint from its RFC 8414 metadata, and
   refuse unless the document's `issuer` is the configured one (RFC 9207's
   concern: a metadata document is only as good as the issuer it names).
3. **Redeem** with the RFC 7523 jwt-bearer grant, authenticating with
   `private_key_jwt`, sender-constrained with a DPoP proof, naming the
   `resource` (RFC 8707).
4. **Store** the access token encrypted, as this ARRIVAL's grant: one row per
   arrival, which the token minted with it points at, so a conversation reaches
   the site through the grant of the page it is on (`grant_for(session=)`).
5. **Wait for the person's passkey.** A stored grant is unusable until the person
   confirms it in the panel (`confirm`), and lasts `CONFIRM_SECONDS` from that
   touch. A re-mint of a page whose permission is confirmed and live inherits the
   confirmation and its end (`_inherit`), so renewing the panel's token never asks
   again and never extends the permission. Only a canopy user has a passkey: a
   contact is never redeemed a grant at all.

**Everything here fails closed, and none of it fails the arrival.** The caller
(`contact_api.contact_token`) catches `HostGrantError`, records an Event and
carries on: the chat still opens, the agent simply has no way to reach the host
as this visitor — which is exactly the state before this existed, minus the
agent's own credential (a visitor turn never falls back to it).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from canopy_sdk import consumer, contract
from django.core.cache import cache
from django.db import models
from django.utils import timezone

from apps.tokens.debug_lifetime import debug_shortened

from . import client_identity, outbound

log = logging.getLogger(__name__)

# The contract's values and checks live in the SDK (`canopy_sdk.contract` /
# `canopy_sdk.consumer`), which hosts build against too. Re-exported under the
# names this module has always had.

#: The contract caps an ID-JAG at 300s after `iat`.
MAX_ID_JAG_LIFETIME = contract.ID_JAG_MAX_LIFETIME
LEEWAY_SECONDS = contract.LEEWAY_SECONDS
#: The contract caps a host access token (30 minutes); a longer claim is clamped,
#: never trusted — a host bug must not make a visitor's token outlive the visit.
MAX_TOKEN_SECONDS = contract.ACCESS_TOKEN_MAX_LIFETIME
ID_JAG_TYP = contract.ID_JAG_TYP
#: What canopy accepts a host to sign with. The contract: EdDSA or ES256.
ID_JAG_ALGORITHMS = list(contract.GRANT_ALGORITHMS)
METADATA_CACHE_SECONDS = 600
#: An ID-JAG is a handful of claims; anything this large is not one, and is
#: refused before a signature check spends CPU on it.
MAX_ID_JAG_BYTES = contract.MAX_JWT_BYTES

#: How long one passkey confirmation lets an agent act as the person on a page.
#: ``CANOPY_HOST_GRANT_CONFIRM_SECONDS`` shortens it for debugging (`debug_shortened`).
CONFIRM_SECONDS = debug_shortened(30 * 60, "CANOPY_HOST_GRANT_CONFIRM_SECONDS")

#: A refusal with a stable `code`. The message never contains a credential. The
#: SDK's class, so a refusal from `consumer.check_id_jag` is this type.
HostGrantError = consumer.RedemptionRefused


def _norm(url: str) -> str:
    return contract.normalize_url(url)


# --- discovery ---------------------------------------------------------------


def metadata_url(issuer: str) -> str:
    """RFC 8414 §3.1: insert the well-known segment between host and path."""
    return contract.metadata_url(issuer)


def discover(issuer: str) -> dict:
    """The host's authorization-server metadata, validated. Cached.

    The `issuer` in the document must be the one configured on the site, and
    the token endpoint it names must itself pass the outbound checks — a
    metadata document is a list of URLs canopy is about to send credentials to.
    """
    issuer = _norm(issuer)
    key = f"tokens:host-as:{issuer}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        doc = outbound.get_json(metadata_url(issuer), what="host metadata URL")
    except outbound.OutboundError as exc:
        raise HostGrantError("metadata_unreachable", str(exc)) from exc
    token_endpoint = consumer.validate_authorization_server_metadata(doc, issuer)
    try:
        outbound.check_url(token_endpoint, what="token endpoint")
    except outbound.OutboundError as exc:
        raise HostGrantError("bad_token_endpoint", str(exc)) from exc
    out = {"issuer": issuer, "token_endpoint": token_endpoint}
    cache.set(key, out, METADATA_CACHE_SECONDS)
    return out


# --- the ID-JAG ----------------------------------------------------------------


def check_id_jag(app, id_jag: str, *, subject: str) -> dict:
    """Verify the host's grant before canopy spends it. Returns its claims.

    The checks are the SDK's `consumer.check_id_jag`; which keys a site has
    registered (its pasted PEMs + published JWKS) is ours, resolved only once
    the header says this is an ID-JAG at all.
    """
    from apps.embedding import assertions

    def keys():
        try:
            return assertions.keys_for_app(app, id_jag)
        except assertions.AssertionError_ as exc:
            raise HostGrantError(exc.code, exc.message) from exc

    return consumer.check_id_jag(
        id_jag, keys,
        issuer=app.host_issuer,
        client_id=client_identity.client_id(),
        resource=app.host_mcp_resource,
        subject=subject,
        label=app.name,
        algorithms=ID_JAG_ALGORITHMS,
        leeway=LEEWAY_SECONDS,
        max_lifetime=MAX_ID_JAG_LIFETIME,
    )


# --- redemption ------------------------------------------------------------------


def _token_request(endpoint: str, form: dict):
    """One POST, retried ONCE if the host demands a DPoP nonce (RFC 9449 §8).

    `outbound.post_form` is looked up per call (not bound at import) so the
    transport stays the one guarded door out — and replaceable in tests.
    """
    audience = form["_aud"]
    body = {k: v for k, v in form.items() if k != "_aud"}
    return consumer.request_token(
        lambda url, data, headers, what="": outbound.post_form(url, data, headers=headers, what=what),
        endpoint, body, audience=audience,
        client_assertion=client_identity.client_assertion,
        dpop_proof=lambda htm, htu, nonce=None: client_identity.dpop_proof(htm, htu, nonce=nonce),
    )


def redeem(app, id_jag: str, *, subject: str, contact=None, user=None,
           runner_requirements=(), agent=None, probe: bool = False):
    """Redeem the host's ID-JAG for a DPoP-bound access token and store it.

    `runner_requirements` are the arrival's (already parsed from the verified
    assertion); they are stored on the grant so the gateway can enforce them
    whichever conversation later reaches the grant. `agent` is the agent the
    arrival was BOUND to (`canopy_agent`), or None: a bound grant is kept apart
    from the visitor's other grants and only that agent reaches it.

    Returns the `HostGrant`, unconfirmed unless the same page's permission is
    confirmed and live (`_inherit`). Raises `HostGrantError` for every refusal — the
    caller decides that a refusal does not fail the arrival. A contact (no `user`) is
    always refused: confirming takes a passkey, which only a canopy user has. The
    live probe (`probe=True`) is the one grant with no person: its synthetic principal
    is no canopy account, `lookup` never reaches it, and it is deleted when the probe
    ends, so it is stored confirmed.
    """
    from apps.common.encryption import encrypt_secret

    from apps.tokens.models import HostGrant  # noqa: PLC0415 — a model read: loads with a call, as this module's other model reads

    if user is None and not probe:
        raise HostGrantError("contact", "an act-as-you permission is confirmed with a passkey, "
                                        "which only a canopy user has")
    if not app.issues_host_grants():
        raise HostGrantError("not_configured",
                             f"{app.name!r} has no host issuer / MCP resource configured")
    if not client_identity.configured():
        raise HostGrantError("no_client_key", "this canopy has no OAuth client keys")

    claims = check_id_jag(app, id_jag, subject=subject)
    meta = discover(app.host_issuer)
    try:
        status, doc = _token_request(meta["token_endpoint"], {
            "_aud": meta["issuer"],
            **consumer.redemption_form(id_jag, client_id=client_identity.client_id(),
                                       resource=app.host_mcp_resource),
        })
    except outbound.OutboundError as exc:
        raise HostGrantError("token_endpoint_unreachable", str(exc)) from exc
    # The OAuth error CODE only (`error_description` is the host's prose and
    # could echo anything back); DPoP or nothing (a bearer token here would be
    # usable by anyone who copied it); `expires_in` clamped, never trusted.
    answer = consumer.parse_token_response(status, doc, max_lifetime=MAX_TOKEN_SECONDS)
    token, lifetime = answer.access_token, answer.expires_in
    scope = str(answer.scope or claims.get("scope") or "")[:500]

    # This arrival's own row. A visitor's pages each arrive (and re-arrive every few
    # minutes), and a host may hold each page's grant to what THAT page shows: one
    # row per visitor let the last page to arrive speak for all of them. A grant no
    # longer lives once it expires, so the visitor's expired ones go now.
    now = timezone.now()
    HostGrant.objects.filter(app=app, subject=subject, expires_at__lte=now).delete()
    grant = HostGrant(
        app=app, subject=subject, agent=agent, contact=None, user=user,
        access_token_enc=encrypt_secret(token),
        scope=scope,
        resource=app.host_mcp_resource,
        dpop_jkt=client_identity.dpop_jkt(),
        runner_requirements=sorted(set(runner_requirements or ())),
        expires_at=now + timedelta(seconds=lifetime),
    )
    if probe:
        grant.confirmed_at = now
    else:
        _inherit(grant)
    grant.save()
    return grant


def _inherit(grant) -> None:
    """Carry the same page's live confirmation onto a re-mint, ending when it ends.

    The panel renews its token every few minutes while its tab is open, and each
    renewal is a fresh row; asking for the passkey on each would be a prompt every
    few minutes, and letting each renewal start a new 30 minutes would make one
    touch last as long as the tab. So a row inherits the confirmation of the page's
    newest live confirmed row, and its end."""
    held = (confirmed(unexpired(same_page(grant)))
            .order_by("-confirmed_at", "-pk").first())
    if held is None:
        # Still waiting: an agent's ask (`ask`) carries over too, or a renewal
        # mid-turn would close the prompt the person is about to answer.
        asked = pending(same_page(grant)).filter(requested_at__isnull=False).order_by("requested_at").first()
        if asked is not None:
            grant.requested_at = asked.requested_at
            grant.requested_reason = asked.requested_reason
        return
    grant.confirmed_at = held.confirmed_at
    grant.expires_at = min(grant.expires_at, held.confirmed_at + timedelta(seconds=CONFIRM_SECONDS))


#: The longest reason an agent can put in front of the person.
MAX_REASON = 300


def ask(grant, reason: str = "") -> int:
    """An agent needs `grant`, still waiting for the passkey: mark its page's waiting
    rows requested, with what the agent said it needs it for, so the panel asks the
    person now (`host_gateway.resolve` pushes it there). A send alone never asks — the
    turn may never act as them. The first ask's time stands; a later ask's reason
    replaces the earlier one. Returns how many rows it marked."""
    rows = pending(same_page(grant))
    rows.filter(requested_at__isnull=True).update(requested_at=timezone.now())
    return rows.update(requested_reason=" ".join(str(reason or "").split())[:MAX_REASON])


def confirm(grant, *, now=None) -> None:
    """The person just confirmed `grant` with their passkey: usable from now, for
    `CONFIRM_SECONDS` at most (never past the host token's own end)."""
    now = now or timezone.now()
    grant.confirmed_at = now
    grant.expires_at = min(grant.expires_at, now + timedelta(seconds=CONFIRM_SECONDS))
    grant.save(update_fields=["confirmed_at", "expires_at", "updated_at"])


#: Why `lookup` found no grant: the person has none this conversation may use, the
#: page's grant ran out, the person stopped it, or never confirmed it with a passkey.
NONE, EXPIRED, REVOKED, UNCONFIRMED = "none", "expired", "revoked", "unconfirmed"


class Lookup:
    """`lookup`'s answer: the grant, or why there is none (`NONE`/`EXPIRED`/`REVOKED`/
    `UNCONFIRMED`). For `UNCONFIRMED`, `waiting` is the grant that waits for the
    passkey, so the caller can `ask` the person for it."""

    __slots__ = ("grant", "reason", "waiting")

    def __init__(self, grant=None, reason: str = "", waiting=None):
        self.grant, self.reason = grant, ("" if grant is not None else (reason or NONE))
        self.waiting = waiting


def unexpired(rows):
    """Not expired, not revoked — confirmed or not."""
    return rows.filter(expires_at__gt=timezone.now(), revoked_at__isnull=True)


def confirmed(rows):
    return rows.filter(confirmed_at__isnull=False)


def live(rows):
    """The rows of `rows` that can be used: confirmed with a passkey, not expired,
    not revoked. Every reader that spends a grant goes through this (or `lookup`)."""
    return confirmed(unexpired(rows))


def pending(rows):
    """The rows of `rows` waiting for the person's passkey: unexpired, unconfirmed."""
    return unexpired(rows).filter(confirmed_at__isnull=True)


def _principal_rows(app, *, contact=None, user=None):
    """A user's grants at `app`; None for a contact, who never holds one."""
    from apps.tokens.models import HostGrant  # noqa: PLC0415 — a model read: loads with a call, as this module's other model reads

    if contact is not None or user is None:
        return None
    return HostGrant.objects.filter(app=app, user=user, contact__isnull=True)


def lookup(app, *, agent, contact=None, user=None, session=None) -> Lookup:
    """The visitor's grant at `app` that `agent` may use — the ONE lookup the gateway
    (a turn) and MCP Apps Views (a viewer) share — or why there is none.

    **In a conversation (`session`): the grant of the PAGE that last acted in it as
    this person, while it lives, and nothing else.** A page acts by sending, by
    declaring its page state or actions, by starting the conversation, or by a View
    call it makes (`canopy_sessions.services.note_host_grant`). A host may hold a
    page's grant to what that page shows, so a conversation on one page must never
    read through the grant another of their pages minted — not even once its own
    has run out: then the answer is `EXPIRED` ("reload the page"), a stopped
    grant is `REVOKED`, and one the person has not confirmed with their passkey is
    `UNCONFIRMED`. A page only holds a grant once the person sent from it (the
    panel asks its host for one then, never on load), so a turn never reaches a
    grant the person did not trigger.

    A contact has none (`_principal_rows`).

    **Outside one (`session=None`)**: their latest LIVE (confirmed) grant — one bound to `agent`
    first (the page that chose this agent granted those scopes for it), then an
    unbound one (an arrival that named no binding); expired and stopped rows never.

    Never a grant bound to a DIFFERENT agent — a data page's grant, minted for
    `labsdata`, is not ACE's to spend however recently it was redeemed."""
    from django.db.models import Q  # noqa: PLC0415 — as the model reads below

    rows = _principal_rows(app, contact=contact, user=user)
    if rows is None:
        return Lookup(reason=NONE)
    agent_id = getattr(agent, "pk", agent)
    usable = rows.filter(Q(agent_id=agent_id) | Q(agent__isnull=True)) if agent_id else \
        rows.filter(agent__isnull=True)
    if session is not None:
        from apps.canopy_sessions.services import page_host_grant_id  # noqa: PLC0415 — as this module's other model reads: loads with a call, never at import

        pinned = page_host_grant_id(session, user=user if contact is None else None, contact=contact)
        if not pinned:
            return Lookup(reason=NONE)
        grant = rows.filter(pk=pinned).first()
        if grant is None:
            # Pruned: a visitor's expired grants go at their next arrival.
            return Lookup(reason=EXPIRED)
        if not usable.filter(pk=pinned).exists():
            return Lookup(reason=NONE)
        if grant.revoked_at is not None:
            return Lookup(reason=REVOKED)
        if grant.expires_at <= timezone.now():
            return Lookup(reason=EXPIRED)
        if grant.confirmed_at is None:
            return Lookup(reason=UNCONFIRMED, waiting=grant)
        return Lookup(grant)
    # Bound before unbound (NULLs last), then the latest arrival — live rows only,
    # so an expired bound grant can never sort ahead of a live unbound one.
    grant = live(usable).order_by(models.F("agent_id").asc(nulls_last=True), "-created_at", "-pk").first()
    return Lookup(grant)


def grant_for(app, *, agent, contact=None, user=None, session=None):
    """`lookup(...).grant`: the grant, or None."""
    return lookup(app, agent=agent, contact=contact, user=user, session=session).grant


# --- seeing and stopping one's own grants -----------------------------------------


def same_page(grant):
    """Every grant that is the same page's permission as `grant`: the same person at
    the same site, for the same agent (or none), with the same scopes. The panel
    re-mints while its tab is open, so one page's permission is several rows; a
    person sees and stops it as one."""
    from apps.tokens.models import HostGrant  # noqa: PLC0415 — a model read: loads with a call, as this module's other model reads

    rows = HostGrant.objects.filter(app_id=grant.app_id, agent_id=grant.agent_id, scope=grant.scope)
    if grant.contact_id is not None:
        return rows.filter(contact_id=grant.contact_id)
    return rows.filter(user_id=grant.user_id, contact__isnull=True)


def revoke(grant) -> int:
    """Stop `grant` and every unexpired grant of the same page (`same_page`),
    confirmed or still waiting, at once: the gateway and Views refuse them from now on
    (`lookup` → `REVOKED`), and their access tokens are wiped. Returns how many rows
    it stopped."""
    return unexpired(same_page(grant)).update(revoked_at=timezone.now(), access_token_enc="")


def live_for(*, user=None, contact=None) -> list:
    """The person's live (confirmed) grants, one per page (`same_page`) — the newest
    row of each, newest first: every grant redeemed for their account, whichever site
    and workspace. A contact holds none."""
    from apps.tokens.models import HostGrant  # noqa: PLC0415 — a model read: loads with a call, as this module's other model reads

    if contact is not None or user is None or not getattr(user, "is_authenticated", False):
        return []
    rows = HostGrant.objects.filter(user=user)
    out, seen = [], set()
    for grant in live(rows).select_related("app", "agent").order_by("-expires_at", "-pk"):
        key = (grant.app_id, grant.agent_id, grant.scope)
        if key not in seen:
            seen.add(key)
            out.append(grant)
    return out


def record_outcome(app, *, ok: bool, subject: str, code: str = "", detail: str = "",
                   scope: str = "") -> None:
    """One Event per redemption — the place an operator looks when "the agent
    could not reach the host" and nothing else says why. Best-effort."""
    try:
        from apps.events import services as events

        events.record([{
            "source": "tokens.host_grants",
            "kind": "host_grant.redeemed" if ok else "host_grant.refused",
            "level": "info" if ok else "warn",
            # Coalesce failures per site+code so a misconfigured host is one
            # row with a count, not a row per page view.
            "key": "" if ok else f"{app.pk}:{code}",
            "summary": (f"{app.name}: host grant redeemed (scope {scope or '-'})" if ok
                        else f"{app.name}: host grant refused — {code}"),
            "payload": {"site": app.name, "subject": subject, "code": code,
                        "detail": detail[:300], "scope": scope},
        }], workspace=app.workspace)
    except Exception:  # noqa: BLE001 - bookkeeping must never fail an arrival
        log.exception("could not record a host grant event")
