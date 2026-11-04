"""Which turns and chat sessions a request may see listed — the tenant + site filter every
turn listing shares (`list_turns`, the huddles and threads views, an agent's engagement).
A non-route module: `apps/agents` and its neighbours import it without importing
`harness.api_turns` (the router)."""
from __future__ import annotations

from django.db.models import Q
from django.http import HttpRequest

from apps.canopy_sessions import access as session_access
from apps.canopy_sessions.models import Session
from apps.embedding import delegation
from apps.workspaces import services as wsvc

from .models import Turn


def agents_own_thread_q():
    """Session turns on an AGENT's own thread (an email or Slack thread: a runner-
    origin session nobody created), which every member of its workspace may see
    ran — the same interaction tier as an agent turn. Before #734 these were agent
    turns and the whole tenant listed them; converting them to session turns hid
    them from everyone but the agent's admins, and a member's routing audit read
    the silence as "0 turns" (#1087). Their CONTENT stays gated by the session
    (`turn_access.redact`), and a person's own chat is not one of these."""
    return Q(chat_session__origin=Session.ORIGIN_RUNNER, chat_session__created_by__isnull=True,
             chat_session__agent__isnull=False)


def visible_sessions(request: HttpRequest):
    """Chat sessions the caller may read — the chat ACL, as a subquery, for
    every harness listing that would otherwise show a session turn's prompt or
    a session's messages to the whole tenant."""
    return Session.objects.filter(session_access.visible_session_q(request.user)).values("pk")


def site_turn_q(request: HttpRequest):
    """The turns an acting site may see, as a Q — or None when no site acts."""
    offered = delegation.offered_for(request)
    if offered is None:
        return None
    return Q(agent_id__in=offered) | Q(agent__isnull=True, chat_session__agent_id__in=offered)


def visible_turns_qs(request: HttpRequest, *, slugs: set[str] | None = None):
    """Every turn this caller may see LISTED — the exact tenant + site filter
    `list_turns` applies, newest first. Shared so a derived view over turns (the
    huddles API) can never show a turn `/api/harness/turns/` would not. Content
    is a separate gate: rows still go through `turn_access` before their prompt
    or transcript is read.

    The read rule applies (`wsvc.read_scope`): pinned, the pin and the caller's
    workspaces below it (a `/api/w/dimagi/` list includes connect's turns for a
    member of both); unpinned, a caller whose workspaces sit in more than one
    tree gets a 422 rather than a list mixing organisations.

    `slugs` overrides the scope outright, for a by-id view that has already
    resolved its subject's tree (`huddles.get_huddle`, `threads.get_thread`)."""
    if slugs is None:
        slugs = wsvc.request_workspace_slugs(request)
    qs = Turn.objects.select_related(
        "agent", "claimed_by", "initiator_user", "initiator_contact",
        "pinned_runner", "requested_turn_mode_by",
        # A chat turn's session and its agent are read per row by TurnOut.
        "chat_session", "chat_session__agent",
        # TurnOut.canopy_session_url (an agent/project turn's conversation).
        "canopy_session",
        # TurnOut.raised_from_task_ext_id.
        "raised_from_task",
    ).order_by("-created_at")
    # Tenant filter, split by target kind (agent / project / session) — mirrors
    # claim_next_turn's tenant_q (services.py) and turn_or_404 (this module).
    #
    # Security review 2026-07-26, hole B: this used to be one clause,
    # `Q(agent__workspace_id__in=slugs) | Q(agent__workspace_id__isnull=True)`,
    # with two independent problems. (1) The isnull leg left an unhomed
    # agent's turns (TurnOut: prompt, origin_ref, session_id) visible to ANY
    # authenticated caller — more permissive than `agent_for_read`'s fail-closed
    # gate, recreating the exact list-vs-gate drift `_runner_visibility_q`'s
    # docstring warns about (a turn the list shows, then 404s on every
    # action). (2) `agent__workspace_id` traverses a nullable FK: for a
    # PROJECT or SESSION turn (agent_id IS NULL), the LEFT JOIN makes
    # `agent__workspace_id__isnull=True` true unconditionally — so that one
    # clause also leaked every tenant's project/session turns to every
    # authenticated user, regardless of the turn's own workspace. (Confirmed
    # empirically pre-fix: an unrelated stranger's GET returned another
    # tenant's project turn.) Both close by gating each target kind on its
    # own workspace source, with no null-workspace escape hatch anywhere in
    # this list — unlike claim_next_turn, which keeps one for agent turns
    # specifically as a documented, claim-routing-only exception.
    qs = qs.filter(
        (Q(agent__isnull=False) & Q(agent__workspace_id__in=slugs))
        | (Q(agent__isnull=True) & Q(chat_session__isnull=True) & Q(workspace_id__in=slugs))
        | (Q(chat_session__isnull=False) & Q(chat_session__workspace_id__in=slugs)
           & (Q(chat_session__in=visible_sessions(request)) | agents_own_thread_q()))
    )
    site_q = site_turn_q(request)
    if site_q is not None:
        qs = qs.filter(site_q)
    return qs


def huddle_anchors(visible_qs):
    """The turns that anchor a huddle (`origin_ref.kind == "huddle"`) among `visible_qs`."""
    return visible_qs.filter(origin_ref__kind="huddle")
