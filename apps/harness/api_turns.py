"""/api/harness turn routes: enqueue, list, read (events, messages, transcript),
caller context, start/finish/cancel, and the stuck-turn warning.

Registered on the harness router (`api.router`), in the order api.py imports
this module, so the schema and the MCP tool names are unchanged."""
from __future__ import annotations

import logging
import uuid

from django.db.models import Q
from django.http import HttpRequest, StreamingHttpResponse
from ninja import Status
from ninja.errors import HttpError

from apps.agents.gates import agent_for_read
from apps.agents.models import Agent
from apps.api.errors import ProblemError
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import claim, ledger, services, session_sync, turn_asker, turn_gates

# The one harness router and the helpers every harness route shares; api.py
# imports this module after defining them.
from .api import (  # noqa: E402
    ALLOWED_EVENT_KINDS,
    TRANSCRIPT_APPEND_MAX_BYTES,
    agents_own_thread_q,
    reporting_turn_or_404,
    router,
    site_turn_q,
    turn_content_or_404,
    turn_or_404,
    visible_sessions,
)
from .models import (
    Turn,
    agent_turns_q,
)
from .schemas import (
    CallerContextOut,
    EmdashSessionOut,
    TranscriptAppendIn,
    TranscriptAppendOut,
    TurnEventCountOut,
    TurnEventsIn,
    TurnEventsOut,
    TurnFinishIn,
    TurnIn,
    TurnMessagesOut,
    TurnOut,
    TurnStartIn,
    UnclaimableTurnOut,
)

logger = logging.getLogger(__name__)

#: Published operationIds keep the label of the module these routes moved out of
#: (apps/api/api.py `_app_label`): an operationId is an MCP tool name.
OPERATION_LABEL = "harness"


@router.get("/turns/unclaimable", response=list[UnclaimableTurnOut],
            summary="Queued turns no online runner can claim")
@gate("member", "session-acl", "turn-content")  # prompt blanked unless turn-content
def list_unclaimable_turns(request: HttpRequest):
    """A queued turn addressed to an agent/repo nothing declares sits forever with
    no signal (one sat 12h). Surfacing it turns a silent stall into a warning."""
    # A list: the read rule (pinned, or every membership in ONE tenant tree;
    # a caller in two trees who names none gets a 422).
    ws_slugs = wsvc.request_workspace_slugs(request)
    site_q = site_turn_q(request)
    rows = (claim.unclaimable_queued_turns(request.user, ws_slugs=ws_slugs, turn_q=site_q)
            if site_q is not None else
            claim.unclaimable_queued_turns(request.user, ws_slugs=ws_slugs))
    # Each row says whether the caller may cancel it, so the warning offers the
    # one fix that always works right where it is shown.
    turns = Turn.objects.select_related("agent", "chat_session").in_bulk([r["turn_id"] for r in rows])
    for r in rows:
        t = turns.get(uuid.UUID(r["turn_id"]))
        r["can_cancel"] = t is not None and not _cancel_refusal(request.user, t)
    return rows


@router.post("/turns/", response={200: TurnOut, 201: TurnOut})
@gate("agent.work", kind="turn")  # agent and project turns alike
def enqueue_turn(request: HttpRequest, payload: TurnIn):
    """Queue a turn for a runner. Started from inside a canopy session? Pass
    `parent={turn, session}` (the caller-context hook prints both ids) so the
    session the runner makes for it records where it came from and includes that
    conversation's people."""
    turn_gates.check_shape(payload)
    origin_ref = turn_gates.capped_origin_ref(request, payload)
    agent, workspace = turn_gates.resolve_target(request, payload)
    turn_gates.check_email_poster(request, payload, agent)
    pinned = turn_gates.resolve_pin(request, payload, agent)
    initiator = turn_gates.check_asker(request, payload, agent)
    # An agent dispatching on behalf of the person in its turn (turn_asker): the record
    # says so, and that person is who asked for its mode (the claim re-checks them).
    on_behalf = turn_asker.of(request.user)
    if on_behalf is not None:
        origin_ref["on_behalf_of"] = on_behalf.record()

    turn, created = services.enqueue_turn(
        agent=agent,
        project=payload.project,
        workspace=workspace,
        origin=payload.origin,
        idempotency_key=payload.idempotency_key,
        prompt=payload.prompt,
        origin_ref=origin_ref,
        routing=payload.routing,
        enqueued_by=request.user,  # the human launching a manual / composer turn
        pinned_runner=pinned,
        # The CALLER is the asker for every postable origin except email, which a
        # runner posts on a stranger's behalf — enqueue_turn names that sender
        # itself. Passing the caller for email would record the runner's owner as
        # the person who wrote in.
        initiator=initiator,
        requested_turn_mode=payload.turn_mode or "",
        requested_turn_mode_by=turn_asker.subject(request.user) if payload.turn_mode else None,
        # What the caller was running inside (overrides X-Canopy-Parent-*).
        parent=payload.parent,
    )
    from . import turn_access

    # Through the same content gate the get/list routes use: a replay hands back
    # an existing row, and the caller sees of it only what they may read.
    return Status(201 if created else 200, turn_access.redact([turn], request.user)[0])


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
        # TurnOut.initiator names a system account (initiator.describe).
        "initiator_user__system_account",
        "pinned_runner", "requested_turn_mode_by",
        # A chat turn's session and its agent are read per row by TurnOut.
        "chat_session", "chat_session__agent",
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


