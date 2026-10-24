"""Django Ninja router for /api/canopy-sessions — live chat sessions.

Session-authed + workspace-membership gated. A "send" enqueues a session Turn;
in SP2a the stub executor runs it inline (the SP2b cloud runner will claim it
async instead — no API change when that lands).
"""
from __future__ import annotations

import base64
import datetime as dt
import uuid

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404, HttpRequest
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import File, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.agents import services as agent_services
from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, ProblemError
from apps.api.pagination import clamp_limit
from apps.api.route_gates import gate
from apps.harness import initiator as who
from apps.harness import provenance, sensitivity
from apps.harness import runner_requirements as rr
from apps.realtime import groups
from apps.workspaces import services as wsvc

from . import (
    access,
    attachment_storage,
    channel_bindings,
    control,
    conversation_keys,
    doors,
    feed,
    filing,
    handoff,
    page_actions,
    page_state,
    placement,
    secrets,
    serializers,
    services,
    slash_commands,
    staleness,
    status_feed,
    transcript_ingest,
    transcript_read,
    transfer_requests,
)
from . import activity as session_activity
from . import join_requests
from . import send as cs_send
from .models import Attachment, Session
from .schemas import (
    SlashCommandOut,
    AttachmentOut,
    BackfillStateOut,
    ForkIn,
    HumanInputPageOut,
    MenuAnswerIn,
    MessageOut,
    MessagePageOut,
    NoisePurgeIn,
    NoisePurgeOut,
    NoiseReportOut,
    PageActionInvokeIn,
    PageActionOut,
    PageActionResultIn,
    PageActionsDeclareIn,
    PageActionSpec,
    PageStateIn,
    PageStateOut,
    KeyCopyIn,
    MyKeyCopiesIn,
    MyKeyCopiesOut,
    MyKeyCopyOut,
    MyVaultKeyOut,
    ParticipantAddIn,
    ParticipantRecipientOut,
    ParticipantOut,
    PlaceIn,
    ResetIn,
    ResetOut,
    ResetSummaryOut,
    RunAgentInputIn,
    RunAgentInputOut,
    SendIn,
    SendOut,
    SessionCreateIn,
    SessionDetailOut,
    SessionExportOut,
    SessionInfoOut,
    SessionMoveIn,
    SessionNotifyIn,
    SessionOut,
    SessionSearchPageOut,
    SessionSecretIn,
    SessionSecretOut,
    SessionTitleIn,
    StreamStateOut,
    TransferDecisionIn,
    HandoffOut,
    TransferAllIn,
    TransferAllOut,
    TransferIn,
    TransferOut,
    TransferRequestOut,
    JoinRequestOut,
    TurnOutMinimal,
)

#: Reserved, server-owned metadata key: the embedding app a session was created
#: through. Named once so the writer (create) and the reader (list filter)
#: cannot drift onto different spellings.
EMBED_APP_KEY = services.EMBED_APP_KEY

router = Router(auth=session_auth, tags=["chat"])


def _runner_online(runner) -> bool | None:
    """Liveness of a session's bound runner, or None when there is no binding."""
    if runner is None:
        return None
    from apps.harness.models import Runner  # lazy: framework->framework import cycle

    return runner.live_status == Runner.ONLINE


def _runner_status(runner) -> str | None:
    """The bound runner's live_status verbatim — see SessionOut.runner_status.

    Read from the same property `_runner_online` derives its bool from, so the
    two can never disagree about a runner (one says offline, the other online).
    """
    return None if runner is None else runner.live_status


def _mark_people_in(rows, viewer) -> list:
    """`rows`, each sensitive one told whether `viewer` is in it — one query for the
    page (`sensitivity.people_in`), not one per row in `_out`."""
    rows = list(rows)
    sensitive = [s for s in rows if sensitivity.is_sensitive(s)]
    if sensitive:
        mine = sensitivity.people_in(sensitive, viewer)
        for s in sensitive:
            s._you_are_in_it = s.pk in mine
    return rows


def _out(session: Session, *, reply: bool = False, viewer=None) -> dict:
    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    waiting_on_you = serializers.pending_menu(session) is not None
    last_reply, agent_spoke_last = transcript_read.last_reply_of(session, binding) if reply else ("", False)
    # The reply is whole (untrimmed), so carry it only where the feed renders it:
    # sessions where it is the person's turn. That is what bounds the payload.
    if not (agent_spoke_last or waiting_on_you):
        last_reply = ""
    runner = binding.runner if (binding and binding.runner_id) else None
    running = transcript_read.is_session_running(binding)
    turn_mode = getattr(session, "_turn_mode", None) or ""
    turn_origin = getattr(session, "_turn_origin", None) or ""
    # The supervisor feed's verdict for THIS caller — the one rule, which the
    # pushes ask too (canopy_sessions.feed). Only when the feed asked (`reply`).
    feed_status = (
        feed.status(
            viewer, session, waiting_on_you=waiting_on_you, agent_spoke_last=agent_spoke_last,
            running=running, turn_mode=turn_mode, turn_origin=turn_origin,
            turn_open=bool(getattr(session, "_turn_open", False)),
        )
        if reply and viewer is not None
        else ""
    )
    # The name a human recognises for a runner-bound session is the emdash
    # task (what they see in emdash), not a thread_key hash a fallback title
    # may have captured. Web chats keep their own title. Web-origin sessions
    # also prefer their own title once set (e.g. the server-side auto-titler)
    # over a bound session_key, since a web chat's binding is an execution
    # detail, not the identity the human gave the conversation.
    #
    # A runner session keeps its own title too, unless that title is still the
    # thread_key fallback the rule above exists to hide. The key is only a better
    # name when it IS a name: a laptop's emdash task is, but a cloud runner's key
    # is a Claude session UUID, so a cloud agent turn's session ("Daily turn",
    # set by record-session) showed as a UUID while its real title sat unread.
    is_fallback = (
        session.origin == Session.ORIGIN_RUNNER
        and binding is not None
        and session.title == binding.thread_key
    )
    prefer_own = bool(session.title) and not is_fallback
    sensitive = sensitivity.is_sensitive(session)
    return {
        "id": session.id,
        "agent_slug": session.agent.slug if session.agent_id else None,
        "project": session.project,
        "workspace": session.workspace_id,
        "title": (
            session.title
            if prefer_own
            else ((binding.session_key if (binding and binding.session_key) else "") or session.title)
        ),
        "status": session.status,
        "opening": transcript_read.opening_of(session),
        "created_at": session.created_at,
        # When it last DID something (binding > newest message > created).
        "last_activity_at": transcript_read.last_activity_at(session, binding),
        # --- liveness (Plan 4): one shape, computed from the binding ---
        "origin": session.origin,
        "running": running,
        "runner_name": runner.name if runner else None,
        "runner_id": runner.id if runner else None,
        # The macOS user@host the live session belongs to — two laptop logins are
        # two runners told apart by this, so it names WHICH login on a shared box.
        "runner_host": binding.host if binding else "",
        "runner_location": runner.location if runner else None,
        # The emdash project its worktree lives under: the binding's reported one,
        # else the session's own (repo for a project chat, agent slug for an agent
        # chat). What /ide groups by — `project` is blank on every agent chat.
        "emdash_project": (binding.emdash_project if binding else "") or session.emdash_project,
        # See SessionOut.runner_online: an embedder's delegated user cannot list
        # runners, so the session payload is where they learn their bound runner
        # went away. None when unbound — nothing to be offline.
        "runner_online": _runner_online(runner),
        "runner_status": _runner_status(runner),
        "runner_signed_out": bool(runner and runner.signed_out),
        "session_key": binding.session_key if binding else "",
        # Is this session blocked on a human? A bool on the LIST (the menu
        # itself rides the detail read) — a list carrying every session's full
        # dialog would pay for N sets of options to render one badge each. It
        # answers the thing the list could not: a waiting agent and an idle one
        # look identical, which is why spark read as "the session stopped".
        "waiting_on_you": waiting_on_you,
        "close_error": binding.close_error if binding else "",
        "close_error_at": binding.close_error_at if binding else None,
        # Which of close_session's two branches a close takes — what a delete
        # confirm must name (an emdash task, or only this session).
        "runner_reported": control.is_runner_reported(binding),
        # Is a requested full-history ship still outstanding? EXACT, where the
        # client previously had to guess: it slept a flat 1200 ms and read once,
        # which on labs was 13 s early (rows landed at t+14.6s), so the button
        # reported success having changed nothing. Polling until rows GROW is no
        # better on its own — an already-complete session never grows, so the
        # client would spin for its whole timeout on the common case. The runner
        # clears this flag when it ships the final chunk, so it answers "is
        # anything still coming?" directly instead of inferring it from an
        # absence.
        "backfill_pending": bool(binding and binding.backfill_requested),
        "notify_every_completion": session.notify_every_completion,
        "sensitive": sensitive,
        # A list reads who is in which once per page (`_mark_people_in`).
        "you_are_in_it": ((session._you_are_in_it if hasattr(session, "_you_are_in_it")
                           else sensitivity.is_conversation_person(session, user=viewer))
                          if sensitive else None),
        "started_by_name": _started_by_name(session),
        "runner_requirements": sorted(rr.requirements_of_session(session)),
        "last_reply": last_reply,
        "agent_spoke_last": agent_spoke_last,
        "turn_mode": turn_mode,
        "turn_origin": turn_origin,
        "feed_status": feed_status,
        "created_by": session.created_by.email if session.created_by_id else None,
        "provenance": provenance.public(session.provenance),
        "parent_turn_id": session.parent_turn_id,
        "parent_session_id": session.parent_session_id,
        "parent_task": session.parent_task,
        "parent_claude_session": session.parent_claude_session,
        "activity": session_activity.public(session.activity),
        # Where else the conversation is had (a Slack thread), from each channel's
        # own reader — canopy_sessions knows no channel (channel_bindings.py).
        **channel_bindings.of(session),
        # A private conversation is listed by what it is, never by what it says:
        # none of these is ever written with its words (`sensitivity.seal`), and a
        # list must not start reading them if that ever changes.
        **({"title": "", "opening": "", "last_reply": ""} if sensitive else {}),
    }


def _started_by_name(session: Session) -> str:
    """Who started it: a member's name, "a visitor" for a contact's; "" for a
    runner's or an agent's own. A name, never what they said."""
    if session.created_by_id:
        return control.person_name(session.created_by)
    return "a visitor" if session.contact_id else ""


def _visible_slugs(request: HttpRequest, *, listing: bool = False, bulk_write: bool = False) -> set[str]:
    """The tenants a session read may reach. A by-id read (the default) takes
    every membership, or the pin; a LIST or search (`listing=True`) takes the
    read rule — a caller whose workspaces sit in more than one tenant tree must
    pin one, or gets a 422 rather than two organisations' sessions; a pinned list
    covers the caller's workspaces below the pin. A bulk reset (`bulk_write=True`)
    takes the WRITE scope: pinned, exactly that workspace, never the ones below."""
    if bulk_write:
        return wsvc.request_write_slugs(request)
    if listing:
        return wsvc.request_workspace_slugs(request)
    return wsvc.request_member_slugs(request)


def _site_scoped(request: HttpRequest, qs):
    """`site ∩ user`: when a connected site is acting, only its own agents'
    sessions exist (apps/embedding/delegation.py). No-op for the person themself."""
    from apps.embedding import delegation

    offered = delegation.offered_for(request)
    return qs if offered is None else qs.filter(agent_id__in=offered)


def readable_sessions(request: HttpRequest):
    """The sessions a by-id request may name: `access.readable_sessions` over the
    caller's tenants, narrowed to the acting site's agents. Every route that
    takes a session by id reads this — the Slack routes too (apps/slack/session_api.py)."""
    return _site_scoped(request, access.readable_sessions(request.user,
                                                          workspace_slugs=_visible_slugs(request)))


