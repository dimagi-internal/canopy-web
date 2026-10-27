"""Site sign-offs: a connected site asks a person to approve ONE action with their
passkey (spec `docs/superpowers/specs/2026-10-27-site-sign-offs-design.md`).

The site signs its request with its own key, the one canopy already verifies its
visitor assertions and site agents requests with. canopy decides who may approve
(`approvers_for`), asks that person for a passkey step-up on its own page, and signs
the approval with its client key, the one sites already trust for the site agents list.
No new secret exists, and nothing a token can do approves anything.

`approvers_for` is the ONE answer to "who may approve for this subject", and it is the
SDK's own visitor bar: the subject is whoever `contacts.services.user_for_verified_email`
finds, as `resolve_arrival` does for a visitor, and a person must be a member of the
site's workspace. It is asked again when someone decides, so an admin removed in
between, or a person who left the workspace, can no longer approve.
"""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode, urlsplit

from canopy_sdk import contract
from canopy_sdk import sign_offs as wire
from canopy_sdk.jose import new_jti
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from apps.agents.credentials.login_test_key import agent_of_login, is_person
from apps.agents.models import Agent
from apps.contacts.services import user_for_verified_email
from apps.embedding import assertions
from apps.tokens.models import SiteSignOff
from apps.workspaces import services as wsvc
from apps.workspaces.links import scoped_path, scoped_url

from . import client_identity, site_agents
from .site_agents import SiteAgentsRefused

#: A person's own request is answered at once, in the browser it redirected; an agent's
#: waits for its owner (Jonathan, 2026-10-27).
SUBJECT_LIFETIME = timedelta(minutes=15)
AGENT_ADMINS_LIFETIME = timedelta(hours=24)
EXPIRED = "expired"


