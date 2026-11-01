"""Reading sessions: the list, search, one session and its transcript, live streaming."""
from __future__ import annotations

import base64
import datetime as dt
import uuid

from django.db.models import Q
from django.http import Http404, HttpRequest
from ninja.errors import HttpError

from apps.api.errors import ProblemError
from apps.api.pagination import clamp_limit
from apps.api.route_gates import gate
from apps.embedding import delegation
from apps.harness import sensitivity, turn_access
from apps.workspaces import services as wsvc

from . import (
    access,
    audience,
    exports,
    feed,
    filing,
    serializers,
    session_state,
    slash_commands,
    staleness,
    status_feed,
    transcript_ingest,
    transcript_read,
)
from . import join_requests
from .schemas import (
    SlashCommandOut,
    BackfillStateOut,
    HumanInputPageOut,
    MessageOut,
    MessagePageOut,
    SessionDetailOut,
    SessionExportOut,
    SessionInfoOut,
    SessionOut,
    SessionSearchPageOut,
    StreamStateOut,
)
# The one session router and the helpers its routes share; api.py imports this
# module after defining them.
from .api import (
    _aware,
    _filtered_sessions,
    _session_by_key_or_404,
    _session_or_404,
    me_router,
    router,
)
from .session_row import mark_people_in, session_out

#: Published operationIds keep this app's label (apps/api/api.py `_app_label`): an
#: operationId is an MCP tool name and a `generated.ts` key.
OPERATION_LABEL = "canopy_sessions"


@router.get("/", response=list[SessionOut], summary="List sessions (web + runner-discovered)")
@gate("session-acl")  # readable_sessions
def list_sessions(
    request: HttpRequest, state: str = "active", limit: int = 200,
    source: str = "", opp_slug: str = "", opp_run_id: str = "",
    origin_key: str = "", embed_app: str = "",
    resource: str = "", page_path: str = "", reply: bool = False,
    session_key: str = "", q: str = "", repo: str = "", branch: str = "", pr: str = "",
    since: dt.datetime | None = None, until: dt.datetime | None = None, runner: str = "",
    of_user: int | None = None, slack_channel: str = "",
):
    """The sessions you can see: waiting on you first, then running, then most
    recent activity — at most `limit` (≤ 500) of them, with no paging.

    Filters: `q` (title or session_key contains), `repo` (the repo it ran in or
    worked on — a name or owner/name), `branch` (any branch its transcript saw),
    `pr` (a PR it created or asked to merge — N, owner/name#N or a URL),
    `since` / `until` (last activity in [since, until)), `runner` (the runner it
    is on, by id or name — `?runner=<name>&state=active` is every open session on
    a box, what `POST /transfer-all` moves), `of_user` (one person's own sessions,
    by canopy user id: the chats they started and the runner sessions on their own
    boxes — how a coach lists the sessions of someone who let it review them),
    `slack_channel` (a Slack channel id: the sessions that live in it — how an agent
    an admin gave that channel to lists what it may review). To reach EVERY session
    rather than the newest 500, walk `GET /api/canopy-sessions/search` instead.
    """
    # The ONE list: every session the caller can see in their workspaces — their
    # own web sessions UNION any session that has a RunnerBinding
    # (runner-discovered or live). Deduped, running-first, then newest.
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
        branch=branch, pr=pr, since=since, until=until, runner=runner, of_user=of_user,
        slack_channel=slack_channel,
    ).order_by("-created_at")

    # Every row says which mode drove it — the list shows it on each card, so an
    # auto session that just ran and a manual one waiting on you can be told apart.
    rows = session_state.with_state(transcript_read.with_driving_turn(rows))
    if reply:
        rows = transcript_read.with_last_reply(rows)
    out = [session_out(s, reply=reply, viewer=request.user) for s in mark_people_in(rows, request.user)]
    # Waiting first, then running, then genuinely-most-recent: last_activity_at,
    # not created_at, under which a dead repo and a live one "created" in the
    # same report sweep interleave arbitrarily. The client can re-group by
    # project — this is the default order.
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
    rows = _filtered_sessions(
        request, state=state, source=source, origin_key=origin_key,
        embed_app=embed_app, session_key=session_key, q=q, repo=repo,
        branch=branch, pr=pr, since=since, until=until,
    )
    if cursor:
        at, sid = _decode_cursor(cursor)
        rows = rows.filter(Q(_activity__lt=at) | Q(_activity=at, id__lt=sid))
    limit = clamp_limit(limit)
    rows = session_state.with_state(transcript_read.with_driving_turn(rows.order_by("-_activity", "-id")))
    page = list(rows[: limit + 1])
    more = len(page) > limit
    page = page[:limit]
    return {
        "sessions": [session_out(s, viewer=request.user) for s in mark_people_in(page, request.user)],
        "next_cursor": _encode_cursor(page[-1]._activity, page[-1].id) if more else None,
    }


