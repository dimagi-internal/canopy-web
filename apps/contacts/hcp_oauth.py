"""The HCP service: OAuth 2.0 for clients canopy does not operate (HCP 5.2.1).

canopy's own agents reach a person's HCP instance as FIRST-PARTY clients (the
grant per agent × channel × host in `hcp.grant_for`). This module is the other
door: an application someone else runs asks the person, through a standard
authorization-code flow, for access to some categories of their instance. That
is what makes canopy's `authorization_profile` "oauth2" and the service eligible
for HCP v1 Interop. The design record, every judgment call, and what "public"
would take are in docs/architecture/hcp-service.md.

The shape, in the spec's terms:

* **Clients are registered by a canopy admin** (`HcpClient`). No dynamic
  registration. Public clients: no secret, PKCE S256 on every code (RFC 7636).
  Redirect URIs match exactly.
* **Scopes are the spec's** `hcp:{category}:{read|write}` (4.1.1), one by one,
  never a wildcard, and never beyond what the client was registered for.
* **Authorization is two acts** (4.1.4, 4.1.6). First the person allows the
  request — told the client, its operator, every category and action, and the
  expiry; the result is a TEMPORARY grant. Then, separately, they are asked
  whether to keep access until they turn it off; nothing is pre-selected. Only
  after both is the grant written and audited (`grant.issued`, with type and
  modality).
* **Reach** is the person's PERSONAL entries plus any workspace they tick on the
  consent screen — recorded on the grant as a restriction (4.1.5.1), so the
  person's grant list shows exactly how narrow it is.
* **The person's own switches bound everything.** A client reads only when the
  person's "use" is available and on by default, and writes only when "record"
  is — whatever it was granted (`hcp.require`, with no session).
* **Tokens** live 1 hour (access) and 30 days (refresh, persistent) or until the
  grant's absolute expiry (temporary) — 4.1.3. Refresh tokens rotate; a used one
  presented again revokes the grant. Revoking a grant kills every token at once
  (4.2.2) and owes the client's webhook a signed, retried notification (4.2.3).
* **Internal only, for now** — `settings.HCP_SERVICE_AUDIENCE`, read here and
  nowhere else.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from . import hcp
from .models import (HcpAuthorizationCode, HcpClient, HcpRevocationDelivery, HcpToken, Person,
                     PersonAuditEvent, PersonFact, PersonGrant)

logger = logging.getLogger(__name__)

# --- lifetimes (HCP 4.1.3) -----------------------------------------------------------

ACCESS_TTL = dt.timedelta(hours=1)
REFRESH_TTL_PERSISTENT = dt.timedelta(days=30)
CODE_TTL = dt.timedelta(minutes=10)
#: The temporary expiries a person may pick; 4 hours is the spec's default and the
#: one shown first. Never more than 24 hours (4.1.4).
TEMPORARY_CHOICES = {"1h": dt.timedelta(hours=1), "4h": dt.timedelta(hours=4),
                     "24h": dt.timedelta(hours=24)}
TEMPORARY_DEFAULT = "4h"
#: How long a step-1 approval waits for the step-2 answer.
PENDING_TTL = dt.timedelta(minutes=15)

ACCESS_PREFIX, REFRESH_PREFIX, CODE_PREFIX = "hcpat_", "hcprt_", "hcpac_"
MODALITY = "oauth2-authorization-code (canopy consent page: allow, then a separate keep-access question)"
#: The source a grant restriction names for the person's personal entries.
PERSONAL = "personal"
#: 4.2.3: at least 5 attempts over at least an hour. The waits between attempts;
#: after the last wait's attempt fails, the delivery is abandoned — 6 attempts
#: spanning about 1 h 51 min.
WEBHOOK_BACKOFF = [dt.timedelta(minutes=m) for m in (1, 5, 15, 30, 60)]
WEBHOOK_MAX_ATTEMPTS = len(WEBHOOK_BACKOFF) + 1
WEBHOOK_TIMEOUT_S = 5


class OAuthError(Exception):
    """An RFC 6749 error: `code` is the wire value."""

    def __init__(self, code: str, description: str, status: int = 400):
        super().__init__(description)
        self.code, self.description, self.status = code, description, status

    def body(self) -> dict:
        return {"error": self.code, "error_description": self.description}


class BadClientOrRedirect(Exception):
    """The client or redirect URI cannot be trusted, so the error is SHOWN, never
    redirected to (that would be an open redirect)."""


# --- the audience gate: the ONE place "internal" is decided ---------------------------


def audience() -> str:
    value = (getattr(settings, "HCP_SERVICE_AUDIENCE", "internal") or "internal").strip().lower()
    return "public" if value == "public" else "internal"


def may_authorize(user) -> tuple[bool, str]:
    """May this signed-in person authorize a client at all? Internal: only an
    address on AUTH_ALLOWED_EMAIL_DOMAIN (login already enforces that; this is
    defence in depth, so the gate does not depend on the login provider)."""
    if audience() == "public":
        return True, ""
    from apps.workspaces.services import allowed_domains

    email = (getattr(user, "email", "") or "").lower()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    allowed = allowed_domains()
    if allowed and domain not in allowed:
        return False, ("This service is open to Dimagi accounts only for now; "
                       f"{email or 'your account'} cannot authorize an app yet.")
    return True, ""


# --- scopes ----------------------------------------------------------------------------


def all_scopes() -> list[str]:
    return [hcp.scope(c, a) for c in hcp.HELD_CATEGORIES for a in hcp.ACTIONS]


def parse_scope(raw: str) -> list[str]:
    """A scope string → the ordered, de-duplicated list. Each must be a fully named
    `hcp:{held category}:{read|write}` (4.1.1: no wildcards)."""
    out = []
    for s in (raw or "").split():
        parts = s.split(":")
        if (len(parts) != 3 or parts[0] != "hcp" or parts[2] not in hcp.ACTIONS
                or parts[1] not in hcp.HELD_CATEGORIES):
            raise OAuthError("invalid_scope", f"{s!r} is not an HCP scope this instance holds "
                                              "(hcp:{category}:{read|write}, no wildcards)")
        if s not in out:
            out.append(s)
    if not out:
        raise OAuthError("invalid_scope", "name at least one hcp:{category}:{action} scope")
    return out


def describe_scopes(scopes: list[str]) -> list[dict]:
    """For the consent screen: one row per category, with what may be done."""
    labels = {"general_preferences": "How you like to work",
              "work_context": "Your work and role",
              "goals_and_constraints": "Your goals and constraints",
              "coordination_context": "How to coordinate with you"}
    rows: dict[str, set] = {}
    for s in scopes:
        _, cat, action = s.split(":")
        rows.setdefault(cat, set()).add(action)
    return [{"category": c, "label": labels.get(c, c),
             "read": "read" in a, "write": "write" in a} for c, a in rows.items()]


# --- the authorization request ----------------------------------------------------------


@dataclass
class AuthorizeRequest:
    client: HcpClient
    redirect_uri: str
    scopes: list[str]
    state: str
    code_challenge: str

    def redirect(self, **params) -> str:
        params = {k: v for k, v in params.items() if v is not None}
        if self.state:
            params["state"] = self.state
        parts = urlsplit(self.redirect_uri)
        query = parts.query + ("&" if parts.query else "") + urlencode(params)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class AuthorizeError(Exception):
    """An error the client is told about by redirect (its redirect URI is trusted)."""

    def __init__(self, req: AuthorizeRequest, code: str, description: str):
        super().__init__(description)
        self.url = req.redirect(error=code, error_description=description)


def parse_authorize(params) -> AuthorizeRequest:
    """Validate an authorization request (RFC 6749 4.1.1 + RFC 7636). The client
    and redirect URI are checked FIRST: until both are trusted, an error is shown,
    not redirected."""
    client = HcpClient.objects.filter(client_id=(params.get("client_id") or "").strip()).first()
    if client is None:
        raise BadClientOrRedirect("This app is not registered with canopy's HCP service.")
    if not client.is_active:
        raise BadClientOrRedirect(f"{client.name} has been disabled by canopy's administrators.")
    redirect_uri = (params.get("redirect_uri") or "").strip()
    if redirect_uri not in (client.redirect_uris or []):
        raise BadClientOrRedirect("That return address is not one this app registered, "
                                  "so canopy will not send you there.")
    req = AuthorizeRequest(client=client, redirect_uri=redirect_uri, scopes=[],
                           state=(params.get("state") or "")[:500], code_challenge="")
    if params.get("response_type") != "code":
        raise AuthorizeError(req, "unsupported_response_type", "only response_type=code")
    challenge = (params.get("code_challenge") or "").strip()
    if not challenge:
        raise AuthorizeError(req, "invalid_request", "PKCE is required: send code_challenge")
    if (params.get("code_challenge_method") or "") != "S256":
        raise AuthorizeError(req, "invalid_request", "PKCE code_challenge_method must be S256")
    if len(challenge) != 43:
        raise AuthorizeError(req, "invalid_request", "code_challenge is a base64url SHA-256 (43 chars)")
    try:
        scopes = parse_scope(params.get("scope") or "")
    except OAuthError as e:
        raise AuthorizeError(req, e.code, e.description) from None
    beyond = [s for s in scopes if s not in (client.allowed_scopes or [])]
    if beyond:
        raise AuthorizeError(req, "invalid_scope",
                             f"{client.name} is not registered to ask for {' '.join(beyond)}")
    req.scopes, req.code_challenge = scopes, challenge
    return req


def workspace_sources(person: Person) -> list[str]:
    """The workspaces holding entries about this person — what the consent screen
    offers to share beyond their personal entries (unticked by default)."""
    return sorted({w for w in hcp.current_versions(person).order_by()
                   .values_list("workspace_id", flat=True).distinct() if w})


# --- step 1 → step 2: a pending approval, kept server-side ------------------------------


def stash_pending(req: AuthorizeRequest, *, user, sources: list[str], expiry: str) -> str:
    """Step 1 done: the person allowed the request. Hold it until they answer the
    separate keep-access question; returns the nonce step 2 must present. Bound to
    the user, so another session cannot complete it."""
    nonce = secrets.token_urlsafe(24)
    cache.set(f"hcp-pending:{nonce}", {
        "user": user.pk, "client": req.client.pk, "redirect_uri": req.redirect_uri,
        "scopes": req.scopes, "state": req.state, "code_challenge": req.code_challenge,
        "sources": sources, "expiry": expiry,
    }, timeout=int(PENDING_TTL.total_seconds()))
    return nonce


def take_pending(nonce: str, user) -> dict | None:
    key = f"hcp-pending:{nonce}"
    data = cache.get(key)
    if not data or data.get("user") != user.pk:
        return None
    cache.delete(key)
    return data


def _restrictions(scopes: list[str], sources: list[str]) -> list[dict]:
    """4.1.5.1: every scope narrowed to the sources the person chose — their
    personal entries, plus any workspaces they ticked."""
    named = [PERSONAL] + [f"workspace:{s}" for s in sources]
    return [{"scope": s, "narrowedTo": {"sources": named}} for s in scopes]


@transaction.atomic
def issue_grant(pending: dict, *, person: Person, user, persistent: bool) -> PersonGrant:
    """Write the grant both acts produced, and audit it (4.1.4: the elected type and
    the modality are recorded in grant.issued; 4.1.5.1: so are the restrictions)."""
    client = HcpClient.objects.get(pk=pending["client"])
    now = timezone.now()
    expires = None if persistent else now + TEMPORARY_CHOICES.get(
        pending.get("expiry") or TEMPORARY_DEFAULT, TEMPORARY_CHOICES[TEMPORARY_DEFAULT])
    sources = [s for s in pending.get("sources") or [] if s in workspace_sources(person)]
    grant = PersonGrant.objects.create(
        person=person, workspace=None, agent=None, hcp_client=client, channel="oauth", host="",
        client_key=f"oauth-client={client.client_id}|grant={uuid.uuid4()}",
        client_name=client.name[:200], scopes=list(pending["scopes"]),
        restrictions=_restrictions(pending["scopes"], sources),
        grant_type=PersonGrant.PERSISTENT if persistent else PersonGrant.TEMPORARY,
        modality="oauth2", expires_at=expires)
    hcp.audit(person, "grant.issued", actor=hcp.user_actor(user), grant=grant,
              detail=(f"client={client.name} ({client.client_id}, operated by {client.operator}); "
                      f"type={grant.grant_type}"
                      + (f"; expires={expires.isoformat()}" if expires else "; until revoked")
                      + f"; modality={MODALITY}; scopes={' '.join(grant.scopes)}; "
                        f"restrictions={json.dumps(grant.restrictions)}"))
    return grant


def issue_code(pending: dict, grant: PersonGrant) -> str:
    raw = CODE_PREFIX + secrets.token_urlsafe(32)
    HcpAuthorizationCode.objects.create(
        code_hash=_hash(raw), client_id=pending["client"], grant=grant,
        redirect_uri=pending["redirect_uri"], code_challenge=pending["code_challenge"],
        expires_at=timezone.now() + CODE_TTL)
    return raw


# --- the token endpoint ------------------------------------------------------------------


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def _client_for(params) -> HcpClient:
    client = HcpClient.objects.filter(client_id=(params.get("client_id") or "").strip()).first()
    if client is None or not client.is_active:
        raise OAuthError("invalid_client", "unknown or disabled client", status=401)
    return client


class _Lapsed(Exception):
    """The grant's absolute expiry has passed: record `grant.expired` (4.3.1) outside
    the refused exchange's transaction, then refuse."""

    def __init__(self, grant: PersonGrant):
        super().__init__("the grant has expired")
        self.grant = grant