def _session_or_404(request: HttpRequest, session_id: uuid.UUID, *, write: bool = False) -> Session:
    # One authority (`access`): the tenant, then who within it — the same rule
    # the list, the chat socket and attachments apply. `write=True` also needs
    # an owner/editor role; a viewer gets 403, since they can already see it.
    session = get_object_or_404(
        readable_sessions(request)
        .select_related("agent", "created_by", "runner_binding", "runner_binding__runner")
        .annotate(_last_msg_at=transcript_read.last_message_at()),
        pk=session_id,
    )
    if write and not access.can_write(request.user, session):
        raise HttpError(403, "you can read this session but not act in it")
    return session


def _set_status(request: HttpRequest, session_id: uuid.UUID, status: str) -> dict:
    session = _session_or_404(request, session_id, write=True)   # membership gate: non-member -> 404
    if session.status != status:
        session.status = status
        session.save(update_fields=["status", "updated_at"])
    return _out(session)


@router.post("/", response=SessionOut, summary="Create a chat session")
# wsvc.current_workspace; any member may start a chat
@gate(
    "member",
    kind="conversation",
    viewer_write=(
        "starting a chat with an agent is the interaction tier a viewer holds"
    ),
)
def create_session(request: HttpRequest, payload: SessionCreateIn):
    """Create a chat session. Started from inside a canopy session? Pass
    `parent={turn, session}` (the caller-context hook prints both ids) so the new
    session records where it came from and includes that conversation's people."""
    if payload.agent_slug and payload.project:
        raise HttpError(422, "a session targets an agent or a project, not both")
    try:
        workspace = wsvc.current_workspace(request.user, getattr(request, "workspace_slug", None))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    agent = None
    if payload.agent_slug:
        agent = agent_services.get_agent(payload.agent_slug)
        if agent is None or agent.workspace_id != workspace.slug:
            raise HttpError(404, f"agent '{payload.agent_slug}' not found in this workspace")
    # A site acting for its visitor may open a chat only with an agent it
    # offers — and never an agent-less (project) session, which no site offers.
    from apps.embedding import delegation

    offered = delegation.offered_for(request)
    if offered is not None and (agent is None or agent.pk not in offered):
        raise HttpError(404, f"agent '{payload.agent_slug}' not found in this workspace")
    try:
        metadata = services.host_metadata(payload.metadata)
    except ValueError as exc:
        raise HttpError(422, str(exc))
    # `embed_app` and the site's runner requirements are SERVER-OWNED, from the
    # delegated token (a client-supplied value was discarded by host_metadata on
    # every auth path). `origin_key` is deliberately NOT touched: it is a finer,
    # host-chosen scope (ace-web derives one per ace workspace from a
    # membership-checked path). See tests/test_embed_session_provenance.py.
    services.stamp_host_metadata(metadata, request)
    # A sensitive site's conversation is sealed to its people's vault keys: someone
    # without one is refused HERE, before an empty conversation exists, in the
    # sentence the panel shows them (`conversation_keys.CannotHold`).
    if sensitivity.is_sensitive(Session(metadata=metadata)):
        _holder_or_422(user=request.user)
    # A key pair made in the starter's browser: if this conversation is (or becomes)
    # sensitive its words are sealed to it, and canopy-web never sees its private half.
    offered = None
    if payload.conversation_key is not None:
        try:
            offered = conversation_keys.check_offer(request.user, payload.conversation_key.model_dump(mode="json"))
        except conversation_keys.WrapRefused as exc:
            raise HttpError(422, str(exc)) from None
    if payload.runner_id:
        from apps.harness.models import Runner

        # The stash is consumed on the first send WITHOUT asking who may choose
        # (placement._resolve_placement), so the choice is decided here, by the one
        # placement rule every door asks (`placement.may_place`): for a chat with
        # an agent its admins may pin any box, anyone else only a box they
        # administer; for a project chat, only someone who administers the box.
        # An id that names no runner is left as before — the first send ignores
        # it — and the message names no runner: the id may be one the caller
        # cannot see.
        requested = Runner.objects.filter(pk=payload.runner_id).first()
        draft = Session(agent=agent, project=payload.project)
        if requested is not None and not placement.may_place(request.user, draft, requested):
            if agent is not None:
                raise HttpError(
                    403,
                    f"running {agent.slug} on a chosen runner is for the agent's owner or "
                    "admins, or for someone who administers that runner; start the chat "
                    "without a runner and the agent's routing places it",
                )
            raise HttpError(403, placement.PROJECT_CHAT_PLACEMENT_REFUSED.format(
                runner="a chosen runner"))
        # Directed new chat: stashed for the session's first send to pin onto
        # (as long as it's still unbound at that point) — see send.send_message.
        metadata["requested_runner_id"] = str(payload.runner_id)
    session = services.create_session(
        workspace=workspace, created_by=request.user, agent=agent,
        project=payload.project, title=payload.title, metadata=metadata,
        parent=payload.parent, conversation_key=offered,
    )
    return _out(session)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """A query-string datetime, read as UTC when it names no zone."""
    if value is None or timezone.is_aware(value):
        return value
    return timezone.make_aware(value, dt.timezone.utc)


def _filtered_sessions(
    request: HttpRequest, *, state: str, source: str = "", opp_slug: str = "",
    opp_run_id: str = "", origin_key: str = "", embed_app: str = "",
    resource: str = "", page_path: str = "", session_key: str = "",
    q: str = "", repo: str = "", branch: str = "", pr: str = "",
    since: dt.datetime | None = None,
    until: dt.datetime | None = None, runner: str = "",
):
    """Every session the caller may see, narrowed by the list filters, with
    `_last_msg_at`, `_activity` (= `last_activity_at`, computed in SQL) and
    `_opening` annotated. Unordered — each route orders it its own way. The
    list and the search route share it so a filter (and, above all, the
    delegated-app scoping) cannot exist on one and not the other."""
    from django.db.models import Q

    if state not in ("active", "archived", "all"):
        raise HttpError(422, "state must be one of: active, archived, all")

    slugs = _visible_slugs(request, listing=True)
    rows = (
        # The same authority every other session surface reads — see
        # apps/canopy_sessions/access.py — narrowed to the acting site's agents.
        _site_scoped(request, access.readable_sessions(request.user, workspace_slugs=slugs))
        .select_related("agent", "created_by", "runner_binding", "runner_binding__runner")
    )
    # Embedder filters (Task 9): an embedder (e.g. ace-web) narrows the shared
    # session list to the sessions it cares about, keyed on the opaque
    # `metadata` bag a session carries (never interpreted elsewhere in this
    # app). Empty string = no filter, so the default call is unaffected.
    #
    # `origin_key` is the generic one: an embedder whose own product is
    # multi-tenant stamps ITS tenant into metadata.origin_key at create time and
    # filters on it here, so two of its tenants sharing one canopy workspace do
    # not see each other's sessions in the list. Deliberately opaque — canopy
    # never parses it. (Note the residual: this scopes the LIST; canopy's own
    # tenancy still lets any member of the canopy workspace open a session by id.
    # An embedder that needs hard isolation maps its tenants onto separate canopy
    # workspaces instead.)
    rows = services.host_filters(rows, source=source, origin_key=origin_key, opp_slug=opp_slug,
                                 opp_run_id=opp_run_id, resource=resource, page_path=page_path)
    # Which embedding app created the session — stamped server-side from the
    # delegated token at create (see create_session), so unlike `origin_key`
    # this one cannot have been chosen by whoever wrote the row.
    #
    # For a DELEGATED caller it is FORCED, not a filter. `embed_app` was
    # caller-supplied on read, so a token issued to connect-labs could pass
    # `embed_app=canopy-web` and enumerate that user's conversations from
    # another host — same user, but across the host boundary, titles included.
    # `test_embed_session_provenance`'s own docstring states the goal as "my
    # previous conversations HERE without being able to ask for someone else's
    # conversations THERE", and the second half was only enforced at create.
    #
    # A browser/PAT caller is unchanged: it is the human themself, not an app
    # acting for them, so an unfiltered list stays unfiltered and canopy's own
    # UI does not quietly narrow.
    acting_app = getattr(request, "delegated_app", None)
    if acting_app is not None:
        rows = rows.filter(**{f"metadata__{EMBED_APP_KEY}": acting_app.name})
    elif embed_app:
        rows = rows.filter(**{f"metadata__{EMBED_APP_KEY}": embed_app})

    # The name an Open Sessions card shows for a runner session (an emdash task
    # name, or a cloud session's Claude UUID) — so it can be looked up by what
    # a person reads off the card, not only by the session id they never see.
    if session_key:
        rows = rows.filter(runner_binding__session_key=session_key)
    # The box it is on, by id or name — what to move off a runner (`/transfer-all`).
    if runner:
        box = transfer_requests.resolve_runner(runner)
        rows = rows.filter(runner_binding__runner=box) if box is not None else rows.none()
    # Free text over what a person reads off a card: the title, or the runner's
    # session_key (an emdash task name, or a cloud session's Claude UUID).
    if q:
        rows = rows.filter(Q(title__icontains=q) | Q(runner_binding__session_key__icontains=q))
    # The repo a session ran in — or worked on. An agentless repo chat names it
    # in `project`, a runner session's binding carries the emdash project its
    # worktree lives under (`RunnerBinding.emdash_project`), and Session.activity
    # (folded at ingest, activity.py) holds every repo the session cd'd into,
    # edited, pushed to or opened a PR on — so an agent session that built
    # connect-labs matches `repo=connect-labs` too. `repo` takes a bare name or
    # owner/name; `branch` any branch the transcript saw; `pr` a number,
    # owner/name#N or a PR URL.
    if repo:
        name = repo.strip().lower().removesuffix(".git").split("/")[-1]
        rows = rows.filter(Q(project__iexact=name) | Q(runner_binding__emdash_project__iexact=name)
                           | session_activity.filter_q(repo=repo))
    if branch or pr:
        try:
            rows = rows.filter(session_activity.filter_q(branch=branch, pr=pr))
        except ValueError as exc:
            raise HttpError(422, str(exc)) from exc
    # `_activity` is transcript_read.last_activity_at's rule in SQL, so a window and a
    # cursor can be applied to it.
    rows = transcript_read.with_activity(transcript_read.with_opening(rows)).distinct()
    if since is not None:
        rows = rows.filter(_activity__gte=_aware(since))
    if until is not None:
        rows = rows.filter(_activity__lt=_aware(until))
    if state == "active":
        rows = staleness.active(rows)
    elif state == "archived":
        rows = rows.filter(Q(status=Session.ARCHIVED) | staleness.unseen_q())
    return rows


@router.get("/", response=list[SessionOut], summary="List sessions (web + runner-discovered)")
@gate("session-acl")  # readable_sessions
def list_sessions(
    request: HttpRequest, state: str = "active", limit: int = 200,
    source: str = "", opp_slug: str = "", opp_run_id: str = "",
    origin_key: str = "", embed_app: str = "",
    resource: str = "", page_path: str = "", reply: bool = False,
    session_key: str = "", q: str = "", repo: str = "", branch: str = "", pr: str = "",
    since: dt.datetime | None = None, until: dt.datetime | None = None, runner: str = "",
):
    """The sessions you can see: waiting on you first, then running, then most
    recent activity — at most `limit` (≤ 500) of them, with no paging.

    Filters: `q` (title or session_key contains), `repo` (the repo it ran in or
    worked on — a name or owner/name), `branch` (any branch its transcript saw),
    `pr` (a PR it created or asked to merge — N, owner/name#N or a URL),
    `since` / `until` (last activity in [since, until)), `runner` (the runner it
    is on, by id or name — `?runner=<name>&state=active` is every open session on
    a box, what `POST /transfer-all` moves). To reach EVERY session
    rather than the newest 500, walk `GET /api/canopy-sessions/search` instead.
    """
    # The ONE unified list (Plan 4): every session the caller can see in their
    # workspaces — their own web sessions UNION any session that has a
    # RunnerBinding (runner-discovered or live). Deduped, running-first, then
    # newest. Replaces the creator-only list + the harness OpenSessions projection.
    #
    # `state` gives that list an END. Two rules combine into "archived":
    #   - WRITTEN: status == archived (the runner saw the emdash task archived, or
    #     a human called /archive). Durable.
    #   - DERIVED: a RUNNER-origin session whose binding has not been seen within
    #     SESSION_STALE_AFTER. Computed here, never stored, so it reverses itself
    #     the moment the task is reported again. Web sessions are exempt — they
    #     have no runner to be seen by, so only an explicit archive ends them.
    rows = _filtered_sessions(
        request, state=state, source=source, opp_slug=opp_slug, opp_run_id=opp_run_id,
        origin_key=origin_key, embed_app=embed_app, resource=resource,
        page_path=page_path, session_key=session_key, q=q, repo=repo,
        branch=branch, pr=pr, since=since, until=until, runner=runner,
    ).order_by("-created_at")

    # Every row says which mode drove it — the list shows it on each card, so an
    # auto session that just ran and a manual one waiting on you can be told apart.
    rows = transcript_read.with_driving_turn(rows)
    if reply:
        rows = transcript_read.with_open_turn(transcript_read.with_last_reply(rows))
    out = [_out(s, reply=reply, viewer=request.user) for s in _mark_people_in(rows, request.user)]
    # Waiting first, then running, then genuinely-most-recent. Sorting by
    # created_at made a dead repo and a live one interleave arbitrarily (both
    # "created" in the same report sweep); last_activity_at is the real signal.
    # The client can re-group by project — this is the default order.
    #
    # `waiting_on_you` outranks both because activity ordering actively BURIES
    # it: a session stops writing the moment it asks, so the longer somebody has
    # been kept waiting the further down it sinks, and the row you can actually
    # do something about ends up below a dozen you cannot. Same trap the runner
    # side avoids by reading the question for every session rather than the top
    # K — this is that trap one layer up.
    out.sort(key=_list_order)
    # Clamp AFTER the sort, never as a queryset slice: the queryset is ordered by
    # -created_at, so slicing it could drop the running session this sort exists to
    # float. `state=active` already bounds the set; this is a payload backstop.
    return out[: clamp_limit(limit)]


