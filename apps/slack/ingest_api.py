"""Django Ninja router for /api/slack-ingests — an agent reading a whole Slack channel.

Not under /api/slack/ for the same reason as /api/slack-config: that prefix is
the login middleware's allowlist for Slack's own signed webhooks.

The REST side only ASKS and READS; the permission is given in Slack, on the
message `create_channel_ingest` posts (apps/slack/ingest.py). Who may do what:

* request — the agent's own login, or an admin of the agent;
* read the transcript, and record how far it has read — the agent's own login, and nobody else;
* see an ingest's status — the agent's login, its admins, a workspace admin;
* revoke — those, plus whoever approved it (`ingest.may_revoke`).
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import ingest as ingest_svc
from .models import SlackChannelIngest
from .schemas import ChannelIngestIn, ChannelIngestOut, ChannelIngestReadMarkIn, ChannelIngestTranscriptOut

router = Router(auth=session_auth, tags=["slack"])


def _iso(d) -> str:
    return d.isoformat() if d else ""


def _out(i: SlackChannelIngest) -> dict:
    from .services import public_url

    email = lambda u: getattr(u, "email", "") or ""  # noqa: E731
    return {
        "id": str(i.pk), "agent": i.agent.slug, "workspace": i.workspace_id,
        "channel": i.channel_id, "channel_name": i.channel_name, "purpose": i.purpose,
        "follow": i.follow, "status": i.status, "requested_by_email": email(i.requested_by),
        "created_at": _iso(i.created_at), "decided_by_email": email(i.decided_by),
        "decided_by_slack_id": i.decided_by_slack_id, "decided_at": _iso(i.decided_at),
        "revoked_at": _iso(i.revoked_at), "messages": i.messages, "backfilled_at": _iso(i.backfilled_at),
        "followed_at": _iso(i.followed_at), "error": i.error,
        "transcript_url": public_url(f"/api/slack-ingests/{i.pk}/transcript"),
        "read_mark": i.read_mark, "read_at": _iso(i.read_at),
    }


def _is_agent_login(user, agent: Agent) -> bool:
    return agent.is_own_login(user)


def _may_see(user, i: SlackChannelIngest) -> bool:
    return (_is_agent_login(user, i.agent) or i.agent.is_admin(user)
            or perms.can(user, i.workspace, perms.INTEGRATIONS))


def _ingest_or_404(request: HttpRequest, ingest_id: str) -> SlackChannelIngest:
    try:
        i = (SlackChannelIngest.objects.select_related("agent", "workspace", "requested_by", "decided_by",
                                                       "installation").filter(pk=ingest_id).first())
    except Exception:  # noqa: BLE001 — not a UUID
        i = None
    if i is None or i.workspace_id not in wsvc.request_workspace_slugs(request) or not _may_see(request.user, i):
        raise HttpError(404, "no such channel ingest")
    return i


@router.post("/", response={201: ChannelIngestOut}, summary="Ask to read a whole Slack channel for an agent")
def create_channel_ingest(request: HttpRequest, payload: ChannelIngestIn):
    """Creates a PENDING ingest and posts ONE approval message in the channel.

    Nothing is read until a member of the channel who administers the agent's
    workspace (or created the channel) presses Approve there; a decline is final.
    Poll `GET /api/slack-ingests/{id}` for `status`, then read the transcript."""
    agent = Agent.objects.select_related("workspace").filter(slug=payload.agent).first()
    if agent is None or agent.workspace_id not in wsvc.request_workspace_slugs(request):
        raise HttpError(404, f"agent '{payload.agent}' not found")
    if not (_is_agent_login(request.user, agent) or agent.is_admin(request.user)):
        raise HttpError(403, f"only {agent.slug} itself or one of its admins can ask for this")
    try:
        i = ingest_svc.request(agent=agent, channel_id=payload.channel, purpose=payload.purpose,
                               follow=payload.follow, requested_by=request.user)
    except ingest_svc.IngestError as e:
        raise HttpError(e.status, str(e)) from e
    return 201, _out(i)


@router.get("/", response=list[ChannelIngestOut], summary="Channel ingests you can see")
def list_channel_ingests(request: HttpRequest, agent: str = "", status: str = "") -> list[dict]:
    rows = (SlackChannelIngest.objects.select_related("agent", "workspace", "requested_by", "decided_by")
            .filter(workspace_id__in=wsvc.request_workspace_slugs(request)).order_by("-created_at"))
    if agent:
        rows = rows.filter(agent__slug=agent)
    if status:
        rows = rows.filter(status=status)
    return [_out(i) for i in rows[:200] if _may_see(request.user, i)]


@router.get("/{ingest_id}", response=ChannelIngestOut, summary="One channel ingest: its status and progress")
def get_channel_ingest(request: HttpRequest, ingest_id: str) -> dict:
    return _out(_ingest_or_404(request, ingest_id))


@router.get("/{ingest_id}/transcript", response=ChannelIngestTranscriptOut,
            summary="The channel as Markdown — the requesting agent's own login only")
def get_channel_ingest_transcript(request: HttpRequest, ingest_id: str, after: int = 0) -> dict:
    """Readable once approved (partial while `reading`, whole when `ready`).
    Nobody but the agent itself reads it here — not its owner, not an admin:
    the approval was for that agent.

    `after` (the ingest's `read_mark`) returns only messages canopy stored since
    that read — a new reply to an old thread included, under its parent. Record
    the returned `mark` with `POST …/read-mark` once you have kept the text."""
    i = _agent_reads(request, ingest_id)
    return {"id": str(i.pk), "status": i.status, **ingest_svc.transcript(i, after=max(0, after))}


@router.post("/{ingest_id}/read-mark", response=ChannelIngestOut,
             summary="Record how far the agent has read the transcript")
def mark_channel_ingest_read(request: HttpRequest, ingest_id: str, payload: ChannelIngestReadMarkIn) -> dict:
    """Moves `read_mark` forward to `mark` (never back, never past what is stored),
    so the next `GET …/transcript?after=<read_mark>` holds only what is new."""
    return _out(ingest_svc.mark_read(_agent_reads(request, ingest_id), payload.mark))


def _agent_reads(request: HttpRequest, ingest_id: str) -> SlackChannelIngest:
    i = _ingest_or_404(request, ingest_id)
    if not _is_agent_login(request.user, i.agent):
        raise HttpError(403, f"only {i.agent.slug}'s own login can read this transcript")
    if i.status not in ingest_svc.LIVE:
        raise HttpError(409, f"nothing to read: this ingest is {i.status}")
    return i


@router.post("/{ingest_id}/revoke", response=ChannelIngestOut, summary="Stop reading and delete the transcript")
def revoke_channel_ingest(request: HttpRequest, ingest_id: str) -> dict:
    i = _ingest_or_404(request, ingest_id)
    if not ingest_svc.may_revoke(i, user=request.user):
        raise HttpError(403, "only whoever approved this, the agent, its owner or a workspace admin can revoke it")
    # A pending request is withdrawn the same way: its buttons stop working.
    return _out(ingest_svc.revoke(i, user=request.user))