def _live_grant(grant: PersonGrant) -> PersonGrant:
    if (grant.status == PersonGrant.ACTIVE and grant.expires_at is not None
            and grant.expires_at <= timezone.now()):
        raise _Lapsed(grant)
    if grant.status != PersonGrant.ACTIVE:
        raise OAuthError("invalid_grant", f"the grant is {grant.status}")
    return grant


def _mint(client: HcpClient, grant: PersonGrant) -> dict:
    now = timezone.now()
    access_raw = ACCESS_PREFIX + secrets.token_urlsafe(32)
    refresh_raw = REFRESH_PREFIX + secrets.token_urlsafe(32)
    access_exp = now + ACCESS_TTL
    if grant.expires_at is not None:
        access_exp = min(access_exp, grant.expires_at)
        refresh_exp = grant.expires_at                      # 4.1.4: never past the grant
    else:
        refresh_exp = now + REFRESH_TTL_PERSISTENT
    HcpToken.objects.create(token_hash=_hash(access_raw), kind=HcpToken.ACCESS, client=client,
                            grant=grant, scopes=list(grant.scopes), expires_at=access_exp)
    HcpToken.objects.create(token_hash=_hash(refresh_raw), kind=HcpToken.REFRESH, client=client,
                            grant=grant, scopes=list(grant.scopes), expires_at=refresh_exp)
    return {"access_token": access_raw, "token_type": "Bearer",
            "expires_in": max(1, int((access_exp - now).total_seconds())),
            "refresh_token": refresh_raw, "scope": " ".join(grant.scopes),
            "hcp_grant_id": hcp.entry_urn(grant.grant_id), "hcp_grant_type": grant.grant_type}


