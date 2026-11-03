"""Starting, steering and ending a session: create, send, fork, archive, close."""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.agents import services as agent_services
from apps.api.route_gates import gate
from apps.embedding import delegation
from apps.harness import clis
from apps.harness import initiator as who
from apps.harness import sensitivity
from apps.harness.models import Runner
from apps.realtime import groups
from apps.workspaces import services as wsvc

from . import (
    access,
    control,
    conversation_keys,
    doors,
    filing,
    maintenance,
    placement,
    services,
)
from . import send as cs_send
from .models import Session
from .schemas import (
    ForkIn,
    MenuAnswerIn,
    MessageOut,
    NoisePurgeIn,
    NoisePurgeOut,
    NoiseReportOut,
    PlaceIn,
    ResetIn,
    ResetOut,
    ResetSummaryOut,
    SendIn,
    SendOut,
    SessionCreateIn,
    SessionMoveIn,
    SessionNotifyIn,
    SessionOut,
    SessionTitleIn,
    TurnOutMinimal,
)
# The one session router and the helpers its routes share; api.py imports this
# module after defining them.
from .api import (
    _in_it_or_403,
    _session_or_404,
    _site_scoped,
    _visible_slugs,
    router,
)
from .session_row import session_out

#: Published operationIds keep this app's label (apps/api/api.py `_app_label`): an
#: operationId is an MCP tool name and a `generated.ts` key.
OPERATION_LABEL = "canopy_sessions"


def _set_status(request: HttpRequest, session_id: uuid.UUID, status: str) -> dict:
    session = _session_or_404(request, session_id, write=True)   # membership gate: non-member -> 404
    if status == Session.ARCHIVED:
        control.archive_session(session, by=control.person_name(request.user), user=request.user)
    else:
        control.unarchive_session(session, user=request.user)
    return session_out(session)


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
        if payload.cli and payload.cli not in clis.SENSITIVE_CLIS:
            raise HttpError(422, "a conversation holding this site's sensitive data runs on "
                                 "Claude Code only")
    if (refused := clis.refusal(agent, payload.cli or "")) is not None:
        raise HttpError(422, refused)
    # A key pair made in the starter's browser: if this conversation is (or becomes)
    # sensitive its words are sealed to it, and canopy-web never sees its private half.
    offered = None
    if payload.conversation_key is not None:
        try:
            offered = conversation_keys.check_offer(request.user, payload.conversation_key.model_dump(mode="json"))
        except conversation_keys.WrapRefused as exc:
            raise HttpError(422, str(exc)) from None
    if payload.runner_id:
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
        parent=payload.parent, conversation_key=offered, cli=payload.cli or "",
    )
    return session_out(session)


def _holder_or_422(*, user=None, contact=None) -> None:
    try:
        conversation_keys.check_holder(user=user, contact=contact)
    except conversation_keys.CannotHold as exc:
        raise HttpError(422, str(exc)) from None


# A one-segment path: registered before `/{session_id}` (api_read), which would
# otherwise take "reset" as a session id and 405 on its GET-only view — see the
# import order at the bottom of api.py.
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
    # Only sessions you may act in: scoping by workspace alone would let a co-tenant
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
    if payload.session_id:
        slugs = _visible_slugs(request)
    else:
        slugs = _visible_slugs(request, bulk_write=True) if payload.apply \
            else _visible_slugs(request, listing=True)
    return maintenance.purge_noise(workspace_slugs=slugs, session_id=payload.session_id,
                                   sample=payload.sample, apply=payload.apply, user=request.user)


@router.post("/{session_id}/archive", response=SessionOut, summary="Archive a session")
@gate("session-acl", kind="conversation")  # write
def archive_session(request: HttpRequest, session_id: uuid.UUID):
    """Retire a session by hand. The escape hatch for a web chat — no runner will ever
    report it archived — and for retiring a row without touching emdash (that is
    `/close`). Its queued and running turns are cancelled, and its runner's next
    report does not revive it while the task stays open. Idempotent, and never
    destructive: /unarchive brings it straight back, and so does sending to it."""
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
    return session_out(session)


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
    return session_out(session)


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
    return session_out(session)


@router.post("/{session_id}/send", response=SendOut, summary="Send a message")
@gate("session-acl", kind="conversation")  # write
def send(request: HttpRequest, session_id: uuid.UUID, payload: SendIn):
    session = _session_or_404(request, session_id, write=True)
    if not payload.text.strip():
        raise HttpError(422, "message text is required")
    # The page this was sent from: the agent reaches its site through that page's grant.
    services.note_request_grant(session, request)
    message, turn = doors.send_from(
        "rest", session=session, text=payload.text, user=request.user,
        client_id=payload.client_id,
        runner_requirements=getattr(request, "runner_requirements", ()),
        chat_surface=sensitivity.request_is_chat_surface(request),
        initiator=who.for_request(request, via=who.channel(request, "chat")),
        placement=payload.placement, origin=payload.origin, parent=payload.parent,
        origin_ref={"clear_prompt": True} if payload.clear_prompt else None,
    )
    # The socket path commits the sender's server draft; this one clears it here,
    # or a line sent here would stay in the draft and come back on every later
    # connect as "<you> is typing". Tells the room with the same frame the socket
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
        turn = placement.place_queued_turn(session=session, placement=payload.placement,
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
    return session_out(fork)


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
    # Cancelling turns alone is not a stop: an agent/project turn is already DONE
    # once its prompt is delivered, so there may be nothing to cancel while the
    # agent works on. control.stop_session is the one stop the websocket uses too:
    # turns first, then the session interrupt.
    session = _session_or_404(request, session_id, write=True)
    route = control.stop_session(session, by=control.person_name(request.user))
    return {"cancelled": route == "turns", "interrupted": route == "session", "route": route}