def _list_order(r):
    return (not r["waiting_on_you"], not r["running"], -(r["last_activity_at"].timestamp()))


#: User-level views (`/api/me/…`), mounted at `/me`.
me_router = Router(auth=session_auth, tags=["chat"])


@me_router.get("/sessions/", response=list[SessionOut], summary="Your sessions, across every workspace you are in")
@gate("session-acl")  # the same list per organisation (wsvc.each_organisation)
def list_my_sessions(request: HttpRequest, state: str = "active", limit: int = 200, reply: bool = False, q: str = ""):
    """The session list (`GET /api/canopy-sessions/`) as a user-level view:
    each organisation you are in is read on its own and every row carries its
    workspace, so a page off any one workspace (Supervisor, the IDE) needs one
    call. Same order — waiting on you, then running, then newest — and the same
    `state`, `reply`, `q` and `limit` (≤ 500)."""
    out = wsvc.each_organisation(request, lambda: list_sessions(request, state=state, limit=limit, reply=reply, q=q))
    out.sort(key=_list_order)
    return out[: clamp_limit(limit)]


def _encode_cursor(activity: dt.datetime, session_id: uuid.UUID) -> str:
    raw = f"{activity.isoformat()}|{session_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[dt.datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        when, sid = raw.split("|", 1)
        return _aware(dt.datetime.fromisoformat(when)), uuid.UUID(sid)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HttpError(422, "cursor is not one this endpoint issued; start again without it") from exc


@router.get("/search", response=SessionSearchPageOut, summary="Page through every session (cursor)")
@gate("session-acl")  # readable_sessions, via the list's _filtered_sessions
def search_sessions(
    request: HttpRequest, cursor: str = "", limit: int = 100, state: str = "all",
    q: str = "", repo: str = "", since: dt.datetime | None = None,
    until: dt.datetime | None = None, source: str = "", origin_key: str = "",
    embed_app: str = "", session_key: str = "", branch: str = "", pr: str = "",
):
    """Every session you can see, newest last activity first, `limit` (≤ 500)
    at a time — the whole history, not just the newest 500.

    Pass the response's `next_cursor` back as `cursor` (with the same filters)
    for the next page; it is null when the walk is done. Ordered by
    (last activity, id), descending, so the order is total and stable. `state`
    defaults to `all`. Filters: `q` (title or session_key contains), `repo` (the
    repo it ran in or worked on), `branch`, `pr` (as on the list), `since` /
    `until` (last activity in [since, until), ISO-8601, UTC when no zone is given).

    A session that does something mid-walk moves to the front and can be
    missed by a walk already past it; pass `until` = the time the walk started
    to freeze the set. Same visibility rule as the list and `GET /{id}`.
    """
    from django.db.models import Q

    rows = _filtered_sessions(
        request, state=state, source=source, origin_key=origin_key,
        embed_app=embed_app, session_key=session_key, q=q, repo=repo,
        branch=branch, pr=pr, since=since, until=until,
    )
    if cursor:
        at, sid = _decode_cursor(cursor)
        rows = rows.filter(Q(_activity__lt=at) | Q(_activity=at, id__lt=sid))
    limit = clamp_limit(limit)
    rows = transcript_read.with_driving_turn(rows.order_by("-_activity", "-id"))
    page = list(rows[: limit + 1])
    more = len(page) > limit
    page = page[:limit]
    return {
        "sessions": [_out(s, viewer=request.user) for s in _mark_people_in(page, request.user)],
        "next_cursor": _encode_cursor(page[-1]._activity, page[-1].id) if more else None,
    }


# Declared BEFORE /{session_id}: Django resolves in declaration order and
# "reset" would otherwise match the session-id pattern and 405 on its GET-only
# view. Any future collection-level route belongs above here too.
@router.post("/reset", response=ResetSummaryOut, summary="Reset every visible session")
@gate("session-acl", kind="conversation")  # readable rows filtered to can_write
def reset_sessions(request: HttpRequest, payload: ResetIn):
    """Bulk reset, scoped to the workspaces the caller can see (and to the pinned
    one on a tenant route). Use `dry_run` to see what would happen first.

    `prune_ghosts` additionally DELETES runner-discovered sessions that have no
    binding at all: they can neither be shown nor rebuilt, and the next session
    report re-creates any whose task is still open. Chats a human started are
    never pruned.
    """
    # Only sessions you may act in. Scoping by workspace alone let a co-tenant
    # reset — and with `prune_ghosts`, delete — conversations they cannot read.
    readable = _site_scoped(
        request, access.readable_sessions(request.user, workspace_slugs=_visible_slugs(request, bulk_write=True)))
    rows = [
        s for s in readable.select_related("created_by", "runner_binding", "runner_binding__runner")
        .order_by("created_at")
        if access.can_write(request.user, s)
    ]
    return services.reset_sessions(
        rows, prune_ghosts=payload.prune_ghosts, dry_run=payload.dry_run
    )


@router.get("/noise", response=NoiseReportOut, summary="Find harness output stored as your messages")
@gate("session-acl")  # rows of sessions the caller can read
def audit_session_noise(request: HttpRequest, session_id: str | None = None, sample: int = 5):
    """Report harness output stored as USER messages in your sessions.

    Claude Code writes task notifications, system reminders, skill bodies and
    interrupt markers as `type: "user"` transcript records, so they can land in
    the store looking like something a person typed. Ingest filters them now;
    this finds rows written before a given prefix was recognised.

    `session_id` restricts it to one session; omitted, it sweeps every session
    you can read — and then a caller whose workspaces sit in more than one
    tenant tree must name the workspace. `sample` (clamped to 50) is how many
    example rows to include. Read-only: `purge_session_noise` deletes.
    """
    from . import maintenance

    slugs = _visible_slugs(request) if session_id else _visible_slugs(request, listing=True)
    return maintenance.audit_noise(workspace_slugs=slugs, session_id=session_id,
                                   sample=sample, user=request.user)


@router.post("/noise/purge", response=NoisePurgeOut,
             summary="Delete harness output stored as your messages")
@gate("session-acl", kind="conversation")  # write; readable sessions, write scope when applied
def purge_session_noise(request: HttpRequest, payload: NoisePurgeIn):
    """Delete the rows `audit_session_noise` reports.

    DRY RUN BY DEFAULT: without `apply: true` this reports what WOULD go and
    deletes nothing. Deleting is irreversible and the match is only as good as
    the current prefix list, so the intended sequence is audit, read the count,
    then apply. Only sessions you can read; a sweep that deletes covers exactly
    the workspace named (never the ones below it).
    """
    from . import maintenance

    if payload.session_id:
        slugs = _visible_slugs(request)
    else:
        slugs = _visible_slugs(request, bulk_write=True) if payload.apply \
            else _visible_slugs(request, listing=True)
    return maintenance.purge_noise(workspace_slugs=slugs, session_id=payload.session_id,
                                   sample=payload.sample, apply=payload.apply, user=request.user)


# ---- transfer requests: a move onto someone else's runner, awaiting their yes ----
# Declared above the `/{session_id}` routes so `/transfer-requests` is never read
# as a session id.

def _transfer_request_out(req, transfer=None) -> dict:
    from . import transfer_requests

    def email(u):
        return getattr(u, "email", "") or ""

    out = {
        "id": req.id, "session_id": req.session_id,
        "session_title": req.session.title or "",
        "agent_slug": req.session.agent.slug if req.session.agent_id else "",
        "to_runner": req.to_runner.name, "to_runner_id": req.to_runner_id,
        "from_runner": req.from_runner.name if req.from_runner_id else "",
        "requested_by": email(req.requested_by), "brief": req.brief, "status": req.status,
        "decided_by": email(req.decided_by), "decided_at": req.decided_at, "note": req.note,
        "created_at": req.created_at,
        "approvers": sorted(email(u) for u in transfer_requests.approvers(req.to_runner)),
        "transfer": None,
    }
    if transfer is not None:
        binding, turn = transfer
        out["transfer"] = {
            "session_id": str(req.session_id),
            "runner": binding.runner.name if binding.runner_id else "",
            "transferred_from": binding.transferred_from.name if binding.transferred_from_id else "",
            "index_offset": binding.index_offset, "turn_id": str(turn.id) if turn else "",
        }
    return out


def _transfer_request_or_404(request: HttpRequest, request_id: uuid.UUID):
    from . import transfer_requests

    req = transfer_requests.visible_to(request.user).filter(pk=request_id).first()
    if req is None:
        raise HttpError(404, "transfer request not found")
    return req


@router.get("/my-vault-key", response=MyVaultKeyOut, summary="My vault public key, to wrap a new chat's key to")
@gate("self")
def my_vault_key(request: HttpRequest):
    """Your vault key's PUBLIC half and its thumbprint, for your browser to wrap a new
    conversation's key to before it starts one (`SessionCreateIn.conversation_key`) —
    also from the embedded panel, which has no browser session to read
    `/api/vault/keyset` with. With it, to your own browser only, the keyset that opens
    it: a browser that has not opened your vault key before derives its public half
    from it before trusting this one. 404 when you have none."""
    found = conversation_keys.recipient(request.user)
    if found is None:
        raise HttpError(404, "you have no vault key")
    # The seals go only to the person's browser, as `sealed_key.keyset` does.
    keyset = (conversation_keys.keyset_for_browser(request.user)
              if sensitivity.request_is_chat_surface(request)
              and sensitivity.request_is_panel_browser(request) else None)
    return {"user_id": str(found["user_id"]), "public_jwk": found["public_jwk"], "jkt": found["jkt"],
            "keyset": keyset}


def _own_browser_or_403(request: HttpRequest) -> None:
    # Your conversation keys are re-wrapped where your vault key is set up: canopy's own
    # signed-in page. Not MCP, a token, a contact, or a site's delegated token.
    if (getattr(request, "delegated_app", None) or getattr(request, "contact", None)
            or not sensitivity.request_is_chat_surface(request)):
        raise HttpError(403, "your conversation keys are re-wrapped from canopy in your browser")


@router.get("/my-keys", response=list[MyKeyCopyOut],
            summary="The private chats whose key I hold a copy of")
@gate("self")
def my_key_copies(request: HttpRequest):
    """Before you replace your vault key: every sensitive conversation you hold a copy
    of the key to (a wrap only your vault key opens — never what was said), so your
    browser can re-wrap each to the new key while the old one is open, and name them
    to you. Store the re-wrapped copies with `PUT /my-keys` once the new key is set.
    Canopy's own signed-in page only."""
    _own_browser_or_403(request)
    return conversation_keys.my_copies(request.user)