class _Compromised(Exception):
    """A code or refresh token presented a second time: someone else may hold it.
    Raised out of the exchange's transaction so the revocation it calls for is not
    rolled back with the refused exchange."""

    def __init__(self, grant: PersonGrant, error: OAuthError, reason: str):
        super().__init__(reason)
        self.grant, self.error, self.reason = grant, error, reason


def exchange(params) -> dict:
    """POST /oauth/token: authorization_code (with PKCE) or refresh_token."""
    try:
        return _exchange(params)
    except _Compromised as c:
        revoke(c.grant, actor=SYSTEM_ACTOR, reason=c.reason)
        raise c.error from None
    except _Lapsed as lapsed:
        hcp._expire_if_due(lapsed.grant)
        raise OAuthError("invalid_grant", "the grant has expired") from None


SYSTEM_ACTOR = hcp.SYSTEM


@transaction.atomic
def _exchange(params) -> dict:
    grant_type = params.get("grant_type") or ""
    client = _client_for(params)
    if grant_type == "authorization_code":
        raw = (params.get("code") or "").strip()
        code = (HcpAuthorizationCode.objects.select_for_update().select_related("grant__person")
                .filter(code_hash=_hash(raw)).first()) if raw else None
        if code is None or code.client_id != client.pk:
            raise OAuthError("invalid_grant", "unknown authorization code")
        if code.used_at is not None:
            # A code presented twice: someone else may hold it. Kill what it bought.
            raise _Compromised(code.grant, OAuthError("invalid_grant", "authorization code already used"),
                               "authorization code replayed")
        if code.expires_at <= timezone.now():
            raise OAuthError("invalid_grant", "authorization code expired")
        if (params.get("redirect_uri") or "") != code.redirect_uri:
            raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")
        verifier = params.get("code_verifier") or ""
        if not verifier:
            raise OAuthError("invalid_request", "PKCE code_verifier is required")
        if not hmac.compare_digest(_s256(verifier), code.code_challenge):
            raise OAuthError("invalid_grant", "PKCE code_verifier does not match")
        HcpAuthorizationCode.objects.filter(pk=code.pk).update(used_at=timezone.now())
        return _mint(client, _live_grant(code.grant))
    if grant_type == "refresh_token":
        raw = (params.get("refresh_token") or "").strip()
        tok = (HcpToken.objects.select_for_update().select_related("grant__person")
               .filter(token_hash=_hash(raw), kind=HcpToken.REFRESH).first()) if raw else None
        if tok is None or tok.client_id != client.pk or tok.revoked_at is not None:
            raise OAuthError("invalid_grant", "unknown refresh token")
        if tok.used_at is not None:
            raise _Compromised(tok.grant, OAuthError(
                "invalid_grant", "refresh token already used; the grant is revoked"),
                "a rotated refresh token was presented again")
        grant = _live_grant(tok.grant)       # 4.1.4: never honoured past the grant's expiry
        if tok.expires_at <= timezone.now():
            raise OAuthError("invalid_grant", "refresh token expired")
        HcpToken.objects.filter(pk=tok.pk).update(used_at=timezone.now())
        return _mint(client, grant)
    raise OAuthError("unsupported_grant_type", "authorization_code or refresh_token")


