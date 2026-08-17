"""Django Ninja router for /api/inbound — the push doorbell and its configuration.

The doorbell (``POST /gmail/{workspace}/``) is ``auth=None`` and self-enforcing,
following ``POST /api/auth/token-exchange``'s precedent: an unauthenticated
endpoint that verifies a credential itself and is explicitly allowlisted in
``apps/common/middleware.py``.

**The workspace is in the URL, and that is the multi-tenancy.** Verification
binds to that workspace's own audience + signer, so a second tenant — in its own
GCP project, with its own push service account — verifies correctly, and one
tenant's service account can never satisfy another's check. It also means the
tenant is known BEFORE the payload is decoded, so no attacker-controlled byte
chooses which credential it is checked against, and an unknown mailbox is logged
against the right workspace instead of leaking into the default one.

Everything an operator needs is here rather than in env vars and a Django shell:
audience, signer, topic, and the mailbox list are all first-class, owner-editable
records, and the push URL is server-computed so nobody hand-copies it wrong.

Two response rules that look odd and are deliberate:

* An unverified push gets **404, not 403** — a probe learns nothing about whether
  a workspace exists or accepts push.
* A push we cannot act on still gets **200**. A 4xx tells Pub/Sub to REDELIVER,
  so a mailbox we do not own would be retried forever. The refusal belongs in the
  event log, where someone can see it, not on the wire where it becomes a storm.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.inbound import services
from apps.inbound.models import InboundMailbox
from apps.inbound.schemas import (
    MailboxIn,
    MailboxListOut,
    MailboxOut,
    MailboxPatchIn,
    PushConfigIn,
    PushConfigOut,
    PushEnvelopeIn,
    PushResultOut,
    RunnerMailboxListOut,
    WatchReportIn,
    WatchReportOut,
)
from apps.inbound.verify import VerificationError, verify_push
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)

router = Router(auth=session_auth, tags=["inbound"])


# ── helpers ──────────────────────────────────────────────────────────────────


def _workspace_or_404(user, slug: str) -> Workspace:
    """Membership first — a non-member gets 404, never a role hint."""
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.user_workspace_slugs(user):
        raise HttpError(404, "no such workspace")
    return ws


def _owner_workspace_or_404(user, slug: str) -> Workspace:
    """Push config is security config: reads are member, writes are owner."""
    ws = _workspace_or_404(user, slug)
    m = WorkspaceMembership.objects.filter(workspace=ws, user=user).first()
    if m is None or m.role != WorkspaceMembership.OWNER:
        raise HttpError(403, "requires the owner role")
    return ws


def _config_out(request: HttpRequest, cfg) -> dict:
    return {
        "workspace": cfg.workspace_id,
        "audience": cfg.audience,
        "service_account": cfg.service_account,
        "watch_topic": cfg.watch_topic,
        "push_url": services.push_url(request, cfg.workspace_id),
        "verifies": cfg.verifies,
        "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else "",
    }


def _mailbox_out(mb: InboundMailbox) -> dict:
    return {
        "id": mb.pk,
        "address": mb.address,
        "agent_slug": mb.agent.slug,
        "workspace": mb.agent.workspace_id,
        "enabled": mb.enabled,
        "last_push_at": mb.last_push_at.isoformat() if mb.last_push_at else "",
        "watch_expires_at": mb.watch_expires_at.isoformat() if mb.watch_expires_at else "",
        "watch_error": mb.watch_error,
        "watch_state": services.watch_state(mb),
    }


# ── the doorbell ─────────────────────────────────────────────────────────────


def _decode(message: dict) -> dict:
    """Pull ``{emailAddress, historyId}`` out of a Pub/Sub message.

    The payload is base64 in ``message.data``. A malformed one is not an error
    worth raising: Pub/Sub would redeliver it forever, and there is nothing to
    fix on our side.
    """
    raw = message.get("data") or ""
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return {}
    try:
        parsed = json.loads(decoded)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@router.post(
    "/gmail/{workspace}/",
    response=PushResultOut,
    auth=None,
    url_name="gmail_push",
    summary="Gmail Pub/Sub push (doorbell — carries no mail)",
)
def gmail_push(request: HttpRequest, workspace: str, payload: PushEnvelopeIn) -> dict:
    """Ring the runner that holds this mailbox's credentials.

    This endpoint never reads mail. A Gmail notification carries
    ``{emailAddress, historyId}`` and no content, so the runner — which owns the
    per-agent ``gog`` OAuth clients — does the read it already does on its poll.
    """
    ws = Workspace.objects.filter(slug=workspace).first()
    cfg = services.get_config(ws) if ws is not None else None
    try:
        verify_push(request, cfg)
    except VerificationError as exc:
        # 404 for an unknown workspace AND for a bad token: a probe must not be
        # able to enumerate which workspaces accept push.
        logger.warning("inbound gmail push refused for %r: %s", workspace, exc)
        raise HttpError(404, "not found") from None

    body = _decode(payload.message or {})
    address = body.get("emailAddress") or ""
    if not address:
        logger.warning("inbound gmail push for %r carried no emailAddress", workspace)
        return {"ok": False, "reason": "no_address", "rang": []}

    result = services.handle_push(address, ws, str(body.get("historyId") or ""))
    return {
        "ok": bool(result.get("ok")),
        "reason": result.get("reason", ""),
        "rang": result.get("rang", []),
    }


# ── configuration (what the UI drives) ───────────────────────────────────────


@router.get("/config/{workspace}", response=PushConfigOut, summary="Read push config")
def get_push_config(request: HttpRequest, workspace: str) -> dict:
    ws = _workspace_or_404(request.user, workspace)
    return _config_out(request, services.get_config(ws))


@router.put("/config/{workspace}", response=PushConfigOut, summary="Set push config (owner)")
def set_push_config(request: HttpRequest, workspace: str, payload: PushConfigIn) -> dict:
    ws = _owner_workspace_or_404(request.user, workspace)
    cfg = services.set_config(
        ws,
        audience=payload.audience,
        service_account=payload.service_account,
        watch_topic=payload.watch_topic,
    )
    return _config_out(request, cfg)


@router.get("/mailboxes/{workspace}", response=MailboxListOut, summary="List mailboxes")
def list_mailboxes(request: HttpRequest, workspace: str) -> dict:
    _workspace_or_404(request.user, workspace)
    qs = (
        InboundMailbox.objects.select_related("agent")
        .filter(agent__workspace_id=workspace)
        .order_by("address")
    )
    return {"items": [_mailbox_out(mb) for mb in qs]}


@router.post("/mailboxes/{workspace}", response=MailboxOut, summary="Register a mailbox (owner)")
def create_mailbox(request: HttpRequest, workspace: str, payload: MailboxIn) -> dict:
    _owner_workspace_or_404(request.user, workspace)
    agent = Agent.objects.filter(slug=payload.agent_slug, workspace_id=workspace).first()
    if agent is None:
        raise HttpError(422, f"no agent {payload.agent_slug!r} in this workspace")
    address = payload.address.strip().lower()
    existing = InboundMailbox.objects.filter(address__iexact=address).first()
    if existing is not None:
        # The address column is globally unique because a Gmail push carries
        # nothing but the address to disambiguate on. Say so plainly rather than
        # surfacing an IntegrityError — and never reveal WHICH workspace holds it.
        if existing.agent.workspace_id != workspace:
            raise HttpError(409, "that address is already registered elsewhere")
        raise HttpError(409, "that address is already registered")
    mb = InboundMailbox.objects.create(
        address=address, agent=agent, enabled=payload.enabled
    )
    return _mailbox_out(mb)


@router.patch("/mailboxes/{workspace}/{mailbox_id}", response=MailboxOut,
              summary="Update a mailbox (owner)")
def update_mailbox(request: HttpRequest, workspace: str, mailbox_id: int,
                   payload: MailboxPatchIn) -> dict:
    _owner_workspace_or_404(request.user, workspace)
    mb = (
        InboundMailbox.objects.select_related("agent")
        .filter(pk=mailbox_id, agent__workspace_id=workspace)
        .first()
    )
    if mb is None:
        raise HttpError(404, "no such mailbox")
    fields = []
    if payload.enabled is not None:
        mb.enabled = payload.enabled
        fields.append("enabled")
    if payload.agent_slug is not None:
        agent = Agent.objects.filter(slug=payload.agent_slug, workspace_id=workspace).first()
        if agent is None:
            raise HttpError(422, f"no agent {payload.agent_slug!r} in this workspace")
        mb.agent = agent
        fields.append("agent")
    if fields:
        mb.save(update_fields=fields)
    return _mailbox_out(mb)


@router.delete("/mailboxes/{workspace}/{mailbox_id}", response={204: None},
               summary="Remove a mailbox (owner)")
def delete_mailbox(request: HttpRequest, workspace: str, mailbox_id: int):
    _owner_workspace_or_404(request.user, workspace)
    deleted, _ = InboundMailbox.objects.filter(
        pk=mailbox_id, agent__workspace_id=workspace
    ).delete()
    if not deleted:
        raise HttpError(404, "no such mailbox")
    return 204, None


# ── the runner's half ────────────────────────────────────────────────────────


@router.get("/runner-mailboxes", response=RunnerMailboxListOut,
            summary="Mailboxes this caller should arm, and on which topic")
def runner_mailboxes(request: HttpRequest) -> dict:
    """What a runner needs to keep watches armed, served from config.

    The topic used to be hand-written into every runner's ``runner.json``, which
    meant onboarding a tenant required editing a file on each box. Serving it
    here makes the UI the single place it is set; the runner intersects this with
    the mailboxes it actually holds credentials for.
    """
    slugs = wsvc.user_workspace_slugs(request.user)
    qs = (
        InboundMailbox.objects.select_related("agent")
        .filter(agent__workspace_id__in=slugs, enabled=True)
        .order_by("address")
    )
    topics = {
        cfg.workspace_id: cfg.watch_topic
        for cfg in services.configs_for(slugs)
    }
    items = [
        {"address": mb.address, "watch_topic": topics.get(mb.agent.workspace_id, "")}
        for mb in qs
    ]
    return {"items": [i for i in items if i["watch_topic"]]}


@router.post("/watch/", response=WatchReportOut,
             summary="Report a mailbox's Gmail watch expiry")
def report_watch(request: HttpRequest, payload: WatchReportIn) -> dict:
    """Tell canopy-web when this mailbox's Gmail watch lapses.

    A Gmail ``users.watch`` expires within 7 days and Google will not renew it,
    so push dies weekly unless something re-arms it. Whatever does that reports
    the new expiry here, and the log then says ``gmail.watch.expiring`` a day out
    and ``gmail.watch.expired`` after.

    Without this the cliff is SILENT: push stops, the poll quietly takes over,
    and the only symptom is that email feels slow again — which is precisely the
    thing that took a manual investigation to notice the first time.

    Caller-authed (session or PAT), not push-verified: this is our own fleet
    reporting state, not Google calling in.
    """
    mailbox = services.resolve_mailbox(payload.address)
    if mailbox is None:
        raise HttpError(404, "no such mailbox")
    if mailbox.agent.workspace_id not in wsvc.user_workspace_slugs(request.user):
        # Same 404, not 403: whether an address is registered is not something a
        # non-member gets to learn.
        raise HttpError(404, "no such mailbox")

    services.note_watch_state(mailbox, payload.expires_at, payload.error)
    return {
        "ok": True,
        "expires_at": payload.expires_at.isoformat() if payload.expires_at else "",
    }