@router.put("/my-keys", response=MyKeyCopiesOut,
            summary="Store my private chats' keys, re-wrapped to my new vault key")
@gate("self", setting=True)
def put_my_key_copies(request: HttpRequest, payload: MyKeyCopiesIn):
    """After `PUT /api/vault/keyset` with `replace`: your copies of your conversations'
    keys, each re-wrapped in your browser to your NEW vault key. Replaces only copies
    you already hold, and only to your current vault key; 422 (storing none) when one
    is not."""
    _own_browser_or_403(request)
    try:
        n = conversation_keys.replace_my_copies(request.user, [
            {"session_id": c.session_id, "wrap": c.wrap.model_dump(), "recipient_jkt": c.recipient_jkt}
            for c in payload.copies])
    except conversation_keys.WrapRefused as exc:
        raise HttpError(422, str(exc)) from None
    return {"stored": n}


@router.get("/transfer-requests", response=list[TransferRequestOut],
            summary="Transfer requests waiting on you, or that you made")
@gate("self", "runner-admin")  # ones you asked for, or for boxes you administer
def list_transfer_requests(request: HttpRequest, status: str = "pending"):
    """Requests to move a session onto a runner you administer (yours to approve
    or decline), plus the ones you asked for. `status` filters (default
    `pending`; `all` for every state)."""
    from . import transfer_requests

    qs = transfer_requests.visible_to(request.user)
    rows = list(qs[:200])
    for req in rows:
        transfer_requests._expire_if_stale(req)
    if status != "all":
        rows = [r for r in rows if r.status == status]
    return [_transfer_request_out(r) for r in rows]


@router.post("/transfer-requests/{request_id}/approve", response=TransferRequestOut,
             summary="Approve a transfer onto your runner (and carry it out)")
@gate("runner-admin", kind="conversation")  # the target box's admins, re-checked at decision
def approve_transfer_request(request: HttpRequest, request_id: uuid.UUID):
    """Approve moving the session onto your runner; the move happens now and
    `transfer` reports it LAUNCHED. Only an administrator of the target runner may
    approve, checked at this moment. 409 while a turn is still executing on the
    session — the request stays pending; stop the session and approve again."""
    from . import transfer_requests

    req = _transfer_request_or_404(request, request_id)
    try:
        transfer = transfer_requests.approve(req=req, user=request.user,
                                    initiator=who.for_request(request, via="transfer"))
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except RuntimeError as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return _transfer_request_out(req, transfer)


@router.post("/transfer-requests/{request_id}/decline", response=TransferRequestOut,
             summary="Decline a transfer onto your runner")
@gate("runner-admin", kind="conversation")
def decline_transfer_request(request: HttpRequest, request_id: uuid.UUID, payload: TransferDecisionIn):
    """Decline; the session stays where it is. `note` is shown to the requester."""
    from . import transfer_requests

    req = _transfer_request_or_404(request, request_id)
    try:
        transfer_requests.decline(req=req, user=request.user, note=payload.note)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return _transfer_request_out(req)


@router.post("/transfer-requests/{request_id}/cancel", response=TransferRequestOut,
             summary="Withdraw a transfer request you made")
@gate("self", kind="conversation")  # only whoever asked
def cancel_transfer_request(request: HttpRequest, request_id: uuid.UUID):
    from . import transfer_requests

    req = _transfer_request_or_404(request, request_id)
    try:
        transfer_requests.cancel(req=req, user=request.user)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return _transfer_request_out(req)


# ---- moving every session off one runner at once ----
# Declared above the `/{session_id}` routes so `/transfer-all` is never read as a
# session id.

@router.post("/transfer-all", response=TransferAllOut,
             summary="Move every open session on one runner to another")
@gate("session-acl", kind="conversation")  # each session: access.can_write, then the transfer's own rules
def transfer_all(request: HttpRequest, payload: TransferAllIn):
    """Move every open session you can act in from `from_runner` to `to_runner`,
    each with its work (see `transfer`), and say what happened to each one.

    The way to empty a box — an account out of Claude tokens, a laptop going
    away, load moving to the cloud. `dry_run` lists what would move first. Each
    session is its own transfer, so one that cannot move (a turn executing, a
    session the target cannot run) is reported `refused` with the reason and the
    rest still move. Discovery alone is `GET /api/canopy-sessions/?runner=<name>`.
    """
    source = transfer_requests.resolve_runner(payload.from_runner)
    target = transfer_requests.resolve_runner(payload.to_runner)
    if source is None:
        raise HttpError(404, f"no runner {payload.from_runner!r}")
    if target is None:
        raise HttpError(404, f"no runner {payload.to_runner!r}")
    rows = (_filtered_sessions(request, state="active")
            .filter(runner_binding__runner=source).order_by("created_at"))
    if payload.session_ids:
        rows = rows.filter(pk__in=[_uuid_or_422(x) for x in payload.session_ids])
    results = []
    for session in rows:
        key = session.runner_binding.session_key if session.runner_binding else ""
        out = {"session_id": str(session.id), "title": session.title or "", "session_key": key,
               "status": "", "error": "", "turn_id": "", "handoff": None}
        results.append(out)
        if not access.can_write(request.user, session):
            out.update(status="refused", error="you can read this session but not act in it")
            continue
        if payload.dry_run:
            busy = placement.busy_reason(session)
            out.update(status="refused" if busy else "would_move", error=busy)
            continue
        try:
            req, moved = transfer_requests.request(
                session=session, runner_value=str(target.id), brief=payload.brief,
                user=request.user, initiator=who.for_request(request, via="transfer"))
        except (LookupError, RuntimeError, FileExistsError, ValueError) as exc:
            out.update(status="refused", error=str(exc))
            continue
        if moved is None:
            out.update(status="pending", error="waiting on an administrator of the target")
            continue
        _binding, turn = moved
        h = session.handoffs.select_related("source_runner", "target_runner").first()
        out.update(status="moved", turn_id=str(turn.id) if turn else "",
                   handoff=handoff.summary(h) if h is not None else None)
    return {"from_runner": source.name, "to_runner": target.name, "results": results}