def client_revoke(params) -> None:
    """POST /oauth/revoke (RFC 7009): the client gives up access. Revoking any of a
    grant's tokens revokes the grant itself. Unknown tokens are not an error."""
    client = _client_for(params)
    raw = (params.get("token") or "").strip()
    tok = HcpToken.objects.select_related("grant__person").filter(token_hash=_hash(raw)).first() if raw else None
    if tok is None or tok.client_id != client.pk:
        return
    revoke(tok.grant, actor=client_actor(client), reason="revoked by the client")


# --- bearer access (the REST transport) -----------------------------------------------------


def client_actor(client: HcpClient) -> hcp.Actor:
    """An OAuth client acts as an `agent` in the audit log's terms (4.3.2:
    user | agent | system) — never the person."""
    return hcp.Actor(f"client:{client.client_id}", PersonAuditEvent.AGENT)


def authenticate_access(raw: str) -> HcpToken | None:
    """The live access token for a raw bearer value, or None. Live = not revoked,
    not expired, its client enabled, its grant active (expiring it now if due)."""
    if not raw.startswith(ACCESS_PREFIX):
        return None
    tok = (HcpToken.objects.select_related("client", "grant__person", "grant__hcp_client")
           .filter(token_hash=_hash(raw), kind=HcpToken.ACCESS).first())
    if tok is None or tok.revoked_at is not None or tok.expires_at <= timezone.now():
        return None
    if not tok.client.is_active:
        return None
    grant = hcp._expire_if_due(tok.grant)
    if grant.status != PersonGrant.ACTIVE:
        return None
    return tok