@router.get("/turns/", response=list[TurnOut])
@gate("member", "session-acl", "turn-content")  # session turns by chat ACL; redacted
def list_turns(
    request: HttpRequest,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 100,
    huddle: str | None = None,
    parent_turn: uuid.UUID | None = None,
):
    # `?agent=` names one agent, so the list cannot mix organisations: read it
    # within that agent's tenant tree and need no pin (fleet surveys — Ada's
    # `conduct`, `canopy agent health` — ask this per agent, unpinned). Without
    # it the read rule applies (`visible_turns_qs`).
    pinned = getattr(request, "workspace_slug", None)
    qs = None if (agent and not pinned) else visible_turns_qs(request)
    if agent:
        # Resolve the TARGET before filtering. The tenant filter below would
        # otherwise express a permission denial as an empty list — 200 [] — which
        # is indistinguishable from "this agent has never run", while the sibling
        # route /api/agents/<slug>/tasks/ answers the same denial with 404.
        #
        # That divergence is load-bearing, not cosmetic: agents legitimately hold
        # different permission sets, so a fleet survey routinely asks about agents
        # it cannot see, and every one of them read back as healthy-and-idle. Ada's
        # `conduct` reads this endpoint per agent to spot stuck turns.
        # (`agent_health` is incidentally safe — it resolves /api/agents/<slug>/
        # first, which already 404s.)
        #
        # 404 rather than 403, and the same 404 for a typo, so the endpoint cannot
        # be used to enumerate which tenants' agents exist (see agent_for_read).
        target = agent_for_read(request, agent)
        if qs is None:
            qs = visible_turns_qs(
                request, slugs=wsvc.tree_scope(request.user, target.workspace_id))
        # An email or Slack thread targets a SESSION, not the agent (enqueue_turn
        # converts it — the XOR check constraint allows only one), so the agent
        # sits on `chat_session.agent`. Filtering on `agent` alone dropped every
        # one of them: `?agent=ace` listed no ACE email turn after 2026-09-10, for
        # every caller, and hal's routing audit read that as "0 turns" (#1087).
        qs = qs.filter(agent_turns_q(target))
    if status:
        qs = qs.filter(status__in=status.split(","))
    # A huddle's turns (canopy `huddle`): its anchor and every round carry
    # `origin_ref.huddle`; a round's `parent_turn` is the anchor.
    if huddle:
        qs = qs.filter(origin_ref__huddle=huddle)
    if parent_turn:
        qs = qs.filter(parent_turn_id=parent_turn)
    limit = max(1, min(limit, 200))  # clamp; default 100 keeps existing callers unchanged
    from . import turn_access

    # Everyone sees THAT a turn ran; its prompt is a log (turn_access).
    return turn_access.redact(list(qs[:limit]), request.user)  # filter BEFORE slicing


@router.get("/sessions", response=list[EmdashSessionOut])
@gate("member", "session-acl")
def list_sessions(request: HttpRequest):
    """Open emdash sessions the caller can see — across their workspaces, live runners
    only, newest-first. Drives the phone's Open Sessions list."""
    from apps.embedding import delegation

    rows = session_sync.list_visible_sessions(request.user, wsvc.request_workspace_slugs(request))
    offered = delegation.offered_for(request)
    if offered is None:
        return rows
    slugs = set(Agent.objects.filter(pk__in=offered).values_list("slug", flat=True))
    return [r for r in rows if r.agent in slugs]


@router.get("/turns/{turn_id}", response=TurnOut)
@gate("member", "session-acl", "turn-content")  # redacted unless turn-content
def get_turn(request: HttpRequest, turn_id: uuid.UUID):
    from . import turn_access

    return turn_access.redact([turn_or_404(request, turn_id)], request.user)[0]


@router.get("/turns/{turn_id}/caller-context", response=CallerContextOut,
            summary="Who asked for this turn, and what canopy knows about them")
@gate("turn-content")
def get_turn_caller_context(request: HttpRequest, turn_id: uuid.UUID):
    """The caller envelope for one turn: the asker, how sure canopy is (for THIS
    message), their relationship to the agent, and the contact profile canopy
    holds. The same document the claiming runner receives."""
    # Same gate as every turn route. The contact profile is no wider than
    # /api/contacts/, which any member of the tenant can already read.
    from .caller_context import build

    return {"envelope": build(turn_content_or_404(request, turn_id), reader_user=request.user)}