def _bases() -> list[str]:
    return [b for b in (client_identity.public_base(),
                        (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")) if b]


def create_audiences() -> list[str]:
    """The ask endpoint under each address canopy answers on (`site_agents.audiences`'s rule)."""
    return list(dict.fromkeys(wire.create_url(b) for b in _bases()))


def status_audiences(sign_off_id: str) -> list[str]:
    return list(dict.fromkeys(wire.status_url(b, sign_off_id) for b in _bases()))


def verify_request(token: str, *, sign_off_id: str | None = None):
    """`(app, claims)` for a request signed by a registered site's own key: to ask
    (`sign_off_id` None) or to read one request. `SiteAgentsRefused` otherwise."""
    app = site_agents.site_for_request(token)
    try:
        keys = assertions.keys_for_app(app, token)
    except assertions.AssertionError_ as exc:
        raise SiteAgentsRefused(exc.code, exc.message) from exc

    def spend(claims: dict) -> None:
        ttl = max(1, int(claims["exp"]) - int(claims["iat"]) + contract.LEEWAY_SECONDS)
        # Fails CLOSED: a cache that cannot record the jti reads as a replay.
        if not cache.add(f"sign-offs:jti:{app.pk}:{claims['jti']}", 1, timeout=ttl):
            raise wire.SignOffError("replayed", "this request has already been used")

    asking = sign_off_id is None
    audiences = create_audiences() if asking else status_audiences(sign_off_id)
    try:
        claims = wire.verify_request(token, keys, audiences=audiences, asking=asking, spend_jti=spend,
                                     algorithms=assertions.ALLOWED_ALGORITHMS)
    except wire.SignOffError as exc:
        status = 422 if exc.code == wire.BAD_RETURN_URL else 400 if exc.code in ("malformed", "bad_alg") else 401
        raise SiteAgentsRefused(exc.code, exc.message, status) from exc
    if claims.get("iss") != app.name:
        raise SiteAgentsRefused("unknown_issuer", f"no connected site named {claims.get('iss')!r}")
    return app, claims


# --- where the approver's browser may go --------------------------------------------


def _origin(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}".lower() if parts.scheme and parts.netloc else ""


def site_origins(app) -> set[str]:
    """The https origins this site registered: where it frames canopy, and where it
    publishes its keys. A return URL anywhere else is refused, so a stolen site key
    cannot send an approver's browser, approval in hand, somewhere new."""
    origins = {_origin(o) for o in app.frame_origins()}
    if app.jwks_url:
        origins.add(_origin(app.jwks_url))
    return {o for o in origins if o.startswith("https://")}


def check_return_url(app, url: str) -> None:
    if _origin(url) not in site_origins(app):
        raise SiteAgentsRefused(wire.BAD_RETURN_URL, "return_url must be https, on an origin this "
                                "site registered (its frame origins or its JWKS URL)", 422)


# --- who may approve ----------------------------------------------------------------


def approvers_for(app, subject: str):
    """`(kind, user, agent)` for `subject` at this site, or `SiteAgentsRefused`
    `no_approver` (422).

    1. The subject is a canopy person (`user_for_verified_email`, `is_person`) who is a
       member of the site's workspace: `subject`, and only that person approves.
    2. The subject is an agent of the site's workspace, by its login or its mailbox:
       `agent_admins`, and its owner or an admin who is a person approves.
    3. Otherwise `no_approver`: the site decides what that means for it.
    """
    email = (subject or "").strip().lower()
    user = user_for_verified_email(email) if email else None
    if user is not None:
        if is_person(user):
            if wsvc.is_member(user, app.workspace_id):
                return SiteSignOff.SUBJECT, user, None
        else:
            agent = agent_of_login(user)
            if agent is not None and agent.workspace_id == app.workspace_id:
                return SiteSignOff.AGENT_ADMINS, user, agent
    if email:
        agent = Agent.objects.filter(email__iexact=email, workspace_id=app.workspace_id).first()
        if agent is not None:
            return SiteSignOff.AGENT_ADMINS, None, agent
    raise SiteAgentsRefused(wire.NO_APPROVER, "nobody in canopy may approve for this account: it is "
                            "not a member of this site's workspace, nor one of its agents", 422)


def may_approve(row: SiteSignOff, user) -> bool:
    """Whether `user` may decide `row` NOW: `approvers_for` asked again, then the
    person it names, or for an agent its owner or an admin, never its own login."""
    if user is None or not user.is_authenticated or not is_person(user):
        return False
    try:
        kind, subject_user, agent = approvers_for(row.app, row.subject)
    except SiteAgentsRefused:
        return False
    if kind != row.approvers:
        return False
    if kind == SiteSignOff.SUBJECT:
        return subject_user is not None and subject_user.pk == user.pk
    return agent is not None and agent.user_id != user.pk and agent.is_admin(user)


# --- the request's life ---------------------------------------------------------------


def approve_url(row: SiteSignOff) -> str:
    return scoped_url(row.app.workspace_id, f"/sign-offs/{row.id}")


def create(app, ask: dict) -> SiteSignOff:
    check_return_url(app, ask["return_url"])
    kind, _user, agent = approvers_for(app, ask["subject"])
    lifetime = SUBJECT_LIFETIME if kind == SiteSignOff.SUBJECT else AGENT_ADMINS_LIFETIME
    return SiteSignOff.objects.create(
        app=app, subject=ask["subject"], action_key=ask["action"]["key"],
        action_title=ask["action"]["title"], action_detail=ask["action"].get("detail") or "",
        site_ref=ask["site_ref"], return_url=ask["return_url"], approvers=kind, agent=agent,
        expires_at=timezone.now() + lifetime)


class SignOffClosedError(Exception):
    """The request was decided already, or has expired."""


def _decide(row: SiteSignOff, user, state: str) -> SiteSignOff:
    with transaction.atomic():
        locked = SiteSignOff.objects.select_for_update().get(pk=row.pk)
        if locked.state_now() != SiteSignOff.OPEN:
            raise SignOffClosedError(locked.state_now())
        locked.state = state
        locked.decided_by = user
        locked.decided_at = timezone.now()
        if state == SiteSignOff.APPROVED:
            locked.approval_jti = new_jti()
        locked.save(update_fields=["state", "decided_by", "decided_at", "approval_jti"])
    return locked


def approve(row: SiteSignOff, user) -> SiteSignOff:
    return _decide(row, user, SiteSignOff.APPROVED)


def decline(row: SiteSignOff, user) -> SiteSignOff:
    return _decide(row, user, SiteSignOff.DECLINED)


def approval_for(row: SiteSignOff, *, now=None) -> str | None:
    """The approval, signed now from the row: the same `jti`, issued at the decision,
    so every copy is one approval that expires ten minutes after it. None when the row
    is not approved or that has passed."""
    if row.state != SiteSignOff.APPROVED or row.decided_at is None or not row.approval_jti:
        return None
    issued = row.decided_at
    if issued + timedelta(seconds=wire.APPROVAL_LIFETIME) <= (now or timezone.now()):
        return None
    approver = row.decided_by
    return client_identity.sign_sign_off(
        row.app.name, subject=row.subject, jti=row.approval_jti, now=issued.timestamp(),
        sign_off={"id": row.id, "action_key": row.action_key, "site_ref": row.site_ref,
                  "approver": {"email": getattr(approver, "email", "") or "",
                               "name": ((approver.get_full_name() or "").strip() if approver else "")},
                  "approved_at": issued.isoformat(), "step_up": wire.STEP_UP_PASSKEY})


def redirect_url(row: SiteSignOff, approval: str | None) -> str:
    """Where the approver's browser goes after deciding: the site's `return_url`, with
    the approval (or the decline) in its query."""
    query = {"sign_off": approval, "id": row.id} if approval else {"declined": row.id}
    sep = "&" if "?" in row.return_url else "?"
    return f"{row.return_url}{sep}{urlencode(query)}"


def page_path(row: SiteSignOff) -> str:
    return scoped_path(row.app.workspace_id, f"/sign-offs/{row.id}")


def open_for(user, *, workspaces: set[str]):
    """Open requests `user` may approve, in `workspaces` (Home's attention list)."""
    rows = (SiteSignOff.objects.filter(state=SiteSignOff.OPEN, expires_at__gt=timezone.now(),
                                       app__workspace_id__in=workspaces)
            .select_related("app", "agent").order_by("-created_at")[:100])
    return [r for r in rows if may_approve(r, user)]