def sources_of(grant: PersonGrant) -> set | None:
    """The workspaces (None = personal) an OAuth grant reaches; None for a
    first-party grant (which reaches its own workspace, as before)."""
    if not grant.hcp_client_id:
        return None
    reach: set | None = None
    for r in grant.restrictions or []:
        named = (r.get("narrowedTo") or {}).get("sources")
        if named is None:
            continue
        these = {None if n == PERSONAL else n.split(":", 1)[1]
                 for n in named if n == PERSONAL or str(n).startswith("workspace:")}
        reach = these if reach is None else reach & these
    return reach if reach is not None else {None}


#: HCP 3.4.5: an OAuth client's calls to /api/hcp/v1/, per client, per minute. A 429
#: carries Retry-After; published in the discovery document. The person's own
#: routes — revocation above all — are never limited.
RATE_LIMIT_PER_MINUTE = 120


def check_rate(client: HcpClient) -> None:
    window = int(timezone.now().timestamp() // 60)
    key = f"hcp-rate:{client.pk}:{window}"
    cache.add(key, 0, timeout=120)
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=120)
        count = 1
    if count > RATE_LIMIT_PER_MINUTE:
        raise hcp.HcpError("rate-limited", f"at most {RATE_LIMIT_PER_MINUTE} requests a minute per client")