@router.post("/turns/{turn_id}/events", response=TurnEventCountOut)
@gate("runner", "agent.work", "session-acl", kind="turn")  # claimed: owner; unclaimed: agent.work / chat write
def append_turn_events(request: HttpRequest, turn_id: uuid.UUID, payload: TurnEventsIn):
    turn = reporting_turn_or_404(request, turn_id)
    for event in payload.events:
        if event.kind not in ALLOWED_EVENT_KINDS:
            raise HttpError(422, f"unknown event kind '{event.kind}'")
    count = ledger.append_events(turn, [e.dict() for e in payload.events])
    from . import owner_ask

    for event in payload.events:
        if event.kind == owner_ask.EVENT:
            owner_ask.record(turn, (event.payload or {}).get("line"))
    return {"count": count}


@router.get("/turns/{turn_id}/events", response=TurnEventsOut)
@gate("turn-content")
def read_turn_events(request: HttpRequest, turn_id: uuid.UUID, after: int = 0):
    turn = turn_content_or_404(request, turn_id)
    events = turn.events.filter(seq__gt=after).order_by("seq")[:500]
    return {"events": list(events)}


@router.post("/turns/{turn_id}/transcript", response=TranscriptAppendOut)
@gate("runner", "agent.work", "session-acl", kind="turn")
def append_turn_transcript(request: HttpRequest, turn_id: uuid.UUID, payload: TranscriptAppendIn):
    """Ingest a batch of raw `claude -p` JSONL lines onto a turn's retained
    transcript. Same tenancy gate as every other turn route (_turn_or_404) —
    deliberately not a bespoke check; a transcript is more sensitive than a
    turn's status, and a second gate is exactly how the session-turn tenancy
    leak happened.

    Appending to an already-terminal turn is allowed by design: a runner may
    flush its last batch after finishing (services.append_transcript has no
    status check either).

    `batch_id`, if given, dedups a retry of the immediately-preceding batch
    (F5). The per-turn size ceiling (F2) is enforced inside
    `services.append_transcript` itself, never here — crossing it drops the
    batch's content and writes a marker rather than 4xx-ing, because a
    turn's transcript getting long is not a reason to fail a live run;
    `truncated` in the response tells the caller that happened.
    """
    turn = reporting_turn_or_404(request, turn_id)
    total_bytes = sum(len(line.encode("utf-8")) for line in payload.lines)
    if total_bytes > TRANSCRIPT_APPEND_MAX_BYTES:
        raise HttpError(
            422,
            f"transcript batch too large ({total_bytes} bytes; "
            f"limit is {TRANSCRIPT_APPEND_MAX_BYTES} bytes per request)",
        )
    transcript = ledger.append_transcript(turn, payload.lines, batch_id=payload.batch_id)
    return {
        "line_count": transcript.line_count,
        "bytes_raw": transcript.bytes_raw,
        "truncated": transcript.truncated,
    }


@router.get(
    "/turns/{turn_id}/messages", response=TurnMessagesOut,
    summary="A turn's transcript as readable messages",
)
@gate("turn-content")
def read_turn_messages(request: HttpRequest, turn_id: uuid.UUID):
    """The turn's retained transcript parsed into messages (user, assistant,
    tool use, tool result), with secrets scrubbed. Bounded: `truncated` is true
    when the view stopped early. Empty for a turn that kept no transcript."""
    # Same gate as the raw route below — a transcript is a LOG
    # (turn_access.can_read_turn_content). Parsing is bounded
    # (services.TRANSCRIPT_VIEW_MAX_MESSAGES) and reads the blob incrementally.
    turn = turn_content_or_404(request, turn_id)
    messages, truncated = ledger.transcript_messages(turn)
    return {"messages": messages, "truncated": truncated}


@router.get("/turns/{turn_id}/transcript", summary="Raw retained JSONL for a turn")
@gate("turn-content")
def read_turn_transcript(request: HttpRequest, turn_id: uuid.UUID):
    """The byte-for-byte raw transcript, streamed as plain JSONL bytes — a
    turn with nothing ever appended reads as an empty 200, not a 404;
    absence of a transcript is not absence of a turn. No `response=` schema
    is declared so Ninja returns this StreamingHttpResponse verbatim instead
    of trying to serialize it (mirrors apps/canopy_sessions.api's plain
    HttpResponse for attachment_content).

    Streams `services.iter_transcript`, which inflates the stored gzip
    INCREMENTALLY in bounded chunks rather than decompressing the whole blob
    into memory at once (security review 2026-07-26, F3 — the sibling
    `/events` route caps at 500 rows for the same underlying reason).

    A PRIOR version of this fix instead served the still-gzipped bytes
    directly with `Content-Encoding: gzip`, betting the HTTP client would
    inflate transparently — a follow-up review empirically falsified that:
    `curl --compressed` and `httpx` both return only the FIRST gzip member
    of Task 1's multi-member on-disk format, silently truncating the
    transcript with a 200 and no error, and this repo's own runner client
    (`runner/canopy_runner`, `urllib.request`) does no content-decoding at
    all — it would have treated raw gzip bytes as JSONL. Streaming plaintext
    here removes that wire-format gamble: every caller gets exactly the
    bytes `services.read_transcript` would return, with none of its
    all-at-once memory cost.
    """
    turn = turn_content_or_404(request, turn_id)
    return StreamingHttpResponse(
        ledger.iter_transcript(turn), content_type="application/x-ndjson"
    )