def _uuid_or_422(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise HttpError(422, f"not a session id: {value!r}")


# ---- join requests: asking to be added to a private conversation ----
# Declared above the `/{session_id}` routes, like the transfer requests.

def _join_request_out(req) -> dict:
    return join_requests.dto(req)


def _join_request_or_404(request: HttpRequest, request_id: uuid.UUID):
    req = (join_requests.visible_to(request.user).select_related("session", "requested_by", "decided_by")
           .filter(pk=request_id).first())
    if req is None:
        raise HttpError(404, "join request not found")
    return req


@router.post("/join-requests/{request_id}/decline", response=JoinRequestOut,
             summary="Decline someone's request to join your private conversation")
@gate("session-acl", kind="conversation")  # its owner, in it (`join_requests.may_decide`)
def decline_join_request(request: HttpRequest, request_id: uuid.UUID):
    """They stay out. To let them in, add them (`POST /{id}/participants`, from your
    browser, which gives them their copy of the conversation's key): that answers
    the request."""
    req = _join_request_or_404(request, request_id)
    try:
        join_requests.decline(req, request.user)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(409, str(exc))
    return _join_request_out(req)


@router.post("/join-requests/{request_id}/cancel", response=JoinRequestOut,
             summary="Withdraw your request to join a private conversation")
@gate("self", kind="conversation")  # only whoever asked
def cancel_join_request(request: HttpRequest, request_id: uuid.UUID):
    req = _join_request_or_404(request, request_id)
    try:
        join_requests.cancel(req, request.user)
    except PermissionError as exc:
        raise HttpError(403, str(exc))
    except ValueError as exc:
        raise HttpError(409, str(exc))
    return _join_request_out(req)


def _session_by_key_or_404(request: HttpRequest, key: uuid.UUID) -> Session:
    """The fallback for a UUID that is not a session id: the runner's
    `session_key` (a cloud session's key is a Claude session UUID, and it is
    what the Open Sessions card shows). Read through the SAME authority as the
    id lookup, so it finds nothing the caller could not already open by id."""
    matches = list(
        _site_scoped(request, access.readable_sessions(request.user,
                                                       workspace_slugs=_visible_slugs(request)))
        .filter(runner_binding__session_key=str(key))
        .values_list("pk", flat=True)[:5]
    )
    if not matches:
        raise HttpError(
            404,
            "no session with this id or session_key is visible to you. A session is "
            "visible to its creator, its participants, the agent's admins (for the "
            "agent's own threads) and, for an emdash-discovered session, its workspace",
        )
    if len(matches) > 1:
        raise HttpError(
            409,
            "this session_key names more than one session; open one by id: "
            + ", ".join(str(m) for m in matches),
        )
    return _session_or_404(request, matches[0])


def _in_it_or_403(request: HttpRequest, session) -> None:
    # Bringing someone into a sensitive conversation, or copying it whole (a fork),
    # hands its words on: only its own people may, on its own chat surfaces. Without
    # this an agent's admin — an owner of a visitor's thread who is not IN it — could
    # add themselves, or fork it, and read it back.
    if not sensitivity.reads_content(session, user=request.user,
                                     chat_surface=sensitivity.request_is_chat_surface(request)):
        raise HttpError(403, sensitivity.NOT_IN_IT)


def _page_in_it_or_403(request: HttpRequest, session) -> None:
    # A sensitive conversation's page is its people's screen: what it shows, what it
    # can do and what an action on it answers are read and driven behind the same
    # gate as its words (`sensitivity.reads_content`) — its own people, on its chat
    # surfaces. The agent running in it reaches the page through its chat key
    # (`apps/mcp/page_tools.py`, `apps/mcp/tools/page.py`), never through these routes.
    if not sensitivity.reads_content(session, user=request.user,
                                     chat_surface=sensitivity.request_is_chat_surface(request)):
        raise HttpError(403, sensitivity.NOT_IN_IT_PAGE)


def _holder_or_422(*, user=None, contact=None) -> None:
    try:
        conversation_keys.check_holder(user=user, contact=contact)
    except conversation_keys.CannotHold as exc:
        raise HttpError(422, str(exc)) from None


def _readable(request: HttpRequest, session, rows) -> list:
    # A sensitive conversation reads back only for its own people, on its chat
    # surfaces (`sensitivity.reads_content`): an MCP call, a PAT or a co-tenant
    # who is not in it gets the metadata columns.
    return sensitivity.readable(rows, session, user=request.user,
                                chat_surface=sensitivity.request_is_chat_surface(request))


@router.get("/{session_id}", response=SessionDetailOut, summary="Get a session + transcript tail")
@gate("session-acl")
def get_session(request: HttpRequest, session_id: uuid.UUID, full: bool = False):
    """A session and the tail of its transcript.

    `session_id` is the session's id. When no session you can read has that id,
    it is tried as the runner's `session_key` instead (a cloud session's key is
    a UUID, and it is the name the Open Sessions card shows); the response's
    `id` then differs from the one you asked for, and `session_key` is the one
    that matched. A key naming several sessions is a 409 listing their ids.
    """
    # Tail-first: never ship the whole transcript by default. The client gets the
    # last SESSION_TAIL_DEFAULT messages + a backward cursor; ?full=true is the
    # explicit escape hatch. Scroll-back pages via GET /{id}/messages?before=.
    try:
        session = _session_or_404(request, session_id)
    except Http404:
        session = _session_by_key_or_404(request, session_id)
    data = _out(session, viewer=request.user)
    if data["you_are_in_it"] is False:
        asked = join_requests.latest_of(session, request.user)
        data["your_join_request"] = asked.status if asked is not None else ""
    rows, has_more, oldest = transcript_read.visible_transcript(session, full=full)
    rows = _readable(request, session, rows)
    from apps.agents.credentials import secret_requests
    from apps.embedding import delegation
    from apps.mcp_apps_host import mcp_apps_views

    mcp_apps_views.annotate(session, rows)
    secret_requests.annotate(session, rows)

    if delegation.acting_app(request) is not None:
        rows = transcript_read.for_widget(rows)
    data["messages"] = [MessageOut.from_orm(m) for m in rows]
    data["has_more_before"] = has_more
    data["oldest_loaded_turn_index"] = oldest
    # Same reader as the WS snapshot, so opening a session over REST and over
    # the socket can never disagree about whether an agent is waiting.
    data["menu"] = serializers.pending_menu(session)
    data["subagents"] = serializers.session_subagents(session)
    data["turn_status"] = status_feed.status_for_session(session)
    data["my_role"] = access.role_for(request.user, session)
    data["can_move"] = filing.can_move(request.user, session)
    # A sensitive conversation's key, for the reader's browser to open `sealed` with.
    data["sealed_key"] = sensitivity.sealed_key_for(
        session, user=request.user, chat_surface=sensitivity.request_is_chat_surface(request),
        keyset=sensitivity.request_is_panel_browser(request))
    return data


#: How many of a session's turns Session info lists. A long-lived chat has a
#: turn per message; the panel is for "how did this start, and what drove it
#: lately", and `turn_count` says how many there were in all.
SESSION_INFO_TURNS = 50


#: The metadata keys Session info shows, and no others: what started it and where it
#: belongs (the host's own linkage, the site, the requirements). Everything else canopy
#: keeps there is its own bookkeeping — where each turn's transcript ends
#: (`turn_end_epochs`), View receipts, Slack's history — and a new key stays unpublished
#: until someone decides it is safe.
INFO_METADATA_KEYS = frozenset({
    "source", "origin_key", "opp_slug", "opp_run_id",
    "embed_app", "via", "capability", "runner_requirements", "transcript_sourced",
    "requested_runner_id", "slack_team", "slack_channel",
})


def _info_binding(binding, *, sensitive: bool = False) -> dict | None:
    if binding is None:
        return None
    runner = binding.runner if binding.runner_id else None
    if sensitive:
        # The box's own Claude session and the task summary say what the
        # conversation is, and it says nothing outside its people.
        return {**_info_binding(binding), "transcript_id": "", "summary": ""}
    return {
        "runner_id": runner.id if runner else None,
        "runner_name": runner.name if runner else None,
        "runner_kind": runner.kind if runner else None,
        "host": binding.host,
        "emdash_project": binding.emdash_project,
        "session_key": binding.session_key,
        "thread_key": binding.thread_key,
        "transcript_id": binding.transcript_id,
        "task_uid": binding.task_uid,
        "agent_task_ext_id": binding.agent_task_ext_id,
        "status": binding.status,
        "agent_status": binding.agent_status,
        "summary": binding.summary,
        "transferred_at": binding.transferred_at,
        "transferred_from_name": binding.transferred_from.name if binding.transferred_from_id else None,
        "reported_at": binding.reported_at,
        "live_seen_at": binding.live_seen_at,
        "last_interacted_at": binding.last_interacted_at,
        "updated_at": binding.updated_at,
    }


@router.get("/{session_id}/info", response=SessionInfoOut, summary="Everything stored about a session")
@gate("session-acl")
def get_session_info(request: HttpRequest, session_id: uuid.UUID):
    """What canopy stores about one session: the session itself (who opened it,
    what created it and under which parent, what it did), the runner binding
    that backs it, and the turns that drove it — each with who asked, through
    which credential and client, in which mode and on which runner. Readable by
    anyone who can read the session; turn content you may not read is blanked."""
    from django.db.models import Q

    from apps.harness import turn_access
    from apps.harness.models import Turn

    session = _session_or_404(request, session_id)
    binding = getattr(session, "runner_binding", None)
    # The same two ways a turn drives a session as `staleness.driving_turns`,
    # for one session rather than under a queryset.
    driving = Q(chat_session=session)
    if session.agent_id and binding is not None and binding.session_key:
        driving |= Q(agent_id=session.agent_id, session_key=binding.session_key)
    turns_qs = Turn.objects.filter(driving)
    first_claimed = turns_qs.exclude(turn_mode="").order_by("created_at").first()
    session._turn_mode = first_claimed.turn_mode if first_claimed else ""
    session._turn_origin = first_claimed.origin if first_claimed else ""
    turns = list(
        turns_qs.select_related(
            "agent", "chat_session", "claimed_by", "enqueued_by", "pinned_runner",
            "requested_turn_mode_by", "raised_from_task", "initiator_user", "initiator_contact",
            "parent_turn__claimed_by",
        ).order_by("-created_at")[:SESSION_INFO_TURNS]
    )
    turn_access.redact(turns, request.user)
    return {
        "session": _out(session, viewer=request.user),
        "metadata": {k: v for k, v in (session.metadata or {}).items() if k in INFO_METADATA_KEYS},
        "contact": str(session.contact) if session.contact_id else None,
        "updated_at": session.updated_at,
        "binding": _info_binding(binding, sensitive=sensitivity.is_sensitive(session)),
        "turns": turns,
        "turn_count": turns_qs.count(),
        "turns_limit": SESSION_INFO_TURNS,
        "participant_count": session.participants.count(),
        "message_count": session.messages.count(),
    }


@router.get("/{session_id}/slash-commands", response=list[SlashCommandOut],
            summary="The slash commands the composer offers")
@gate("session-acl")  # can_write: only someone who can send gets the list
def list_slash_commands(request: HttpRequest, session_id: uuid.UUID):
    """What `/` offers in this session's composer: the agent's launchable skills
    and what the session's runner reported it can run. Empty for someone who can
    read the session but not send to it."""
    session = _session_or_404(request, session_id)
    if not access.can_write(request.user, session):
        return []
    return slash_commands.for_session(session)


@router.get(
    "/{session_id}/messages",
    response=MessagePageOut,
    summary="Load earlier transcript (scroll-back)",
)
@gate("session-acl")
def list_messages(
    request: HttpRequest,
    session_id: uuid.UUID,
    before: int,
    limit: int = transcript_read.SCROLLBACK_PAGE_DEFAULT,
):
    # Cursor-based backward paging: the window of `limit` messages immediately
    # older than `before` (a turn_index), chronological, + whether older exists.
    # Clamp here (not in transcript_read.messages_before, which stays a pure helper) —
    # an unclamped `?limit=-1`/`0` hits `queryset[:limit]` and raises
    # ValueError("Negative indexing is not supported"), surfacing as a 500.
    session = _session_or_404(request, session_id)
    limit = clamp_limit(limit)
    rows, has_more = transcript_read.messages_before(session, before=before, limit=limit)
    rows = _readable(request, session, rows)
    from apps.agents.credentials import secret_requests
    from apps.embedding import delegation
    from apps.mcp_apps_host import mcp_apps_views

    mcp_apps_views.annotate(session, rows)
    secret_requests.annotate(session, rows)

    if delegation.acting_app(request) is not None:
        rows = transcript_read.for_widget(rows)
    return {
        "messages": [MessageOut.from_orm(m) for m in rows],
        "has_more_before": has_more,
    }


@router.get(
    "/{session_id}/human-inputs",
    response=HumanInputPageOut,
    summary="What people typed into a session (cursor)",
)
@gate("session-acl")  # _session_or_404 / _session_by_key_or_404, as get_session
def list_human_inputs(
    request: HttpRequest,
    session_id: uuid.UUID,
    after: int | None = None,
    limit: int = transcript_read.SCROLLBACK_PAGE_DEFAULT,
):
    """Only the human side of a session: what a person typed, oldest first,
    `limit` (≤ 500) at a time. Pass the response's `next_cursor` back as
    `after` for the next page; it is null when there is no more.

    Excluded, though some are stored on the user's side of a transcript: tool
    results, the agent's output, system rows, harness records (task
    notifications, system reminders, local command output) and the prompts
    PROGRAMS delivered — a scheduled or email turn's `/agent:turn …`, an API
    caller — and what Claude Code itself wrote as a user record: a skill's or
    slash command's injected body, the agent's own ScheduleWakeup/cron fires,
    a peer session's message, a compaction summary, a subagent's records. Those
    are decided by the transcript record's own flags (`content.record`, captured
    by the runner); a row shipped before runners captured them falls back to the
    session's own rows (the tool call that carried the prompt, the Skill result
    it follows) and, last, to text shape (`transcript_read.InjectedPrompts`).

    A session with no durable transcript yet (a local runner session before its
    backfill) answers from the runner's recent tail, `source: "tail"`, unpaged.
    Same access rule as `GET /{id}`, and `session_id` may likewise be the
    runner's `session_key`.
    """
    try:
        session = _session_or_404(request, session_id)
    except Http404:
        session = _session_by_key_or_404(request, session_id)
    rows, next_cursor, source = transcript_read.human_inputs(
        session, after=after, limit=clamp_limit(limit))
    return {
        "messages": [MessageOut.from_orm(m) for m in rows],
        "next_cursor": next_cursor,
        "source": source,
    }


@router.post("/{session_id}/archive", response=SessionOut, summary="Archive a session")
@gate("session-acl", kind="conversation")  # write
def archive_session(request: HttpRequest, session_id: uuid.UUID):
    """Retire a session by hand. The escape hatch for a web chat — no runner will ever
    report it archived — and for force-retiring a row without touching emdash.
    Idempotent, and never destructive: /unarchive brings it straight back."""
    return _set_status(request, session_id, Session.ARCHIVED)


@router.post("/{session_id}/reset", response=ResetOut, summary="Reset a session from its transcript")
@gate("session-acl", kind="conversation")  # write
def reset_session(request: HttpRequest, session_id: uuid.UUID, dry_run: bool = False):
    """Drop this session's derived messages and re-derive them from the runner's
    transcript.

    A first-class action, not a repair: once the transcript is the durable record,
    these rows are a CACHE of a file on the runner's disk, so rebuilding them is
    cheap and repeatable — the thing you reach for constantly while building, and
    the way to pick up history the old per-turn projection could never capture.

    Refuses (200 with ok=false + a reason) rather than erroring when there is
    nothing to re-derive from: `no_binding` (no pointer to a transcript) or
    `runner_unreachable` (its box is offline — try again when it's back). Turns
    and their event ledger are never touched; nothing can rebuild those.
    """
    session = _session_or_404(request, session_id, write=True)   # membership gate: non-member -> 404
    return services.reset_session(session, dry_run=dry_run)


@router.post("/{session_id}/unarchive", response=SessionOut, summary="Unarchive a session")
@gate("session-acl", kind="conversation")  # write
def unarchive_session(request: HttpRequest, session_id: uuid.UUID):
    """Undo an archive. Note this clears only the WRITTEN half: a runner session that
    is also past SESSION_STALE_AFTER stays out of `state=active` until its runner
    reports it again, because that half is derived on every read."""
    return _set_status(request, session_id, Session.ACTIVE)


@router.put("/{session_id}/notify", response=SessionOut, summary="Set a session's completion notifications")
@gate("session-acl", kind="conversation")  # write
def set_session_notify(request: HttpRequest, session_id: uuid.UUID, payload: SessionNotifyIn):
    """`every_completion: true` pushes a notification each time a turn in this
    session finishes. Off (the default), one notification is sent once the session
    has been quiet for your chosen number of minutes."""
    session = _session_or_404(request, session_id, write=True)
    if session.notify_every_completion != payload.every_completion:
        session.notify_every_completion = payload.every_completion
        session.save(update_fields=["notify_every_completion", "updated_at"])
    return _out(session)


@router.put("/{session_id}/title", response=SessionOut, summary="Rename a session")
@gate("session-acl", kind="conversation")  # write
def set_session_title(request: HttpRequest, session_id: uuid.UUID, payload: SessionTitleIn):
    """Give this session a title of your own. A title somebody typed is never
    replaced by the emdash task name or the first message; a blank one clears
    it, and the session is named again the way an untitled one is. Renames the
    session in canopy only — the emdash task keeps its name."""
    session = _session_or_404(request, session_id, write=True)
    title = " ".join(payload.title.split())[:200]
    # A sensitive conversation keeps no title: a name is shown in every list,
    # Home and push, where its words never go. Clearing one is still allowed.
    if title and sensitivity.is_sensitive(session):
        raise HttpError(422, sensitivity.NO_TITLE)
    if session.title != title:
        session.title = title
        session.save(update_fields=["title", "updated_at"])
        groups.publish(
            groups.session_group(session.pk),
            {"type": "session.title_updated", "title": title},
        )
    return _out(session)


@router.post("/{session_id}/move", response=SessionOut, summary="Move a session to another workspace")
# filing.move: read gate, then the session's own person (creator / runner owner),
# then content.write in the TARGET workspace; an agent's session is a 422.
@gate("session-acl", "content.write", kind="conversation")
def move_session(request: HttpRequest, session_id: uuid.UUID, payload: SessionMoveIn):
    """File this session in another workspace you belong to.

    Only the person whose session it is may move it: its creator for a web chat,
    or the owner of the runner it was discovered on for an emdash session. The
    target must be a workspace you are an editor (or better) in. An agent's
    session cannot be moved (422) — it belongs to the agent's workspace.

    Tenant-first: afterwards only members of the target can see it. Participants
    who are not members of the target are removed from the chat. Your runner
    re-reporting the session never moves it back. Moving to the workspace it is
    already in is a no-op."""
    session = _session_or_404(request, session_id)
    try:
        filing.move(session, payload.workspace, by=request.user)
    except filing.FilingError as exc:
        raise HttpError(exc.status, str(exc)) from exc
    return _out(session)


def _participants(session: Session) -> list[dict]:
    rows = list(session.participants.select_related("user").order_by("created_at"))
    # Who in a sensitive conversation cannot read it yet, for its people to give a copy.
    keyless = (conversation_keys.without_current_copy(session, [p.user_id for p in rows])
               if sensitivity.is_sensitive(session) else set())
    return [{**serializers.participant_dto(p), "needs_key": p.user_id in keyless} for p in rows]


@router.get("/{session_id}/participants", response=list[ParticipantOut],
            summary="Who has been given this chat")
@gate("session-acl")
def list_participants(request: HttpRequest, session_id: uuid.UUID):
    """Everyone explicitly in this conversation, with their role."""
    return _participants(_session_or_404(request, session_id))


@router.post("/{session_id}/participants", response=list[ParticipantOut],
             summary="Give a teammate this chat")
@gate("session-acl", setting=True)  # can_share (chat owner)
def add_participant(request: HttpRequest, session_id: uuid.UUID, payload: ParticipantAddIn):
    """Owner only. The teammate must already be a member of the chat's
    workspace; adding someone again changes their role."""
    from django.contrib.auth import get_user_model

    from .models import SessionParticipant

    session = _session_or_404(request, session_id)
    if not access.can_share(request.user, session):
        raise HttpError(403, "only the chat's owner can share it")
    _in_it_or_403(request, session)
    target = get_user_model().objects.filter(email__iexact=payload.email.strip()).first()
    if target is None or not wsvc.is_member(target, session.workspace_id):
        raise HttpError(404, "nobody with that email is in this workspace")
    if target.pk == session.created_by_id:
        raise HttpError(409, "that person owns this chat")
    with transaction.atomic():
        if sensitivity.is_sensitive(session):
            _store_wrap(request, session, target, payload.wrap)
        SessionParticipant.objects.update_or_create(
            session=session, user=target, defaults={"role": payload.role})
        # Their request to be added, if they asked, is answered by this.
        join_requests.settle_on_add(session, target, by=request.user)
    return _participants(session)


def _store_wrap(request: HttpRequest, session, target, wrap) -> None:
    """A sensitive conversation's new person needs their copy of its key, which only a
    browser already in it can make (canopy-web cannot unwrap the key). Not needed again
    for a role change: they already hold a current one."""
    # Someone who already holds a current copy keeps it (a role change, a re-add): an
    # overwrite could lock them out (`conversation_keys.CopyExists`).
    if conversation_keys.has_current_wrap(session, target):
        return
    if wrap is None:
        raise ProblemError(409, "Their copy of the key is needed", type_=TYPE_CONFLICT,
                           detail="this conversation is sealed to its people: add them from a "
                                  "browser already in it, which wraps its key to their vault key",
                           members={"error": "wrap_required"})
    try:
        conversation_keys.add_wrap(session, target, wrap.model_dump(exclude={"recipient_jkt"}),
                                   recipient_jkt=wrap.recipient_jkt, by=request.user)
    except conversation_keys.WrapRefused as exc:
        raise HttpError(422, str(exc)) from None


@router.get("/{session_id}/participants/recipient", response=ParticipantRecipientOut,
            summary="The vault key to wrap a sensitive chat's key to")
@gate("session-acl")  # can_share (chat owner), and in the conversation
def participant_recipient(request: HttpRequest, session_id: uuid.UUID, email: str):
    """Before adding someone to a SENSITIVE conversation: their user id and current
    vault public key, which your browser wraps the conversation's key to. 404 when
    nobody with that email is in the workspace; 409 `no_vault_key` when they have no
    vault key yet (they could not read it)."""
    session = _session_or_404(request, session_id)
    _in_it_or_403(request, session)
    target = get_user_model().objects.filter(email__iexact=email.strip()).first()
    if target is None or not wsvc.is_member(target, session.workspace_id):
        raise HttpError(404, "nobody with that email is in this workspace")
    # Its owner asks before adding someone; anyone in it, before giving a copy of the key
    # to someone already in it (`POST /{id}/key-copies`).
    if not access.can_share(request.user, session) and not sensitivity.is_conversation_person(
            session, user=target):
        raise HttpError(403, "only the chat's owner can share it")
    found = conversation_keys.recipient(target)
    if found is None:
        raise ProblemError(409, "No vault key", type_=TYPE_CONFLICT,
                           detail=f"{target.email} has no vault key yet, so they cannot read a "
                                  "sensitive conversation; ask them to set one up first",
                           members={"error": "no_vault_key"})
    return {**found, "email": target.email}


@router.post("/{session_id}/key-copies", response=list[ParticipantOut],
             summary="Give someone in a private chat their copy of its key")
@gate("session-acl", setting=True)  # in the conversation, holding a current copy
def give_key_copy(request: HttpRequest, session_id: uuid.UUID, payload: KeyCopyIn):
    """For someone ALREADY in a sensitive conversation who cannot read it: given it
    before it became sensitive, or whose vault key changed since (its starter
    included). Your browser opens your copy of the key and wraps it to their current
    vault key (`GET /{id}/participants/recipient`); canopy-web stores the wrap. An editor
    or the owner who holds a current copy may give one (403 otherwise), and only to
    someone holding no copy for their current vault key: 409 when they do. A copy for
    an older vault key is replaced; one that does not open is set aside by its holder
    first (`DELETE /{id}/key-copies/mine`). It puts nobody in it: 404 when they are not
    in it."""
    session = _session_or_404(request, session_id)
    _in_it_or_403(request, session)
    if not access.can_write(request.user, session):
        raise HttpError(403, "only an editor or the owner of this conversation can give "
                             "someone a copy of its key")
    if not sensitivity.is_sensitive(session) or conversation_keys.key_of(session) is None:
        raise HttpError(409, "this conversation has no key to give")
    if not conversation_keys.has_current_wrap(session, request.user):
        raise HttpError(409, "your own copy of its key is not for your current vault key; "
                             "ask someone else in it to give you a fresh one first")
    target = get_user_model().objects.filter(pk=payload.user_id).first()
    if target is None or not sensitivity.is_conversation_person(session, user=target):
        raise HttpError(404, "nobody with that id is in this conversation")
    try:
        conversation_keys.add_wrap(session, target, payload.wrap.model_dump(exclude={"recipient_jkt"}),
                                   recipient_jkt=payload.wrap.recipient_jkt, by=request.user)
    except conversation_keys.CopyExists as exc:
        raise HttpError(409, str(exc)) from None
    except conversation_keys.WrapRefused as exc:
        raise HttpError(422, str(exc)) from None
    return _participants(session)


@router.delete("/{session_id}/key-copies/mine", response=list[ParticipantOut],
               summary="Set aside your copy of a private chat's key that does not open")
@gate("session-acl", setting=True)  # your own copy, in the conversation
def set_aside_my_key_copy(request: HttpRequest, session_id: uuid.UUID):
    """Your copy of this conversation's key does not open in your browser (it was
    damaged, or wrapped to a vault key you no longer have). Setting it aside drops it,
    so you are listed `needs_key` and someone in it can give you a fresh one
    (`POST /{id}/key-copies`). Only you can do this for your copy."""
    session = _session_or_404(request, session_id)
    _in_it_or_403(request, session)
    conversation_keys.set_aside_my_copy(session, request.user)
    return _participants(session)


@router.delete("/{session_id}/participants/{user_id}", response=list[ParticipantOut],
               summary="Take a chat away from someone")
@gate("session-acl", setting=True)  # can_share, or self-removal
def remove_participant(request: HttpRequest, session_id: uuid.UUID, user_id: int):
    """The owner can remove anyone but themselves; anyone can remove themselves."""
    from .models import SessionParticipant

    session = _session_or_404(request, session_id)
    if user_id == session.created_by_id:
        raise HttpError(409, "the chat's owner cannot be removed")
    if user_id != request.user.pk and not access.can_share(request.user, session):
        raise HttpError(403, "only the chat's owner can remove someone else")
    SessionParticipant.objects.filter(session=session, user_id=user_id).delete()
    # Their copy of a sensitive conversation's key goes with them. (The key itself is
    # not rotated: they could have kept what they read.)
    conversation_keys.drop_wrap(session, user_id)
    # A tab they still hold on it re-asks who may read it — for a sensitive
    # conversation, someone no longer in it must stop hearing it now.
    transaction.on_commit(lambda: services.recheck_sockets(session.pk, user_ids=[user_id]))
    return _participants(session)


@router.post("/{session_id}/join-requests", response=JoinRequestOut,
             summary="Ask to be added to a private conversation")
@gate("session-acl", kind="conversation")  # you can see it exists, and are not in it
def ask_to_join(request: HttpRequest, session_id: uuid.UUID):
    """A private (sensitive) conversation you can see listed but are not in: ask its
    owner to add you. The request names you and the conversation, nothing it said.
    Asking again returns the request already open. 409 for an ordinary
    conversation, or one you are already in; 429 when your last request into it,
    declined or withdrawn, was made within the hour."""
    session = _session_or_404(request, session_id)
    try:
        req = join_requests.ask(session, request.user)
    except join_requests.TooSoon as exc:
        raise HttpError(429, str(exc))
    except ValueError as exc:
        raise HttpError(409, str(exc))
    return _join_request_out(req)


@router.get("/{session_id}/join-requests", response=list[JoinRequestOut],
            summary="Open requests to join a private conversation you are in")
@gate("session-acl")  # its people; anyone else reads none
def list_join_requests(request: HttpRequest, session_id: uuid.UUID):
    """The people asking to be added, oldest first, for the conversation's own people
    (the same list rides the chat's socket as `session.join_requests`). Its owner adds
    them from the browser (`POST /{id}/participants`) or declines. Empty for anyone
    else."""
    session = _session_or_404(request, session_id)
    if not join_requests.may_see(request.user, session):
        return []
    return [_join_request_out(r) for r in join_requests.pending_for(session)]


@router.post("/{session_id}/send", response=SendOut, summary="Send a message")
@gate("session-acl", kind="conversation")  # write
def send(request: HttpRequest, session_id: uuid.UUID, payload: SendIn):
    session = _session_or_404(request, session_id, write=True)
    if not payload.text.strip():
        raise HttpError(422, "message text is required")
    message, turn = doors.send_from(
        "rest", session=session, text=payload.text, user=request.user,
        client_id=payload.client_id,
        runner_requirements=getattr(request, "runner_requirements", ()),
        chat_surface=sensitivity.request_is_chat_surface(request),
        initiator=who.for_request(request, via=who.channel(request, "chat")),
        placement=payload.placement, origin=payload.origin, parent=payload.parent,
        origin_ref={"clear_prompt": True} if payload.clear_prompt else None,
    )
    # The socket path commits the sender's server draft; this one did not, so a
    # line sent here stayed in the draft and came back on every later connect
    # as "<you> is typing". Clear it and tell the room, same frame the socket
    # sends (`draft.updated`, rendered per recipient by the consumer).
    cs_send.clear_draft_after_http_send(session, request.user, payload.text)
    return {"turn_id": turn.id if turn else None, "message": MessageOut.from_orm(message)}


@router.post(
    "/{session_id}/place", response=TurnOutMinimal,
    summary="Re-pin a session's oldest queued turn to a runner",
)
@gate("session-acl", kind="conversation")  # write
def place(request: HttpRequest, session_id: uuid.UUID, payload: PlaceIn):
    # The chat banner's after-the-fact directed-placement decision (vs. `runner_id`
    # on create / `placement` on send, which only apply to a turn at enqueue time).
    session = _session_or_404(request, session_id, write=True)
    try:
        turn = cs_send.place_queued_turn(session=session, placement=payload.placement,
                                          user=request.user)
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return turn


@router.post("/{session_id}/fork", response=SessionOut, summary="Fork a session from a message")
@gate("session-acl", kind="conversation")  # write
def fork_session(request: HttpRequest, session_id: uuid.UUID, payload: ForkIn):
    """Start a new session from this one's conversation up to the end of the turn
    `turn_index` belongs to. The new session runs on the same runner, in the same
    worktree, so both see the same files. Its own transcript is cut on the runner
    when its first message is sent; until then it holds a copy of the history.

    Needs a runner on the Claude CLI runtime (422 otherwise). 404 for an unknown
    message; 409 when the cut would fall inside a turn that is still running."""
    session = _session_or_404(request, session_id, write=True)
    _in_it_or_403(request, session)
    # A fork of a sensitive conversation carries its key, its people's wraps and its
    # agent's memory into a conversation its forker owns: the owner's act, as adding
    # someone is (`access.can_share`), never an editor's.
    if sensitivity.is_sensitive(session) and not access.can_share(request.user, session):
        raise HttpError(403, sensitivity.NOT_OWNER_FORK)
    try:
        fork = placement.fork_session(session=session, turn_index=payload.turn_index,
                                     user=request.user)
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except RuntimeError as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return _out(fork)


@router.post(
    "/{session_id}/transfer", response=TransferOut,
    summary="Move a live session onto another runner",
)
@gate("session-acl", kind="conversation")  # write
def transfer(request: HttpRequest, session_id: uuid.UUID, payload: TransferIn):
    """Move a session between boxes — cloud -> laptop, or between the two macOS
    accounts — carrying its message history across.

    `place` was the closest thing before this and it is not the same operation:
    it re-pins one queued turn and leaves the binding where it was, so the next
    ship still 404s and the next send sticks to the old box. The failure that
    motivated this endpoint was doing the move by hand with `place`/`send` —
    execution DID move, and the session's entire pre-transfer history was deleted
    on the new box's first ship (session 169212e2, 2026-09-12).

    WHOSE box it lands on decides whether it moves now (`status: moved`) or asks
    (`status: pending`): onto a runner you administer, or between two runners with
    the SAME owner, it moves now. Onto someone else's box it becomes a transfer
    request that one of `approvers` must approve (`approve_transfer_request`),
    because the move spends their machine and Claude subscription. `runner` is the
    target's id or name.

    409, not 422, while a turn executes: the request is well-formed and will
    succeed once the source box is idle, which is a state conflict rather than a
    bad body. Stop the session (`POST /{id}/stop`) and retry. Also 409 while
    another request for this session is waiting.
    """
    from . import transfer_requests

    session = _session_or_404(request, session_id, write=True)
    try:
        req, moved = transfer_requests.request(
            session=session, runner_value=payload.runner, brief=payload.brief,
            user=request.user, initiator=who.for_request(request, via="transfer"),
        )
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except (RuntimeError, FileExistsError) as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    if moved is None:
        return {
            "session_id": str(session.id), "runner": "", "transferred_from": "",
            "index_offset": 0, "turn_id": "", "status": "pending", "request_id": req.id,
            "approvers": sorted(u.email for u in transfer_requests.approvers(req.to_runner)),
        }
    return _moved_out(session, moved, req)


@router.get("/{session_id}/handoffs", response=list[HandoffOut],
            summary="What each transfer of this session carried")
@gate("session-acl")  # read
def list_handoffs(request: HttpRequest, session_id: uuid.UUID):
    """Every transfer of this session, newest first: what the source packed (or
    why it could not — NO SNAPSHOT), whether the target restored it and `git
    status` matched, and whether the source's task was closed."""
    session = _session_or_404(request, session_id)
    return [handoff.summary(h) for h in
            session.handoffs.select_related("source_runner", "target_runner")[:50]]


@router.post("/{session_id}/handoffs/{handoff_id}/restore", response=HandoffOut,
             summary="Restore a snapshot that arrived after its transfer")
@gate("session-acl", kind="conversation")  # write
def restore_handoff(request: HttpRequest, session_id: uuid.UUID, handoff_id: uuid.UUID):
    """A source that was asleep at the transfer packs its work when it is back.
    This puts that snapshot into the session now: a turn on the session's
    current runner restores it — into a worktree with no uncommitted changes
    only; the report says otherwise and leaves the worktree alone. 409 while a
    turn is queued or running in the session."""
    session = _session_or_404(request, session_id, write=True)
    h = get_object_or_404(session.handoffs.select_related("source_runner", "target_runner"),
                          pk=handoff_id)
    try:
        handoff.restore_late(h, user=request.user,
                             initiator=who.for_request(request, via="transfer"))
    except RuntimeError as exc:
        raise HttpError(409, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    h.refresh_from_db()
    return handoff.summary(h)


def _moved_out(session, moved, req) -> dict:
    binding, turn = moved
    h = session.handoffs.select_related("source_runner", "target_runner").first()
    return {
        "session_id": str(session.id),
        "runner": binding.runner.name if binding.runner_id else "",
        "transferred_from": (
            binding.transferred_from.name if binding.transferred_from_id else ""
        ),
        "index_offset": binding.index_offset,
        "turn_id": str(turn.id) if turn else "",
        "status": "moved",
        "request_id": req.id,
        "handoff": handoff.summary(h) if h is not None else None,
    }


@router.post(
    "/{session_id}/answer-menu", response=dict,
    summary="Answer the dialog an agent is blocked on",
)
@gate("session-acl", kind="conversation")  # write
def answer_menu(request: HttpRequest, session_id: uuid.UUID, payload: MenuAnswerIn):
    """Approve or refuse a permission prompt from the web.

    A refusal is a 200 with `ok:false` and a stable reason, not a 4xx: the dialog
    can go stale between the phone rendering it and a thumb reaching it, and the
    runner can go offline in between — both ordinary, neither a client error.
    Same shape `reset` uses for the same reason.
    """
    session = _session_or_404(request, session_id, write=True)
    outcome = control.answer_menu(session=session, option=payload.option,
                                   selections=payload.selections,
                                   texts=payload.texts)
    return {"ok": outcome == "sent", "reason": "" if outcome == "sent" else outcome}


@router.post("/{session_id}/close", response=dict, summary="Close a session for good")
@gate("session-acl", kind="conversation")  # write
def close_session(request: HttpRequest, session_id: uuid.UUID):
    """End a session — delete its emdash task if a runner is reporting one, or
    archive it outright if nothing exists on a box.

    `closing: true` means the close was relayed to a runner and the row is still
    listed: the runner deletes the task and its next report retires the session.
    `closing: false` with `ok: true` means it is already done. A refusal is a 200
    with `ok:false` and a stable reason (`unavailable`, `already_closed`), never a
    4xx — same shape `answer-menu` and `reset` use, for the same reason.

    There is deliberately no `unbound` refusal: a session with no binding has
    nothing on a box, which is the second branch rather than an error.
    """
    session = _session_or_404(request, session_id, write=True)   # membership gate: non-member -> 404
    outcome = control.close_session(session=session)
    ok = outcome in ("closing", "closed")
    return {"ok": ok, "closing": outcome == "closing", "reason": "" if ok else outcome}


@router.post("/{session_id}/stop", response=dict,
             summary="Stop this session: cancel its open turns, or interrupt its agent")
@gate("session-acl", kind="conversation")  # write
def stop_session_turn(request: HttpRequest, session_id: uuid.UUID):
    """Stop whatever this session is doing — the same stop as the web UI's button.
    Returns `cancelled` (an open turn was cancelled), `interrupted` (no turn was
    open, so the session's runner was asked to interrupt the agent) and `route`."""
    # canopy-web#1226: this used to cancel turns only. An agent/project turn is
    # already DONE once its prompt is delivered, so a stop from MCP or a script
    # found nothing to cancel and the agent worked on. control.stop_session is the
    # one stop the websocket uses too: turns first, then the session interrupt.
    session = _session_or_404(request, session_id, write=True)
    route = control.stop_session(session, by=control.person_name(request.user))
    return {"cancelled": route == "turns", "interrupted": route == "session", "route": route}


@router.post("/{session_id}/attach", response=StreamStateOut, summary="Attach a viewer (start live streaming)")
@gate("session-acl", kind="conversation")  # READ gate on a POST (viewer signal)
def attach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": transcript_ingest.attach_session(session)}


@router.post("/{session_id}/detach", response=StreamStateOut, summary="Detach a viewer (stop when last leaves)")
@gate("session-acl", kind="conversation")  # READ gate on a POST
def detach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": transcript_ingest.detach_session(session)}


@router.post("/{session_id}/backfill", response=BackfillStateOut, summary="Request full history from the runner")
@gate("session-acl", kind="conversation")  # READ gate on a POST
def request_backfill(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    # A sensitive-data runner's session is refused (409): that box keeps its
    # transcripts to itself (`transcript_read.backfill_refused`).
    try:
        return {"status": transcript_read.request_backfill(session)}
    except transcript_read.BackfillRefusedError as exc:
        raise ProblemError(409, "History is not asked of a sensitive-data runner",
                           detail=str(exc)) from exc


#: Why a private conversation takes no file (`upload_attachment`).
ATTACHMENTS_REFUSED = ("files cannot be attached to a private conversation yet: canopy would have "
                       "to keep them where it could read them. Paste the part that matters as text.")


@router.post(
    "/{session_id}/attachments",
    response={201: AttachmentOut},
    summary="Upload an attachment for this session",
)
@gate("session-acl", kind="conversation")  # write
def upload_attachment(
    request: HttpRequest, session_id: uuid.UUID, file: UploadedFile = File(...)
):
    """Store the bytes and return an id the caller passes to /send.

    UNBOUND on purpose (`message` null): the composer uploads while you are
    still typing, so the message it belongs to does not exist yet. Sending binds
    it. That ordering is also what lets the UI show a thumbnail before send.
    """
    session = _session_or_404(request, session_id, write=True)   # membership gate: non-member -> 404
    # A private conversation's words are sealed to its people, and its files would be
    # its words too — but the agent reads a file on its box, which holds no key to the
    # conversation. Until the box can open a sealed file, none is taken in one rather
    # than kept where canopy-web could read it.
    if sensitivity.is_sensitive(session):
        raise HttpError(422, ATTACHMENTS_REFUSED)
    if not attachment_storage.is_configured():
        raise HttpError(503, "attachments are not configured on this deployment")

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    allowed = settings.ATTACHMENT_ALLOWED_CONTENT_TYPES
    if content_type not in allowed:
        # An allowlist: these bytes get opened by an agent and rendered inline by
        # a browser, so anything not explicitly understood is refused.
        raise HttpError(422, f"unsupported file type '{content_type or 'unknown'}'")
    if file.size is None or file.size <= 0:
        raise HttpError(422, "file is empty")
    if file.size > settings.ATTACHMENT_MAX_UPLOAD_BYTES:
        limit_mb = settings.ATTACHMENT_MAX_UPLOAD_BYTES // (1024 * 1024)
        raise HttpError(422, f"file is larger than the {limit_mb}MB limit")

    attachment = Attachment(
        session=session,
        uploaded_by=request.user,
        filename=attachment_storage.safe_filename(file.name),
        content_type=content_type,
        size_bytes=file.size,
    )
    attachment.storage_key = attachment_storage.storage_key(
        session.id, attachment.id, attachment.filename
    )
    # Bytes FIRST, row second: a row whose object is missing is a broken
    # thumbnail and a runner download that 500s, while an orphaned object is
    # invisible and sweepable. Fail in the harmless direction.
    attachment_storage.put(attachment.storage_key, file.read(), content_type)
    attachment.save()
    return 201, attachment


@router.get(
    "/attachments/{attachment_id}/content",
    summary="Stream an attachment's bytes",
)
@gate("session-acl")  # can_read
def attachment_content(request: HttpRequest, attachment_id: uuid.UUID):
    """The bytes, for a person who can read the chat — the browser rendering a
    thumbnail. Gated on who can read the session, not on who uploaded it: a
    session is multiplayer, so a teammate who can read it sees what was shared
    in it. A runner does NOT come through here; it fetches what the turn it is
    serving carries via the harness route (turn_attachment_content)."""
    attachment = get_object_or_404(
        Attachment.objects.select_related("session").filter(
            session__in=_site_scoped(request, Session.objects.all())),
        pk=attachment_id,
    )
    # The session's own read rule: a teammate who can read the chat can see
    # what was shared in it; a co-tenant who cannot read it cannot either.
    if attachment.session.workspace_id not in _visible_slugs(request) or not access.can_read(
            request.user, attachment.session):
        raise HttpError(404, "attachment not found")
    # What was shared in a sensitive conversation is its words: only its own
    # people, on its chat surfaces, read it back (`sensitivity.reads_content`).
    if not sensitivity.reads_content(attachment.session, user=request.user,
                                     chat_surface=sensitivity.request_is_chat_surface(request)):
        raise HttpError(404, "attachment not found")
    return attachment_storage.http_response(attachment)


@router.delete("/attachments/{attachment_id}", response={204: None})
@gate("session-acl", kind="conversation")  # can_write
def delete_attachment(request: HttpRequest, attachment_id: uuid.UUID):
    """Remove an attachment you have not sent yet — the composer's "x" on a chip.

    Only while UNBOUND. Once it is part of a sent message it is transcript, and
    deleting it would leave the agent's reply referring to something nobody else
    can see.
    """
    attachment = get_object_or_404(
        Attachment.objects.select_related("session").filter(
            session__in=_site_scoped(request, Session.objects.all())),
        pk=attachment_id,
    )
    if attachment.session.workspace_id not in _visible_slugs(request) or not access.can_read(
            request.user, attachment.session):
        raise HttpError(404, "attachment not found")
    if not access.can_write(request.user, attachment.session):
        raise HttpError(403, "you can read this session but not act in it")
    if attachment.message_id is not None:
        raise HttpError(409, "this attachment has already been sent")
    if attachment_storage.is_configured():
        attachment_storage.delete(attachment.storage_key)
    attachment.delete()
    return 204, None


# --- page actions -----------------------------------------------------------
#
# An embedded host declares what its page can do; the agent driving the session
# calls one. Deliberately NOT MCP: canopy's MCP tools are static Python
# functions scoped to a connection, while these are declared by a host at
# runtime, scoped to one session, and valid only while a tab is open. ACP
# already models this shape — a client advertises capabilities and the agent
# invokes them over the session — so canopy takes the shape rather than the
# wire format. See apps/canopy_sessions/page_actions.py.


@router.put("/{session_id}/page-actions", response=list[PageActionSpec],
            summary="Declare what the attached page can do")
@gate("session-acl", kind="conversation")  # write
def declare_page_actions(request: HttpRequest, session_id: uuid.UUID,
                         payload: PageActionsDeclareIn) -> list[PageActionSpec]:
    """Called by the page itself as it mounts, and whenever its actions change.

    Replaces the declaration wholesale — see `set_declared_actions` for why
    merging would leave the agent able to call into a page the user has left.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    page_actions.set_declared_actions(
        session, [a.dict() for a in payload.actions]
    )
    return [PageActionSpec(**a) for a in page_actions.declared_actions(session)]


@router.get("/{session_id}/page-actions", response=list[PageActionSpec],
            summary="What the attached page can do")
@gate("session-acl")
def list_page_actions(request: HttpRequest, session_id: uuid.UUID) -> list[PageActionSpec]:
    """How the agent discovers its options. An empty list means no page is
    attached — not that the page can do nothing."""
    session = _session_or_404(request, session_id)
    _page_in_it_or_403(request, session)
    return [PageActionSpec(**a) for a in page_actions.declared_actions(session)]


@router.put("/{session_id}/page-state", response=PageStateOut,
            summary="Declare what the attached page is showing")
@gate("session-acl", kind="conversation")  # write
def declare_page_state(request: HttpRequest, session_id: uuid.UUID,
                       payload: PageStateIn) -> PageStateOut:
    """Called by the page as it mounts and whenever its view changes.

    Replaces the declaration wholesale. A state larger than the server's cap is
    rejected with `too_large`: send the selection (ids, filters) and the tool
    that resolves it, not the rows themselves.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    try:
        stored = page_state.set_page_state(session, payload.state)
    except page_state.PageStateError as exc:
        # 422: well-formed request, unacceptable CONTENT — the page must change
        # what it sends, which is a different fix from retrying.
        raise HttpError(422, f"{exc.code}: {exc.message}")
    return PageStateOut(state=stored, version=int(stored.get("version") or 0))


@router.put("/{session_id}/run-input", response=RunAgentInputOut,
            summary="Declare the page in AG-UI's own shape")
@gate("session-acl", kind="conversation")  # write
def declare_run_input(request: HttpRequest, session_id: uuid.UUID,
                      payload: RunAgentInputIn) -> RunAgentInputOut:
    """Accepts AG-UI's `RunAgentInput` and applies the parts canopy honours.

    `state` becomes the page's declared view and `tools` become its callable
    actions — one call where canopy otherwise needs two. Fields canopy has no
    use for are accepted and ignored, so a conforming client can send the whole
    object unchanged.

    A `state` larger than the server's cap is rejected with `too_large`: send
    the selection (ids, filters) and the tool that resolves it, not the rows.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)

    # Actions first, then state — the same order the widget uses, and for the
    # same reason: whichever lands last, the agent must never see a page that
    # declares rows it has no way to act on.
    page_actions.set_declared_actions(session, [t.dict() for t in payload.tools])
    stored = page_state.current_page_state(session)
    if payload.state:
        try:
            stored = page_state.set_page_state(session, payload.state)
        except page_state.PageStateError as exc:
            raise HttpError(422, f"{exc.code}: {exc.message}")
    return RunAgentInputOut(
        state=stored,
        version=int(stored.get("version") or 0),
        tools=[PageActionSpec(**a) for a in page_actions.declared_actions(session)],
    )


@router.get("/{session_id}/page-state", response=PageStateOut,
            summary="What the attached page is showing")
@gate("session-acl")
def read_page_state(request: HttpRequest, session_id: uuid.UUID) -> PageStateOut:
    """How a surface other than the agent's MCP tool reads the current view."""
    session = _session_or_404(request, session_id)
    _page_in_it_or_403(request, session)
    stored = page_state.current_page_state(session)
    return PageStateOut(state=stored, version=int(stored.get("version") or 0))


@router.post("/{session_id}/page-actions/invoke", response=PageActionOut,
             summary="Ask the attached page to run an action")
@gate("session-acl", kind="conversation")  # write
def invoke_page_action(request: HttpRequest, session_id: uuid.UUID,
                       payload: PageActionInvokeIn) -> PageActionOut:
    """Blocks until the page answers, or refuses with a reason.

    Every non-success is an error with a `code` the caller can branch on
    (`no_page`, `unknown_action`, `bad_arguments`, `timeout`, `refused`) — a
    caller must never be able to read "the tab was closed" as "done".
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    try:
        action = page_actions.request_action(
            session=session, name=payload.name, args=payload.args, user=request.user
        )
    except page_actions.PageActionError as exc:
        # 409: the request was well-formed, the PAGE could not satisfy it.
        raise HttpError(422 if exc.code == "bad_arguments" else 409,
                        f"{exc.code}: {exc.message}")
    return PageActionOut(id=str(action.id), name=action.name, status=action.status,
                         result=action.result, error=action.error)


@router.post("/{session_id}/page-actions/{action_id}/result", response=PageActionOut,
             summary="The page reporting an action's outcome")
@gate("session-acl", kind="conversation")  # write
def resolve_page_action(request: HttpRequest, session_id: uuid.UUID, action_id: uuid.UUID,
                        payload: PageActionResultIn) -> PageActionOut:
    """Posted by the page after it runs the callback.

    Membership-gated like every other by-id read, and scoped to the session, so
    one page cannot resolve another's action.
    """
    session = _session_or_404(request, session_id, write=True)
    _page_in_it_or_403(request, session)
    action = session.page_actions.filter(pk=action_id).first()
    if action is None:
        raise HttpError(404, "no such page action on this session")
    action = page_actions.resolve(action, result=payload.result, error=payload.error)
    return PageActionOut(id=str(action.id), name=action.name, status=action.status,
                         result=action.result, error=action.error)


# ---- Secrets shared with a chat (models.SessionSecret) -----------------------
# The BROWSER half: share, list, forget. Values never come back here; the agent
# side (`secrets_api`) spends them, and only from the session bound to this chat.

def _secret_out(row) -> dict:
    return {
        "name": row.name,
        "created_by": getattr(row.created_by, "email", None),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expires_at": secrets.expires_at(row).isoformat(),
    }


@router.post("/{session_id}/secrets", response={201: SessionSecretOut},
             summary="Share a secret with this chat (write-only)")
@gate("session-acl", kind="conversation")  # write
def share_secret(request: HttpRequest, session_id: uuid.UUID, payload: SessionSecretIn):
    """Stores the value encrypted. Posts NOTHING into the chat: the person just
    refers to it by name, and the session finds it with `canopy secret list`."""
    session = _session_or_404(request, session_id, write=True)
    try:
        row = secrets.set_secret(session, payload.name, payload.value, user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return 201, _secret_out(row)


@router.get("/{session_id}/secrets", response=list[SessionSecretOut],
            summary="Secrets shared with this chat (names only, never values)")
@gate("session-acl")
def list_secrets(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return [_secret_out(r) for r in secrets.live_secrets(session)]


@router.delete("/{session_id}/secrets/{name}", response={204: None},
               summary="Forget a shared secret")
@gate("session-acl", kind="conversation")  # write
def delete_secret(request: HttpRequest, session_id: uuid.UUID, name: str):
    session = _session_or_404(request, session_id, write=True)
    session.secrets.filter(name=name).delete()
    return 204, None


@router.get("/{session_id}/export", response=SessionExportOut,
            summary="Export a session's conversation to pick it up in your own Claude")
@gate("session-acl", "self")  # visible AND you started it
def export_session(request: HttpRequest, session_id: uuid.UUID):
    """This session as readable markdown — the same rows the web view shows, tool
    output shortened — for handing to your own Claude so it can see where the
    work stands and carry on. For when the runner
    is out of tokens, or you want to take it from here yourself.

    `canopy runner export <session>` saves it to a file and prints the prompt to
    start from. Only the person who started the session can export it."""
    # Creator only, deliberately narrower than the session ACL: editors and
    # participants can drive a session, but taking its conversation off canopy is
    # the starter's call (Jon, 2026-10-08; admins to follow). An embedding site
    # acting for that user is refused — a widget is not a way to lift a chat out.
    # Built server-side from Message rows (`exports.build_markdown`); the runner's
    # raw transcript is never asked for.
    from apps.embedding import delegation

    from . import exports

    if delegation.acting_app(request) is not None:
        raise HttpError(403, "session export is not available to an embedded site")
    session = _session_or_404(request, session_id)
    if session.created_by_id is None or session.created_by_id != request.user.pk:
        raise HttpError(403, "only the person who started this session can export it")
    if sensitivity.is_sensitive(session):
        raise HttpError(403, sensitivity.REFUSAL)
    markdown, count = exports.build_markdown(session)
    if not count:
        raise HttpError(409, "this session has no conversation to export yet")
    return {"session_id": session.id, "title": session.title or "",
            "message_count": count, "markdown": markdown}