# --- revocation and its notification (4.2) ------------------------------------------------


@transaction.atomic
def revoke(grant: PersonGrant, *, actor: hcp.Actor, reason: str = "") -> PersonGrant:
    """Revoke the grant, every token it issued — immediately (4.2.2) — and owe its
    client's webhook a notification (4.2.3), which never holds the revocation up."""
    now = timezone.now()
    HcpToken.objects.filter(grant=grant, revoked_at__isnull=True).update(revoked_at=now)
    was_active = grant.status == PersonGrant.ACTIVE
    if was_active:
        PersonGrant.objects.filter(pk=grant.pk).update(status=PersonGrant.REVOKED, revoked_at=now)
        grant.refresh_from_db()
        hcp.audit(grant.person, "grant.revoked", actor=actor, grant=grant,
                  workspace_slug=grant.workspace_id,
                  detail=grant.client_name + (f"; {reason}" if reason else ""))
    client = grant.hcp_client
    if was_active and client is not None and client.webhook_url:
        delivery = HcpRevocationDelivery.objects.create(
            grant=grant, client=client, next_attempt_at=now,
            payload={"type": "grant.revoked", "grantId": hcp.entry_urn(grant.grant_id),
                     "clientId": client.client_id, "revokedAt": now.isoformat(),
                     "hcp_version": hcp.HCP_VERSION})
        transaction.on_commit(lambda: _deliver_soon(delivery.pk))
    return grant


def _deliver_soon(delivery_id: int) -> None:
    import threading

    threading.Thread(target=_attempt_by_id, args=(delivery_id,), daemon=True).start()


