"""Django Ninja router for /api/canopy-sessions — live chat sessions.

Session-authed + workspace-membership gated. A "send" enqueues a session Turn;
in SP2a the stub executor runs it inline (the SP2b cloud runner will claim it
async instead — no API change when that lands).
"""
from __future__ import annotations

import base64
import datetime as dt
import uuid

from django.db.models import Max
from django.db.models.functions import Coalesce

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from ninja import File, Router
from ninja.errors import HttpError
from ninja.files import UploadedFile

from apps.harness import initiator as who
from apps.harness import provenance
from apps.harness import runner_requirements as rr
from apps.agents import services as agent_services
from apps.api.auth import session_auth
from apps.api.pagination import clamp_limit
from apps.workspaces import services as wsvc

from . import (
    access,
    attachment_storage,
    feed,
    page_actions,
    page_state,
    secrets,
    serializers,
    services,
    status_feed,
)
from . import activity as session_activity
from .models import Attachment, Session
from .schemas import (
    SessionExportOut,
    AttachmentOut,
    BackfillStateOut,
    MenuAnswerIn,
    HumanInputPageOut,
    MessageOut,
    MessagePageOut,
    PageActionInvokeIn,
    PageActionOut,
    PageActionResultIn,
    PageActionsDeclareIn,
    PageActionSpec,
    PageStateIn,
    PageStateOut,
    RunAgentInputIn,
    RunAgentInputOut,
    PlaceIn,
    ResetIn,
    ResetOut,
    ResetSummaryOut,
    SendIn,
    SendOut,
    SessionCreateIn,
    SessionDetailOut,
    ParticipantAddIn,
    ParticipantOut,
    SessionNotifyIn,
    SessionOut,
    SessionSearchPageOut,
    SessionSecretIn,
    SessionSecretOut,
    StreamStateOut,
    TransferDecisionIn,
    TransferRequestOut,
    TransferIn,
    TransferOut,
    TurnOutMinimal,
)

#: Reserved, server-owned metadata key: the embedding app a session was created
#: through. Named once so the writer (create) and the reader (list filter)
#: cannot drift onto different spellings.
EMBED_APP_KEY = "embed_app"

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