def _read_window(request: HttpRequest, session, rows) -> list:
    # The one read pipeline (`transcript_read.readable_window`), as the socket
    # snapshot runs it: a sensitive conversation reads back only for its own
    # people on its chat surfaces, and a site's widget gets no tool rows.
    return transcript_read.readable_window(
        session, rows, user=request.user,
        chat_surface=sensitivity.request_is_chat_surface(request),
        widget=delegation.acting_app(request) is not None)


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
    data = session_out(session, viewer=request.user)
    if data["you_are_in_it"] is False:
        asked = join_requests.latest_of(session, request.user)
        data["your_join_request"] = asked.status if asked is not None else ""
    rows, has_more, oldest = transcript_read.visible_transcript(session, full=full)
    rows = _read_window(request, session, rows)
    data["messages"] = [MessageOut.from_orm(m) for m in rows]
    data["has_more_before"] = has_more
    data["oldest_loaded_turn_index"] = oldest
    # Same reader as the WS snapshot, so opening a session over REST and over
    # the socket can never disagree about whether an agent is waiting.
    data["menu"] = serializers.pending_menu(session)
    data["subagents"] = serializers.session_subagents(session)
    data["turn_status"] = status_feed.status_for_session(session)
    data["my_role"] = access.role_for(request.user, session)
    data["can_add_people"] = access.can_add_people(request.user, session)
    data["audience"] = audience.summary(session, request.user)
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
    session = _session_or_404(request, session_id)
    binding = getattr(session, "runner_binding", None)
    turns_qs = staleness.driving_turns_of(session)
    session._turn_mode, session._turn_origin, session._turn_by_agent = feed.driving_turn(session)
    newest_claimed = (turns_qs.exclude(claimed_by=None).select_related("claimed_by")
                      .order_by("-claimed_at").first())
    session._turn_runner_name = newest_claimed.claimed_by.name if newest_claimed else None
    turns = list(
        turns_qs.select_related(
            "agent", "chat_session", "claimed_by", "enqueued_by", "pinned_runner",
            "requested_turn_mode_by", "raised_from_task", "initiator_user", "initiator_contact",
            "parent_turn__claimed_by",
        ).order_by("-created_at")[:SESSION_INFO_TURNS]
    )
    turn_access.redact(turns, request.user)
    return {
        "session": session_out(session, viewer=request.user),
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
    rows = _read_window(request, session, rows)
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


@router.post("/{session_id}/attach", response=StreamStateOut, summary="Attach a viewer (start live streaming)")
@gate("session-acl", kind="conversation")  # READ gate on a POST (viewer signal)
def attach_session(request: HttpRequest, session_id: uuid.UUID):
    """Count this viewer in, and ask the session's runner to stream it live.
    Only someone who may pull from that box (`access.may_pull`: its owner, or
    someone let in to write) starts streaming; anyone else who can read it is
    answered with the current state and asks nothing of the machine."""
    session = _session_or_404(request, session_id)
    if not access.may_pull(request.user, session):
        return {"streaming": transcript_ingest.stream_desired(session)}
    return {"streaming": transcript_ingest.attach_session(session)}


@router.post("/{session_id}/detach", response=StreamStateOut, summary="Detach a viewer (stop when last leaves)")
@gate("session-acl", kind="conversation")  # READ gate on a POST
def detach_session(request: HttpRequest, session_id: uuid.UUID):
    """The other half of `attach`: a no-op for a reader whose attach was one."""
    session = _session_or_404(request, session_id)
    if not access.may_pull(request.user, session):
        return {"streaming": transcript_ingest.stream_desired(session)}
    return {"streaming": transcript_ingest.detach_session(session)}


@router.post("/{session_id}/backfill", response=BackfillStateOut, summary="Request full history from the runner")
@gate("session-acl", kind="conversation")  # READ gate on a POST
def request_backfill(request: HttpRequest, session_id: uuid.UUID):
    """Ask the session's runner to ship its full history. Only someone who may
    pull from that box (`access.may_pull`); anyone else reads what is stored (403)."""
    session = _session_or_404(request, session_id)
    if not access.may_pull(request.user, session):
        raise ProblemError(403, "History is asked of a machine only by its owner",
                           detail=access.PULL_REFUSED)
    # A sensitive-data runner's session is refused (409): that box keeps its
    # transcripts to itself (`transcript_read.backfill_refused`).
    try:
        return {"status": transcript_read.request_backfill(session)}
    except transcript_read.BackfillRefusedError as exc:
        raise ProblemError(409, "History is not asked of a sensitive-data runner",
                           detail=str(exc)) from exc


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
    # the starter's call. An embedding site
    # acting for that user is refused — a widget is not a way to lift a chat out.
    # Built server-side from Message rows (`exports.build_markdown`); the runner's
    # raw transcript is never asked for.
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