def _attempt_by_id(delivery_id: int) -> None:
    from django.db import close_old_connections

    try:
        d = HcpRevocationDelivery.objects.select_related("client", "grant__person").filter(pk=delivery_id).first()
        if d is not None:
            attempt(d)
    except Exception:  # noqa: BLE001
        logger.exception("hcp: revocation delivery %s failed", delivery_id)
    finally:
        close_old_connections()


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """4.2.3: hex HMAC-SHA256 over `<timestamp>.<raw body>`."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def webhook_secret(client: HcpClient) -> str:
    from apps.common.encryption import decrypt_secret as decrypt

    return decrypt(client.webhook_secret_encrypted) if client.webhook_secret_encrypted else ""


def _post(url: str, body: bytes, headers: dict) -> int:
    import urllib.request

    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_S) as resp:  # noqa: S310 — admin-registered https URL
        return resp.status


def attempt(d: HcpRevocationDelivery, *, now: dt.datetime | None = None, post=None) -> None:
    """One delivery attempt. Same payload every time; a fresh HCP-Delivery-Id each
    attempt. Audits `revocation.notified` on first success and on abandonment."""
    now = now or timezone.now()
    with transaction.atomic():
        d = HcpRevocationDelivery.objects.select_for_update().select_related(
            "client", "grant__person").get(pk=d.pk)
        if d.delivered_at or d.abandoned_at or d.next_attempt_at > now:
            return
        body = json.dumps(d.payload, sort_keys=True, separators=(",", ":")).encode()
        ts = int(now.timestamp())
        headers = {"Content-Type": "application/json", "HCP-Timestamp": str(ts),
                   "HCP-Signature": sign(webhook_secret(d.client), ts, body),
                   "HCP-Delivery-Id": str(uuid.uuid4())}
        try:
            status = (post or _post)(d.client.webhook_url, body, headers)
            ok, note = 200 <= status < 300, f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 — any failure is a retry
            ok, note = False, f"{type(exc).__name__}: {exc}"[:200]
        d.attempts += 1
        d.last_status = note
        fields = ["attempts", "last_status"]
        if ok:
            d.delivered_at = now
            fields.append("delivered_at")
            hcp.audit(d.grant.person, "revocation.notified", actor=hcp.SYSTEM, grant=d.grant,
                      detail=f"delivered to {d.client.name} after {d.attempts} attempt(s): {note}")
        elif d.attempts >= WEBHOOK_MAX_ATTEMPTS:
            d.abandoned_at = now
            fields.append("abandoned_at")
            hcp.audit(d.grant.person, "revocation.notified", actor=hcp.SYSTEM, grant=d.grant,
                      detail=f"abandoned after {d.attempts} attempts over "
                             f"{(now - d.created_at).total_seconds() / 3600:.1f}h: {note}")
        else:
            d.next_attempt_at = now + WEBHOOK_BACKOFF[d.attempts - 1]
            fields.append("next_attempt_at")
        d.save(update_fields=fields)


def deliver_due(now: dt.datetime | None = None, limit: int = 20) -> int:
    """Attempt every delivery that is due. Run from the runner heartbeat
    (`maybe_deliver`), like the retention sweep."""
    now = now or timezone.now()
    due = list(HcpRevocationDelivery.objects.filter(
        delivered_at__isnull=True, abandoned_at__isnull=True, next_attempt_at__lte=now)
        .order_by("next_attempt_at")[:limit])
    for d in due:
        try:
            attempt(d, now=now)
        except Exception:  # noqa: BLE001
            logger.exception("hcp: revocation delivery %s failed", d.pk)
    return len(due)


def maybe_deliver(now: dt.datetime | None = None) -> None:
    """The heartbeat's door: at most once a minute across the fleet. Never raises."""
    try:
        if not cache.add("hcp-revocation-deliveries", "1", timeout=60):
            return
        deliver_due(now)
    except Exception:  # noqa: BLE001
        logger.exception("hcp: revocation deliveries failed")


# --- metadata (RFC 8414) and the MCP manifest (Appendix B) ------------------------------------


def authorization_server_metadata(base: str) -> dict:
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": all_scopes(),
        "service_documentation": f"{base}/.well-known/hcp-configuration",
    }


def mcp_manifest() -> dict:
    """Appendix B. The four tools are canopy's agents' (first-party); a client of
    the HCP service uses the REST transport with its OAuth token."""
    return {"name": "hcp-server", "version": hcp.HCP_VERSION,
            "description": "Human Context Protocol v1 -- user-governed preference management",
            "tools": ["hcp_searchPreferences", "hcp_addPreference", "hcp_updatePreference",
                      "hcp_deletePreference"],
            "authorizationRequired": True, "authorizationScheme": "first-party",
            "scopeFormat": "hcp:{category}:{action}"}


# --- the client registry (admins) -------------------------------------------------------------


def new_client_id() -> str:
    return "hcpc_" + secrets.token_urlsafe(12)


def new_webhook_secret() -> tuple[str, str]:
    from apps.common.encryption import encrypt_secret as encrypt

    raw = "hcpwh_" + secrets.token_urlsafe(32)
    return raw, encrypt(raw)


def check_redirect_uri(uri: str) -> str:
    """https everywhere, http only to loopback (native apps, RFC 8252). No
    fragments. Matched exactly at authorization time."""
    uri = (uri or "").strip()
    parts = urlsplit(uri)
    if parts.fragment or not parts.netloc:
        raise ValueError(f"{uri!r}: an absolute URI without a fragment")
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" or (parts.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"}):
        return uri
    raise ValueError(f"{uri!r}: https, or http to localhost only")


def check_scopes(scopes: list[str]) -> list[str]:
    try:
        return parse_scope(" ".join(scopes or []))
    except OAuthError as e:
        raise ValueError(e.description) from None


def reach_entries(grant: PersonGrant):
    """The current versions an OAuth grant reaches (by source), for search."""
    reach = sources_of(grant) or {None}
    from django.db.models import Q

    q = Q()
    for s in reach:
        q |= Q(workspace__isnull=True) if s is None else Q(workspace_id=s)
    return PersonFact.objects.filter(q)