def _out(session: Session, *, reply: bool = False, viewer=None) -> dict:
    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    waiting_on_you = serializers.pending_menu(session) is not None
    last_reply, agent_spoke_last = services.last_reply_of(session, binding) if reply else ("", False)
    # The reply is whole (untrimmed), so carry it only where the feed renders it:
    # sessions where it is the person's turn. That is what bounds the payload.
    if not (agent_spoke_last or waiting_on_you):
        last_reply = ""
    runner = binding.runner if (binding and binding.runner_id) else None
    running = services.is_session_running(binding)
    turn_mode = getattr(session, "_turn_mode", None) or ""
    turn_origin = getattr(session, "_turn_origin", None) or ""
    # The supervisor feed's verdict for THIS caller — the one rule, which the
    # pushes ask too (canopy_sessions.feed). Only when the feed asked (`reply`).
    feed_status = (
        feed.status(
            viewer, session, waiting_on_you=waiting_on_you, agent_spoke_last=agent_spoke_last,
            running=running, turn_mode=turn_mode, turn_origin=turn_origin,
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
        "opening": services.opening_of(session),
        "created_at": session.created_at,
        # When it last DID something (binding > newest message > created).
        "last_activity_at": services.last_activity_at(session, binding),
        # --- liveness (Plan 4): one shape, computed from the binding ---
        "origin": session.origin,
        "running": running,
        "runner_name": runner.name if runner else None,
        "runner_location": runner.location if runner else None,
        # See SessionOut.runner_online: an embedder's delegated user cannot list
        # runners, so the session payload is where they learn their bound runner
        # went away. None when unbound — nothing to be offline.
        "runner_online": _runner_online(runner),
        "runner_status": _runner_status(runner),
        "session_key": binding.session_key if binding else "",
        # Is this session blocked on a human? A bool on the LIST (the menu
        # itself rides the detail read) — a list carrying every session's full
        # dialog would pay for N sets of options to render one badge each. It
        # answers the thing the list could not: a waiting agent and an idle one
        # look identical, which is why spark read as "the session stopped".
        "waiting_on_you": waiting_on_you,
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
    }


def _visible_slugs(request: HttpRequest) -> set[str]:
    pinned = getattr(request, "workspace_slug", None)
    return {pinned} if pinned else set(wsvc.user_workspace_slugs(request.user))


def _site_scoped(request: HttpRequest, qs):
    """`site ∩ user`: when a connected site is acting, only its own agents'
    sessions exist (apps/tokens/delegation.py). No-op for the person themself."""
    from apps.tokens import delegation

    offered = delegation.offered_for(request)
    return qs if offered is None else qs.filter(agent_id__in=offered)


def _session_or_404(request: HttpRequest, session_id: uuid.UUID, *, write: bool = False) -> Session:
    # One authority (`access`): the tenant, then who within it — the same rule
    # the list, the chat socket and attachments apply. `write=True` also needs
    # an owner/editor role; a viewer gets 403, since they can already see it.
    session = get_object_or_404(
        _site_scoped(request, access.readable_sessions(request.user,
                                                       workspace_slugs=_visible_slugs(request)))
        .select_related("agent", "created_by", "runner_binding", "runner_binding__runner")
        .annotate(_last_msg_at=Max("messages__created_at")),
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
def create_session(request: HttpRequest, payload: SessionCreateIn):
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
    from apps.tokens import delegation

    offered = delegation.offered_for(request)
    if offered is not None and (agent is None or agent.pk not in offered):
        raise HttpError(404, f"agent '{payload.agent_slug}' not found in this workspace")
    try:
        metadata = services.host_metadata(payload.metadata)
    except ValueError as exc:
        raise HttpError(422, str(exc))
    # `embed_app` is SERVER-OWNED: it records which registered embedding app
    # created this session, and it is taken from the delegated token rather
    # than the request body, so one host's widget cannot create or list under
    # another host's name. A client-supplied value is discarded on every auth
    # path — including a browser session, where there is no app at all, because
    # otherwise the gate would only be as strong as the auth path a caller
    # chose to use.
    #
    # `origin_key` is deliberately NOT touched. It is a finer, host-chosen
    # scope (ace-web derives one per ace workspace from a membership-checked
    # path) and it answers a different question; overriding it would break that
    # for no gain. See tests/test_embed_session_provenance.py.
    acting_app = getattr(request, "delegated_app", None)
    if acting_app is not None:
        metadata[EMBED_APP_KEY] = acting_app.name
        # Server-owned like `embed_app`: the site's runner requirements (ZDR)
        # ride the token, never the body.
        reqs = getattr(request, "runner_requirements", ())
        if reqs:
            metadata["runner_requirements"] = list(reqs)
    if payload.runner_id and agent is not None:
        from apps.agents.access import may_pin_runner
        from apps.harness.models import Runner

        # The stash is consumed on the first send WITHOUT asking who may pin
        # (services._resolve_placement), so the pin is decided here, by the same
        # rule a pinned dispatch and an explicit placement apply: the agent's
        # admins may pin any box, anyone else only a box they administer. An id
        # that names no runner is left as before — the first send ignores it.
        # The message names no runner: the id may be one the caller cannot see.
        requested = Runner.objects.filter(pk=payload.runner_id).first()
        if requested is not None and not may_pin_runner(request.user, agent, requested):
            raise HttpError(
                403,
                f"running {agent.slug} on a chosen runner is for the agent's owner or "
                "admins, or for someone who administers that runner; start the chat "
                "without a runner and the agent's routing places it",
            )
    if payload.runner_id:
        # Directed new chat: stashed for the session's first send to pin onto
        # (as long as it's still unbound at that point) — see services.send_message.
        metadata["requested_runner_id"] = str(payload.runner_id)
    session = services.create_session(
        workspace=workspace, created_by=request.user, agent=agent,
        project=payload.project, title=payload.title, metadata=metadata,
        parent=payload.parent,
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
    until: dt.datetime | None = None,
):
    """Every session the caller may see, narrowed by the list filters, with
    `_last_msg_at`, `_activity` (= `last_activity_at`, computed in SQL) and
    `_opening` annotated. Unordered — each route orders it its own way. The
    list and the search route share it so a filter (and, above all, the
    delegated-app scoping) cannot exist on one and not the other."""
    from django.db.models import Max, Q

    if state not in ("active", "archived", "all"):
        raise HttpError(422, "state must be one of: active, archived, all")

    slugs = _visible_slugs(request)
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
    if source:
        rows = rows.filter(metadata__source=source)
    if origin_key:
        rows = rows.filter(metadata__origin_key=origin_key)
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

    # "The conversations I had on THIS page."
    #
    # Matched against the session's declared page state (page_state.py), which
    # is the page's own account of what it was showing — `resource` for the kind
    # of thing, `path` for the exact screen. A session that never declared a
    # page matches neither, which is correct: it was not had on any page we know
    # of, and guessing from the title would be canopy inventing provenance.
    if resource:
        rows = rows.filter(page_state__resource=resource)
    if page_path:
        rows = rows.filter(page_state__path=page_path)
    # The name an Open Sessions card shows for a runner session (an emdash task
    # name, or a cloud session's Claude UUID) — so it can be looked up by what
    # a person reads off the card, not only by the session id they never see.
    if session_key:
        rows = rows.filter(runner_binding__session_key=session_key)
    if opp_slug:
        rows = rows.filter(metadata__opp_slug=opp_slug)
    if opp_run_id:
        rows = rows.filter(metadata__opp_run_id=opp_run_id)
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
    rows = services.with_opening(rows.annotate(_last_msg_at=Max("messages__created_at"))).distinct()
    # The SAME rule as services.last_activity_at (binding > newest message >
    # created), in SQL, so a window and a cursor can be applied to it.
    rows = rows.annotate(_activity=Coalesce(
        "runner_binding__last_interacted_at", "_last_msg_at", "created_at"))
    if since is not None:
        rows = rows.filter(_activity__gte=_aware(since))
    if until is not None:
        rows = rows.filter(_activity__lt=_aware(until))
    unseen = services.unseen_q()   # defined once in staleness.py; see Step 3
    if state == "active":
        rows = rows.filter(status=Session.ACTIVE).exclude(unseen)
    elif state == "archived":
        rows = rows.filter(Q(status=Session.ARCHIVED) | unseen)
    return rows


@router.get("/", response=list[SessionOut], summary="List sessions (web + runner-discovered)")
def list_sessions(
    request: HttpRequest, state: str = "active", limit: int = 200,
    source: str = "", opp_slug: str = "", opp_run_id: str = "",
    origin_key: str = "", embed_app: str = "",
    resource: str = "", page_path: str = "", reply: bool = False,
    session_key: str = "", q: str = "", repo: str = "", branch: str = "", pr: str = "",
    since: dt.datetime | None = None, until: dt.datetime | None = None,
):
    """The sessions you can see: waiting on you first, then running, then most
    recent activity — at most `limit` (≤ 500) of them, with no paging.

    Filters: `q` (title or session_key contains), `repo` (the repo it ran in or
    worked on — a name or owner/name), `branch` (any branch its transcript saw),
    `pr` (a PR it created or asked to merge — N, owner/name#N or a URL),
    `since` / `until` (last activity in [since, until)). To reach EVERY session
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
        branch=branch, pr=pr, since=since, until=until,
    ).order_by("-created_at")

    # Every row says which mode drove it — the list shows it on each card, so an
    # auto session that just ran and a manual one waiting on you can be told apart.
    rows = services.with_driving_turn(rows)
    if reply:
        rows = services.with_last_reply(rows)
    out = [_out(s, reply=reply, viewer=request.user) for s in rows]
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
    out.sort(key=lambda r: (not r["waiting_on_you"], not r["running"],
                            -(r["last_activity_at"].timestamp())))
    # Clamp AFTER the sort, never as a queryset slice: the queryset is ordered by
    # -created_at, so slicing it could drop the running session this sort exists to
    # float. `state=active` already bounds the set; this is a payload backstop.
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
    rows = services.with_driving_turn(rows.order_by("-_activity", "-id"))
    page = list(rows[: limit + 1])
    more = len(page) > limit
    page = page[:limit]
    return {
        "sessions": [_out(s, viewer=request.user) for s in page],
        "next_cursor": _encode_cursor(page[-1]._activity, page[-1].id) if more else None,
    }


# Declared BEFORE /{session_id}: Django resolves in declaration order and
# "reset" would otherwise match the session-id pattern and 405 on its GET-only
# view. Any future collection-level route belongs above here too.
@router.post("/reset", response=ResetSummaryOut, summary="Reset every visible session")
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
        request, access.readable_sessions(request.user, workspace_slugs=_visible_slugs(request)))
    rows = [
        s for s in readable.select_related("created_by", "runner_binding", "runner_binding__runner")
        .order_by("created_at")
        if access.can_write(request.user, s)
    ]
    return services.reset_sessions(
        rows, prune_ghosts=payload.prune_ghosts, dry_run=payload.dry_run
    )


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
            "index_offset": binding.index_offset, "turn_id": str(turn.id),
        }
    return out


def _transfer_request_or_404(request: HttpRequest, request_id: uuid.UUID):
    from . import transfer_requests

    req = transfer_requests.visible_to(request.user).filter(pk=request_id).first()
    if req is None:
        raise HttpError(404, "transfer request not found")
    return req


@router.get("/transfer-requests", response=list[TransferRequestOut],
            summary="Transfer requests waiting on you, or that you made")
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


@router.get("/{session_id}", response=SessionDetailOut, summary="Get a session + transcript tail")
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
    data = _out(session)
    rows, has_more, oldest = services.visible_transcript(session, full=full)
    from apps.tokens import delegation, mcp_apps_views

    mcp_apps_views.annotate(session, rows)

    if delegation.acting_app(request) is not None:
        rows = services.for_widget(rows)
    data["messages"] = [MessageOut.from_orm(m) for m in rows]
    data["has_more_before"] = has_more
    data["oldest_loaded_turn_index"] = oldest
    # Same reader as the WS snapshot, so opening a session over REST and over
    # the socket can never disagree about whether an agent is waiting.
    data["menu"] = serializers.pending_menu(session)
    data["turn_status"] = status_feed.status_for_session(session)
    data["my_role"] = access.role_for(request.user, session)
    return data


@router.get(
    "/{session_id}/messages",
    response=MessagePageOut,
    summary="Load earlier transcript (scroll-back)",
)
def list_messages(
    request: HttpRequest,
    session_id: uuid.UUID,
    before: int,
    limit: int = services.SCROLLBACK_PAGE_DEFAULT,
):
    # Cursor-based backward paging: the window of `limit` messages immediately
    # older than `before` (a turn_index), chronological, + whether older exists.
    # Clamp here (not in services.messages_before, which stays a pure helper) —
    # an unclamped `?limit=-1`/`0` hits `queryset[:limit]` and raises
    # ValueError("Negative indexing is not supported"), surfacing as a 500.
    session = _session_or_404(request, session_id)
    limit = clamp_limit(limit)
    rows, has_more = services.messages_before(session, before=before, limit=limit)
    from apps.tokens import delegation, mcp_apps_views

    mcp_apps_views.annotate(session, rows)

    if delegation.acting_app(request) is not None:
        rows = services.for_widget(rows)
    return {
        "messages": [MessageOut.from_orm(m) for m in rows],
        "has_more_before": has_more,
    }


@router.get(
    "/{session_id}/human-inputs",
    response=HumanInputPageOut,
    summary="What people typed into a session (cursor)",
)
def list_human_inputs(
    request: HttpRequest,
    session_id: uuid.UUID,
    after: int | None = None,
    limit: int = services.SCROLLBACK_PAGE_DEFAULT,
):
    """Only the human side of a session: what a person typed, oldest first,
    `limit` (≤ 500) at a time. Pass the response's `next_cursor` back as
    `after` for the next page; it is null when there is no more.

    Excluded, though some are stored on the user's side of a transcript: tool
    results, the agent's output, system rows, harness records (task
    notifications, system reminders, local command output) and the prompts
    PROGRAMS delivered — a scheduled or email turn's `/agent:turn …`, an API
    caller. A line attributed to a person (`author`) is always kept.

    A session with no durable transcript yet (a local runner session before its
    backfill) answers from the runner's recent tail, `source: "tail"`, unpaged.
    Same access rule as `GET /{id}`, and `session_id` may likewise be the
    runner's `session_key`.
    """
    try:
        session = _session_or_404(request, session_id)
    except Http404:
        session = _session_by_key_or_404(request, session_id)
    rows, next_cursor, source = services.human_inputs(
        session, after=after, limit=clamp_limit(limit))
    return {
        "messages": [MessageOut.from_orm(m) for m in rows],
        "next_cursor": next_cursor,
        "source": source,
    }


@router.post("/{session_id}/archive", response=SessionOut, summary="Archive a session")
def archive_session(request: HttpRequest, session_id: uuid.UUID):
    """Retire a session by hand. The escape hatch for a web chat — no runner will ever
    report it archived — and for force-retiring a row without touching emdash.
    Idempotent, and never destructive: /unarchive brings it straight back."""
    return _set_status(request, session_id, Session.ARCHIVED)


@router.post("/{session_id}/reset", response=ResetOut, summary="Reset a session from its transcript")
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
def unarchive_session(request: HttpRequest, session_id: uuid.UUID):
    """Undo an archive. Note this clears only the WRITTEN half: a runner session that
    is also past SESSION_STALE_AFTER stays out of `state=active` until its runner
    reports it again, because that half is derived on every read."""
    return _set_status(request, session_id, Session.ACTIVE)


@router.put("/{session_id}/notify", response=SessionOut, summary="Set a session's completion notifications")
def set_session_notify(request: HttpRequest, session_id: uuid.UUID, payload: SessionNotifyIn):
    """`every_completion: true` pushes a notification each time a turn in this
    session finishes. Off (the default), one notification is sent once the session
    has been quiet for your chosen number of minutes."""
    session = _session_or_404(request, session_id, write=True)
    if session.notify_every_completion != payload.every_completion:
        session.notify_every_completion = payload.every_completion
        session.save(update_fields=["notify_every_completion", "updated_at"])
    return _out(session)


def _participants(session: Session) -> list[dict]:
    rows = session.participants.select_related("user").order_by("created_at")
    return [serializers.participant_dto(p) for p in rows]


@router.get("/{session_id}/participants", response=list[ParticipantOut],
            summary="Who has been given this chat")
def list_participants(request: HttpRequest, session_id: uuid.UUID):
    """Everyone explicitly in this conversation, with their role."""
    return _participants(_session_or_404(request, session_id))


@router.post("/{session_id}/participants", response=list[ParticipantOut],
             summary="Give a teammate this chat")
def add_participant(request: HttpRequest, session_id: uuid.UUID, payload: ParticipantAddIn):
    """Owner only. The teammate must already be a member of the chat's
    workspace; adding someone again changes their role."""
    from django.contrib.auth import get_user_model

    from .models import SessionParticipant

    session = _session_or_404(request, session_id)
    if not access.can_share(request.user, session):
        raise HttpError(403, "only the chat's owner can share it")
    target = get_user_model().objects.filter(email__iexact=payload.email.strip()).first()
    if target is None or not wsvc.is_member(target, session.workspace_id):
        raise HttpError(404, "nobody with that email is in this workspace")
    if target.pk == session.created_by_id:
        raise HttpError(409, "that person owns this chat")
    SessionParticipant.objects.update_or_create(
        session=session, user=target, defaults={"role": payload.role})
    return _participants(session)


@router.delete("/{session_id}/participants/{user_id}", response=list[ParticipantOut],
               summary="Take a chat away from someone")
def remove_participant(request: HttpRequest, session_id: uuid.UUID, user_id: int):
    """The owner can remove anyone but themselves; anyone can remove themselves."""
    from .models import SessionParticipant

    session = _session_or_404(request, session_id)
    if user_id == session.created_by_id:
        raise HttpError(409, "the chat's owner cannot be removed")
    if user_id != request.user.pk and not access.can_share(request.user, session):
        raise HttpError(403, "only the chat's owner can remove someone else")
    SessionParticipant.objects.filter(session=session, user_id=user_id).delete()
    return _participants(session)


@router.post("/{session_id}/send", response=SendOut, summary="Send a message")
def send(request: HttpRequest, session_id: uuid.UUID, payload: SendIn):
    session = _session_or_404(request, session_id, write=True)
    if not payload.text.strip():
        raise HttpError(422, "message text is required")
    # A site's token carries its runner requirements (ZDR); stamp them before
    # the send so the turn is routed under them. A union — never lifts one.
    services.add_runner_requirements(session, getattr(request, "runner_requirements", ()))
    try:
        message, turn = services.send_message(
            session=session, text=payload.text, user=request.user,
            # Capped the same way the WS path caps it (consumers.py's
            # `chat.send` handler) — the schema field itself stays an
            # unbounded `str` so `generated.ts` needs no regen; the length
            # limit is enforced where the id is actually used.
            client_id=payload.client_id[:100], placement=payload.placement,
            origin=payload.origin,
            initiator=who.for_request(request, via=who.channel(request, "chat")),
            parent=payload.parent,
            origin_ref={"clear_prompt": True} if payload.clear_prompt else None,
        )
    except ValueError as exc:
        raise HttpError(422, str(exc))
    # The socket path commits the sender's server draft; this one did not, so a
    # line sent here stayed in the draft and came back on every later connect
    # as "<you> is typing". Clear it and tell the room, same frame the socket
    # sends (`draft.updated`, rendered per recipient by the consumer).
    services.clear_draft_after_http_send(session, request.user, payload.text)
    # Dev/test: run the stub inline. Production: leave it queued for a cloud runner.
    services.maybe_execute_inline(turn)
    return {"turn_id": turn.id if turn else None, "message": MessageOut.from_orm(message)}


@router.post(
    "/{session_id}/place", response=TurnOutMinimal,
    summary="Re-pin a session's oldest queued turn to a runner",
)
def place(request: HttpRequest, session_id: uuid.UUID, payload: PlaceIn):
    # The chat banner's after-the-fact directed-placement decision (vs. `runner_id`
    # on create / `placement` on send, which only apply to a turn at enqueue time).
    session = _session_or_404(request, session_id, write=True)
    try:
        turn = services.place_queued_turn(session=session, placement=payload.placement)
    except LookupError as exc:
        raise HttpError(404, str(exc))
    except ValueError as exc:
        raise HttpError(422, str(exc))
    return turn


@router.post(
    "/{session_id}/transfer", response=TransferOut,
    summary="Move a live session onto another runner",
)
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
    binding, turn = moved
    return {
        "session_id": str(session.id),
        "runner": binding.runner.name if binding.runner_id else "",
        "transferred_from": (
            binding.transferred_from.name if binding.transferred_from_id else ""
        ),
        "index_offset": binding.index_offset,
        "turn_id": str(turn.id),
        "status": "moved",
        "request_id": req.id,
    }


@router.post(
    "/{session_id}/answer-menu", response=dict,
    summary="Answer the dialog an agent is blocked on",
)
def answer_menu(request: HttpRequest, session_id: uuid.UUID, payload: MenuAnswerIn):
    """Approve or refuse a permission prompt from the web.

    A refusal is a 200 with `ok:false` and a stable reason, not a 4xx: the dialog
    can go stale between the phone rendering it and a thumb reaching it, and the
    runner can go offline in between — both ordinary, neither a client error.
    Same shape `reset` uses for the same reason.
    """
    session = _session_or_404(request, session_id, write=True)
    outcome = services.answer_menu(session=session, option=payload.option,
                                   selections=payload.selections,
                                   texts=payload.texts)
    return {"ok": outcome == "sent", "reason": "" if outcome == "sent" else outcome}


@router.post("/{session_id}/close", response=dict, summary="Close a session for good")
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
    outcome = services.close_session(session=session)
    ok = outcome in ("closing", "closed")
    return {"ok": ok, "closing": outcome == "closing", "reason": "" if ok else outcome}


@router.post("/{session_id}/stop", response=dict,
             summary="Stop this session: cancel its open turns, or interrupt its agent")
def stop_session_turn(request: HttpRequest, session_id: uuid.UUID):
    """Stop whatever this session is doing — the same stop as the web UI's button.
    Returns `cancelled` (an open turn was cancelled), `interrupted` (no turn was
    open, so the session's runner was asked to interrupt the agent) and `route`."""
    # canopy-web#1226: this used to cancel turns only. An agent/project turn is
    # already DONE once its prompt is delivered, so a stop from MCP or a script
    # found nothing to cancel and the agent worked on. services.stop_session is the
    # one stop the websocket uses too: turns first, then the session interrupt.
    session = _session_or_404(request, session_id, write=True)
    route = services.stop_session(session, by=services.person_name(request.user))
    return {"cancelled": route == "turns", "interrupted": route == "session", "route": route}


@router.post("/{session_id}/attach", response=StreamStateOut, summary="Attach a viewer (start live streaming)")
def attach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": services.attach_session(session)}


@router.post("/{session_id}/detach", response=StreamStateOut, summary="Detach a viewer (stop when last leaves)")
def detach_session(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"streaming": services.detach_session(session)}


@router.post("/{session_id}/backfill", response=BackfillStateOut, summary="Request full history from the runner")
def request_backfill(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return {"status": services.request_backfill(session)}


@router.post(
    "/{session_id}/attachments",
    response={201: AttachmentOut},
    summary="Upload an attachment for this session",
)
def upload_attachment(
    request: HttpRequest, session_id: uuid.UUID, file: UploadedFile = File(...)
):
    """Store the bytes and return an id the caller passes to /send.

    UNBOUND on purpose (`message` null): the composer uploads while you are
    still typing, so the message it belongs to does not exist yet. Sending binds
    it. That ordering is also what lets the UI show a thumbnail before send.
    """
    session = _session_or_404(request, session_id, write=True)   # membership gate: non-member -> 404
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
def attachment_content(request: HttpRequest, attachment_id: uuid.UUID):
    """The bytes, for both readers: the browser rendering a thumbnail and the
    runner downloading into the agent's workspace (which authenticates with a
    PAT, resolved upstream into request.user like any other caller).

    Gated on who can read the session, not on who uploaded it — a session is
    multiplayer, so a teammate who can read it must see what was shared in it.
    """
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
    if not attachment_storage.is_configured():
        raise HttpError(503, "attachments are not configured on this deployment")

    stored = attachment_storage.get(attachment.storage_key)
    response = HttpResponse(stored.body, content_type=stored.content_type)
    # inline: the browser renders it rather than downloading. filename is already
    # sanitised at upload, so it is safe in the header.
    response["Content-Disposition"] = f'inline; filename="{attachment.filename}"'
    # Served inline from canopy's own origin, and the type allowlist is an env
    # setting — so if it ever admits a document type (HTML, SVG), the bytes still
    # run in an opaque origin, never as the viewer. Same rule as walkthrough
    # content (apps/walkthroughs/streaming.py::SANDBOX_CSP); harmless on an image.
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@router.delete("/attachments/{attachment_id}", response={204: None})
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
def declare_page_actions(request: HttpRequest, session_id: uuid.UUID,
                         payload: PageActionsDeclareIn) -> list[PageActionSpec]:
    """Called by the page itself as it mounts, and whenever its actions change.

    Replaces the declaration wholesale — see `set_declared_actions` for why
    merging would leave the agent able to call into a page the user has left.
    """
    session = _session_or_404(request, session_id, write=True)
    page_actions.set_declared_actions(
        session, [a.dict() for a in payload.actions]
    )
    return [PageActionSpec(**a) for a in page_actions.declared_actions(session)]


@router.get("/{session_id}/page-actions", response=list[PageActionSpec],
            summary="What the attached page can do")
def list_page_actions(request: HttpRequest, session_id: uuid.UUID) -> list[PageActionSpec]:
    """How the agent discovers its options. An empty list means no page is
    attached — not that the page can do nothing."""
    session = _session_or_404(request, session_id)
    return [PageActionSpec(**a) for a in page_actions.declared_actions(session)]


@router.put("/{session_id}/page-state", response=PageStateOut,
            summary="Declare what the attached page is showing")
def declare_page_state(request: HttpRequest, session_id: uuid.UUID,
                       payload: PageStateIn) -> PageStateOut:
    """Called by the page as it mounts and whenever its view changes.

    Replaces the declaration wholesale. A state larger than the server's cap is
    rejected with `too_large`: send the selection (ids, filters) and the tool
    that resolves it, not the rows themselves.
    """
    session = _session_or_404(request, session_id, write=True)
    try:
        stored = page_state.set_page_state(session, payload.state)
    except page_state.PageStateError as exc:
        # 422: well-formed request, unacceptable CONTENT — the page must change
        # what it sends, which is a different fix from retrying.
        raise HttpError(422, f"{exc.code}: {exc.message}")
    return PageStateOut(state=stored, version=int(stored.get("version") or 0))


@router.put("/{session_id}/run-input", response=RunAgentInputOut,
            summary="Declare the page in AG-UI's own shape")
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
def read_page_state(request: HttpRequest, session_id: uuid.UUID) -> PageStateOut:
    """How a surface other than the agent's MCP tool reads the current view."""
    session = _session_or_404(request, session_id)
    stored = page_state.current_page_state(session)
    return PageStateOut(state=stored, version=int(stored.get("version") or 0))


@router.post("/{session_id}/page-actions/invoke", response=PageActionOut,
             summary="Ask the attached page to run an action")
def invoke_page_action(request: HttpRequest, session_id: uuid.UUID,
                       payload: PageActionInvokeIn) -> PageActionOut:
    """Blocks until the page answers, or refuses with a reason.

    Every non-success is an error with a `code` the caller can branch on
    (`no_page`, `unknown_action`, `bad_arguments`, `timeout`, `refused`) — a
    caller must never be able to read "the tab was closed" as "done".
    """
    session = _session_or_404(request, session_id, write=True)
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
def resolve_page_action(request: HttpRequest, session_id: uuid.UUID, action_id: uuid.UUID,
                        payload: PageActionResultIn) -> PageActionOut:
    """Posted by the page after it runs the callback.

    Membership-gated like every other by-id read, and scoped to the session, so
    one page cannot resolve another's action.
    """
    session = _session_or_404(request, session_id, write=True)
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
def list_secrets(request: HttpRequest, session_id: uuid.UUID):
    session = _session_or_404(request, session_id)
    return [_secret_out(r) for r in secrets.live_secrets(session)]


@router.delete("/{session_id}/secrets/{name}", response={204: None},
               summary="Forget a shared secret")
def delete_secret(request: HttpRequest, session_id: uuid.UUID, name: str):
    session = _session_or_404(request, session_id, write=True)
    session.secrets.filter(name=name).delete()
    return 204, None


@router.get("/{session_id}/export", response=SessionExportOut,
            summary="Export a session's conversation to pick it up in your own Claude")
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
    from apps.tokens import delegation

    from . import exports

    if delegation.acting_app(request) is not None:
        raise HttpError(403, "session export is not available to an embedded site")
    session = _session_or_404(request, session_id)
    if session.created_by_id is None or session.created_by_id != request.user.pk:
        raise HttpError(403, "only the person who started this session can export it")
    markdown, count = exports.build_markdown(session)
    if not count:
        raise HttpError(409, "this session has no conversation to export yet")
    return {"session_id": session.id, "title": session.title or "",
            "message_count": count, "markdown": markdown}