@router.post("/turns/{turn_id}/start", response=TurnOut)
@gate("runner", "agent.work", "session-acl", kind="turn")
def start_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnStartIn):
    turn = reporting_turn_or_404(request, turn_id)
    if turn.status not in (Turn.CLAIMED, Turn.RUNNING):
        raise ProblemError(409, "Turn not startable", detail=f"status={turn.status}")
    return services.mark_running(turn, session_id=payload.session_id)


@router.post("/turns/{turn_id}/finish", response=TurnOut)
@gate("runner", "agent.work", "session-acl", kind="turn")  # a LOCAL turn: its initiator only
def finish_turn(request: HttpRequest, turn_id: uuid.UUID, payload: TurnFinishIn):
    from . import runner_protocol

    turn = reporting_turn_or_404(request, turn_id)
    try:
        return runner_protocol.finish(turn, payload)
    except runner_protocol.ProtocolError as exc:
        if exc.status == 422:
            raise HttpError(422, exc.detail) from exc
        raise ProblemError(exc.status, exc.title, detail=exc.detail) from exc


def _cancel_refusal(user, turn: Turn) -> str:
    """Why `user` may not cancel `turn`, or "" when they may.

    Same tier as the enqueue that produced it: an AGENT turn is the author
    tier, so a viewer who cannot dispatch one may not withdraw one either.

    The session carve-out is STRUCTURAL rather than a condition spelled out
    here — `turn_targets_agent_xor_project_xor_session` makes a session turn
    carry `agent=NULL` and derive its agent through `chat_session`, so the agent
    branch cannot see one. That is the right outcome: a viewer may chat, so a
    viewer must be able to take back a misfired send, which is the misfire
    case the phone composer exists for. Project turns likewise have no agent."""
    if turn.agent_id:
        if not perms.can(user, turn.agent.workspace_id, perms.AGENT_WORK):
            return "running a turn for this agent requires the editor or owner role"
    elif turn.initiator_user_id != user.pk:
        # Not your own send: a chat turn needs write access to that chat (a
        # viewer participant may not withdraw someone else's message), and a
        # project turn the same tier that enqueued it.
        if turn.chat_session_id:
            from apps.canopy_sessions import access as session_access

            if not session_access.can_write(user, turn.chat_session):
                return "you can only cancel your own send in this chat"
        elif not perms.can(user, turn.workspace_id, perms.AGENT_WORK):
            return "cancelling a turn requires the editor role or above"
    return ""


@router.post("/turns/{turn_id}/cancel", response=TurnOut)
# agent/project: agent.work; chat: write access, or your own send
@gate("agent.work", "session-acl", "self", kind="turn")
def cancel_turn(request: HttpRequest, turn_id: uuid.UUID):
    """Cancel a QUEUED turn that has not started — the misfire case the phone
    composer needs (dispatch the wrong command, take it back before a runner
    claims it). Finishes it CANCELLED with a cancelled note.

    QUEUED only. A claimed/running turn is already executing in an emdash session;
    stopping that is a different, racier operation (the runner owns the lease) —
    see `services.cancel_turn`, which signals the runner instead and is
    deliberately not wired to this route yet.
    """
    turn = turn_or_404(request, turn_id)
    if turn.agent_id:
        agent_for_read(request, turn.agent.slug)  # a non-member gets a 404, never a 403
    if refusal := _cancel_refusal(request.user, turn):
        raise HttpError(403, refusal)
    if turn.status in Turn.TERMINAL:
        return turn  # idempotent
    cancelled = services.cancel_queued_turn(turn)
    if cancelled is None:
        raise ProblemError(
            409, "Turn not cancelable",
            detail=f"status={turn.status}; only a queued turn can be cancelled",
        )
    return cancelled


# --------------------------------------------------------------------------------------
# AgentSchedule — the runner-facing half. The supervisor's CRUD lives in api_schedules.py;
# these two routes are what the laptop daemon actually calls: sync, then report a due slot.
# --------------------------------------------------------------------------------------
