"""Pydantic schemas for /api/canopy-sessions."""
from __future__ import annotations

from typing import Literal

import datetime as dt
import uuid

from ninja import Schema
from pydantic import AliasChoices, Field, field_validator

from apps.harness.schemas import ChatSendOrigin, ParentIn, TurnOut, chat_send_origin


class ProjectSessionShareOut(Schema):
    """One project you could share your laptop sessions with (`/api/me/session-sharing/`)."""
    project_id: int = Field(description="The project's id.")
    handle: str = Field(description="The project's handle in its workspace.")
    name: str
    workspace: str = Field(description="The workspace the project belongs to.")
    repo: str = Field(description="The repo the project works in; your laptop sessions in it are what is shared.")
    shared: bool = Field(description="Your sessions in this repo are visible to the project's team.")
    team_size: int = Field(description="How many people are on the project's team: its owner and its participants.")


class ProjectSessionShareIn(Schema):
    """Turn sharing your laptop sessions with one project's team on or off."""
    shared: bool


class ConversationKeyWrapIn(Schema):
    """A conversation key's private half wrapped to a vault key: `{alg: "ecdh-es", epk,
    n, ct}`."""
    alg: Literal["ecdh-es"]
    epk: str
    n: str
    ct: str


class ConversationKeyIn(Schema):
    """A key pair for this conversation, made in your browser: its public half, and its
    private half wrapped to YOUR current vault key (info `canopy-chat/v1/wrap|<conversation>|<your
    user id>`). If the conversation is, or later becomes, sensitive its words are sealed
    to this key, and canopy-web never sees the private half. Sent by canopy's own chat
    surfaces; leave it out otherwise."""
    #: A fresh UUID the browser chose: bound into the wrap and every envelope as the
    #: conversation's id (`SealedKeyOut.conversation`).
    conversation: uuid.UUID
    public_jwk: dict
    wrap: ConversationKeyWrapIn
    #: Thumbprint of the vault key `wrap` is for.
    recipient_jkt: str


class SessionCreateIn(Schema):
    agent_slug: str | None = None
    # An agentless PROJECT chat: the repo checkout to drive. Mutually exclusive
    # with agent_slug.
    project: str = ""
    title: str = ""
    metadata: dict = {}
    # Directed placement at creation time: the runner this new chat should run
    # on. Stashed on session.metadata["requested_runner_id"] and consumed by the
    # session's FIRST send (as long as the session is still unbound) — see
    # send.send_message.
    runner_id: uuid.UUID | None = None
    # What this session was opened from (apps/harness/schemas.ParentIn) — the
    # same as the X-Canopy-Parent-* headers. Optional, never checked.
    parent: ParentIn | None = None
    # A key pair the starter's browser made (`conversation_keys.check_offer`).
    conversation_key: ConversationKeyIn | None = None


class SendIn(Schema):
    text: str
    # Optional client-generated nonce for idempotent (double-submit-safe) sends.
    client_id: str = ""
    # Directed placement for the turn this send enqueues: "wait" pins to the
    # session's currently bound runner, or a runner UUID string pins to that
    # runner outright. None leaves normal routing/stickiness in charge.
    placement: str | None = None
    # What KIND of work this send is, for source-aware routing (spec 2026-07-27).
    # None = the default `canopy_web_chat` (a human typing in the web UI), which
    # a caller cannot spell for itself: `ChatSendOrigin` admits only `api` and `ace_web`,
    # so the server-only sources keep exactly one producer each. ace-web sets
    # `ace_web` here — without it, a delegated run enqueues as `canopy_web_chat`
    # and an `ace_web` routing rule has nothing to match.
    origin: ChatSendOrigin | None = None
    # What this send was made from (ParentIn); recorded on the turn it enqueues.
    parent: ParentIn | None = None
    # "Clear & send": if the session's prompt already holds unsent text, the
    # runner deletes it and delivers this message instead of putting up its
    # collision dialog on a box the sender may not be sitting at. Carried to the
    # runner as `origin_ref["clear_prompt"]`.
    clear_prompt: bool = False

    _norm_origin = field_validator("origin")(staticmethod(chat_send_origin))


class PlaceIn(Schema):
    """Body for POST /{session_id}/place — the chat banner's after-the-fact
    directed-placement decision on an already-queued turn."""
    placement: str


class TransferIn(Schema):
    """Body for POST /{session_id}/transfer — move a LIVE session onto another
    runner, history and all.

    Distinct from `place`, which only re-pins a queued turn and leaves the
    binding (and so the next ship, and the next send's stickiness) on the old
    box. This moves the session."""
    # The target runner's UUID. No "wait" spelling here — `place`'s "wait" means
    # "pin to where this session already is", which is the one thing a transfer
    # can never mean.
    runner: str
    # What the new box should know beyond the work itself. The source's branch,
    # commits and uncommitted files travel as a snapshot (`handoff.py`) and the
    # prompt describes them server-side, so this carries only what no checkout
    # holds: what shipped, what is still open, what to do next.
    brief: str = ""


class HandoffOut(Schema):
    """The work half of a transfer: what the source packed, whether the target
    restored it, and whether the source's task was closed."""
    id: str
    session_id: str
    source_runner: str
    target_runner: str
    # pending (the source is packing) | captured | failed | unreachable (the
    # source was not answering: NO SNAPSHOT) | timed_out | not_asked
    capture_status: str
    capture_note: str = ""
    branch: str = ""
    head: str = ""
    local_commits: int = 0
    staged_files: list[str] = []
    unstaged_files: list[str] = []
    untracked_files: list[str] = []
    # Untracked files deliberately not carried, each with why (a secret's name, too big).
    skipped: list[dict] = []
    snapshot_bytes: int = 0
    # The Claude transcript travelled, and the target was told it resumes it.
    transcript_carried: bool = False
    resume_planned: bool = False
    # The transfer turn: empty while the source is still packing.
    turn_id: str = ""
    # "" (nothing to restore) | pending | restored | mismatch | failed
    restore_status: str = ""
    restore_report: dict | None = None
    # "" (nothing to close) | pending | closed | kept (left open: it holds the
    # only copy of work) | failed
    close_status: str = ""
    close_error: str = ""
    created_at: dt.datetime
    settled_at: dt.datetime | None = None


class TransferAllIn(Schema):
    """Body for POST /transfer-all — move every open session on one runner to another."""
    # Runner ids or names.
    from_runner: str
    to_runner: str
    # Given to every session's transfer turn, after what its snapshot holds.
    brief: str = ""
    # Only these sessions (ids); empty = every open session on `from_runner` you can write.
    session_ids: list[str] = []
    # List what would move, and why anything would not, without moving it.
    dry_run: bool = False


class TransferAllResult(Schema):
    session_id: str
    title: str
    session_key: str
    # moved | pending (waiting on the target's admin) | refused (see error) | would_move (dry run)
    status: str
    error: str = ""
    turn_id: str = ""
    handoff: HandoffOut | None = None


class TransferAllOut(Schema):
    from_runner: str
    to_runner: str
    results: list[TransferAllResult]


class ForkIn(Schema):
    """Body for POST /{session_id}/fork."""
    # The message to fork at (`MessageOut.turn_index`). The fork keeps everything
    # up to the end of the turn this message belongs to.
    turn_index: int


class TransferOut(Schema):
    session_id: str
    runner: str
    transferred_from: str
    # The epoch base the target's ordinals now land above — surfaced because it is
    # the observable proof the history was carried rather than dropped.
    index_offset: int
    turn_id: str
    # "moved" — it happened now (the fields above describe it). "pending" — the
    # target belongs to someone else, so a request now waits for one of
    # `approvers`; runner/turn_id are empty until they approve.
    status: str = "moved"
    request_id: uuid.UUID | None = None
    approvers: list[str] = []
    # The work: whether the source is packing it (turn_id is empty until it has,
    # at most `handoff.CAPTURE_WAIT`), and what came — or NO SNAPSHOT and why.
    handoff: HandoffOut | None = None


class TransferDecisionIn(Schema):
    note: str = ""


class JoinRequestOut(Schema):
    """A request to be added to a private conversation. Who asked and how it was
    decided; never anything the conversation said."""
    id: uuid.UUID
    session_id: uuid.UUID
    requested_by: str
    requested_by_name: str
    status: str
    decided_by: str = ""
    decided_at: dt.datetime | None = None
    created_at: dt.datetime


class TransferRequestOut(Schema):
    id: uuid.UUID
    session_id: uuid.UUID
    session_title: str
    agent_slug: str
    to_runner: str
    to_runner_id: uuid.UUID
    from_runner: str
    requested_by: str
    brief: str
    # pending | approved | declined | cancelled | expired
    status: str
    decided_by: str
    decided_at: dt.datetime | None
    note: str
    created_at: dt.datetime
    # Set when this call carried the move out: an approval, or a request from
    # someone who already administers the target. LAUNCHED, not done — a pinned
    # turn lands within seconds, and the agent picking the thread up is separate.
    transfer: TransferOut | None = None
    # The people who may approve — so a requester knows whom to nudge.
    approvers: list[str] = []


class TurnOutMinimal(Schema):
    """Just enough of a Turn for the /place response — the caller only needs to
    confirm the pin took, not the full harness TurnOut shape."""
    id: uuid.UUID
    status: str
    pinned_runner_id: uuid.UUID | None = None


class MessageAppOut(Schema):
    """MCP Apps: the View a tool result carries (spec 2026-10-08 §3)."""
    tool_call_id: str
    site: str
    tool: str
    resource_uri: str
    path: str = ""


class MessageSecretRequestOut(Schema):
    """An agent's request for one of its declared secrets (no value — ever)."""
    id: str
    agent: str
    name: str


class MessageOut(Schema):
    turn_index: int
    role: str
    plaintext: str
    content: dict
    author: dict | None = None
    created_at: dt.datetime
    #: MCP Apps (spec 2026-10-08 §3): set on a tool_result whose call has a View
    #: — `{tool_call_id, site, tool, resource_uri, path}`. Decided server-side;
    #: a client never guesses which tools have Views.
    app: MessageAppOut | None = None
    #: Set on a tool_result that opened a request for one of the agent's
    #: secrets (`agents.secret_requests.annotate`): the chat shows the seal card.
    secret_request: MessageSecretRequestOut | None = None
    #: A SENSITIVE conversation's words, sealed to the conversation's key (a `cc1:`
    #: envelope, `conversation_keys`): `plaintext` and `content` are metadata, and the
    #: reader's browser decrypts this with `SessionDetailOut.sealed_key`. Only for the
    #: conversation's own people on its chat surfaces (`sensitivity.readable`); null
    #: for every other reader and every ordinary conversation.
    sealed: str | None = None

    # Read off the gate's own attribute, never the row's column: a route that forgot
    # `sensitivity.readable` hands out nothing rather than the ciphertext.
    @staticmethod
    def resolve_sealed(obj) -> str | None:
        if isinstance(obj, MessageOut):  # already built (a response re-validates it)
            return obj.sealed
        return getattr(obj, "readable_sealed", None)


class SealedKeyWrapOut(Schema):
    """`{alg: "ecdh-es", epk, n, ct}` — the conversation's private key, wrapped to one
    person's vault key (`zk.ts::wrapEcdhEs`)."""
    alg: Literal["ecdh-es"]
    epk: str
    n: str
    ct: str


class SealedKeysetOut(Schema):
    """The reader's own vault keyset — its public half and its seals, never a private
    key — so a surface with no browser session (the embedded panel) can unlock it."""
    user_id: str
    prf_input: str
    exists: bool = True
    public_jwk: dict
    seals: list[dict]


class SealedKeyOut(Schema):
    """What the reader's browser needs to read a sensitive conversation
    (`conversation_keys.reader_key`): unlock your vault key, unwrap `wrap`
    (info `canopy-chat/v1/wrap|<conversation>|<your user id>`), then open each
    message's `sealed` with the conversation key."""
    conversation: str
    public_jwk: dict
    #: Your copy of the conversation key; null when nobody has given you one.
    wrap: SealedKeyWrapOut | None = None
    recipient_jkt: str = ""
    #: False when `wrap` was made for a vault key you have since replaced.
    current: bool = False
    keyset: SealedKeysetOut | None = None
    #: Some of its words are still under canopy's earlier field key and read again
    #: once `manage.py reseal_sensitive_conversations` moves them.
    needs_migration: bool = False
    #: How far a fork's copied rows sit below the index their envelopes name: an
    #: envelope at row `turn_index` must say `i == turn_index + d` for d in [0, *these].
    #: Null for a fork made before this was recorded.
    index_offsets: list[int] | None = []


class MessagePageOut(Schema):
    """One backward page of transcript for scroll-back ("Load earlier")."""
    messages: list[MessageOut]
    has_more_before: bool


class HumanInputPageOut(Schema):
    """One forward page of what PEOPLE typed into a session, oldest first."""
    messages: list[MessageOut]
    #: Pass back as `?after=` for the next page; null when there is no more.
    next_cursor: int | None = None
    #: "transcript" — canopy's durable rows. "tail" — the session has no durable
    #: rows yet (a local runner session before its backfill), so these come from
    #: the runner's rolling tail: recent only, and never paged.
    source: Literal["transcript", "tail"] = "transcript"


class SlackBindingOut(Schema):
    """The Slack thread a session is connected to (apps/slack/share.py::binding_of)."""
    channel: str
    thread_ts: str = ""
    #: Opens the thread in Slack's web client; "" when canopy can't build one.
    url: str = ""
    #: True when the session was SHARED into the thread (bind), False for a
    #: thread Slack itself started.
    shared: bool = False


class NoiseSampleOut(Schema):
    session_id: str
    workspace: str | None = None
    turn_index: int
    text: str


class NoiseReportOut(Schema):
    #: Rows matched, and the sessions they sit in.
    matched: int
    sessions: int
    sample: list[NoiseSampleOut]


class NoisePurgeIn(Schema):
    #: Restrict to one session (UUID). Omit for all your sessions.
    session_id: str | None = None
    #: Actually delete. Default False: a dry run that deletes nothing.
    apply: bool = False
    #: Example rows to include (clamped to 50).
    sample: int = 5


class NoisePurgeOut(NoiseReportOut):
    applied: bool
    deleted: int


class SessionOut(Schema):
    # A requested full-history ship is still outstanding (the runner has not sent
    # its final chunk). Lets "Load full session" wait on an exact signal rather
    # than on a timer or on rows-stopped-growing, neither of which can tell "still
    # arriving" from "there was nothing more to send".
    backfill_pending: bool = False
    id: uuid.UUID
    agent_slug: str | None
    project: str
    workspace: str
    title: str
    #: The start of the first thing the person typed — what a widget names a
    #: conversation by, since its title is canopy's (`transcript_read.opening_of`).
    #: Empty on a single-session read, which does not annotate it.
    opening: str = ""
    status: str
    created_at: dt.datetime
    # When the session last DID something (binding.last_interacted_at > newest
    # message > created_at). created_at is when canopy first NOTICED a discovered
    # session, which is identical across a report sweep — useless as an age.
    last_activity_at: dt.datetime
    # Liveness (Plan 4) — computed from the RunnerBinding; a web session with no
    # binding is origin="web", running=False, runner_name=None.
    origin: str = "web"
    running: bool = False
    runner_name: str | None = None
    #: The bound runner's id — a stable key to filter by, where names can repeat.
    runner_id: uuid.UUID | None = None
    #: The macOS user@host the live session belongs to ("" when unbound or unreported).
    runner_host: str = ""
    runner_location: str | None = None
    #: The emdash project its worktree lives under — the repo for a project chat,
    #: the agent's own repo (its slug) for an agent chat. What /ide groups by.
    emdash_project: str = ""
    # Is the bound runner reachable right now? Carried on the session because a
    # session's own payload is the only place a caller who can see the session is
    # guaranteed to learn this: GET /api/harness/runners/ is scoped to runners the
    # caller PAIRED (apps/harness/api.py::_runner_visibility_q), so an embedder's
    # delegated user sees an empty fleet and could not otherwise tell a stalled
    # chat ("bound runner offline, turn waiting") from a slow one. None = no
    # binding, so there is nothing to be offline.
    runner_online: bool | None = None
    # WHY it is not reachable — `Runner.live_status` verbatim (online / paused /
    # stale / degraded / disconnected / retired), None when unbound. Served beside
    # `runner_online` for the same reason RunnerOut serves `paused` beside
    # `status`: a client that only reads the bool still behaves correctly, but it
    # cannot tell a box a human PARKED from one that fell over — and those want
    # opposite things from the reader (unpause it vs go find out what broke).
    runner_status: str | None = None
    # The bound runner is online but says Claude is not signed in on it
    # (`Runner.signed_out`): it takes no turns, and `runner_online` alone reads
    # it as fine.
    runner_signed_out: bool = False
    # The model the bound runner's sessions start on, as its settings name it
    # (`opus[1m]`, from its heartbeat) — what the chat sizes the context window
    # by. "" when unbound, or the box does not report it.
    runner_model: str = ""
    session_key: str = ""
    #: Moved onto `runner_name` and not reported there yet (`staleness.TRANSFER_WINDOW`):
    #: listed, but nothing to drive until the new box picks it up.
    transferring: bool = False
    # This session is blocked on a dialog somebody has to answer. A bool here
    # and the dialog itself on the detail read: the list needs to RANK and badge
    # a waiting session, not render its options. Without it a blocked agent and
    # an idle one are indistinguishable in the list — the "it looks like the
    # session stopped" half of the 2026-07-31 spark report.
    waiting_on_you: bool = False
    # The runner tried to close this session and could not — why, in a short
    # sentence, and when. "" when no failed close is outstanding. Shown as
    # "Close failed: <reason>" with a Retry (which re-requests the close).
    close_error: str = ""
    #: A runner is reporting an emdash task for this session right now, so a
    #: close deletes that task. False for a cloud session or an unbound web chat,
    #: where a close cancels its turns and archives it.
    runner_reported: bool = False
    close_error_at: dt.datetime | None = None
    # Push on every finished turn, rather than once the chat has gone quiet.
    notify_every_completion: bool = False
    #: It holds a host's sensitive data (`harness.sensitivity`): read back only by
    #: its own people, never shared, exported, mirrored or reviewed.
    sensitive: bool = False
    #: A sensitive conversation only: whether you are one of its people (its
    #: starter, a participant, its contact) — `sensitivity.is_conversation_person`.
    #: Someone else who can list it sees that it exists, never what it says, and
    #: may ask to be added. Null for an ordinary conversation.
    you_are_in_it: bool | None = None
    #: Who started it, by name ("" when a runner or an agent did): a list shows a
    #: private conversation by this, since it keeps no title. Not content.
    started_by_name: str = ""
    #: The page it was started on: the `resource` its page first declared, set once
    #: (`Session.origin_resource`). "" when it was not started on a page that names one.
    origin_resource: str = ""
    # The supervisor feed's two fields, filled only when the list is asked for
    # them (`?reply=true`) — every other list caller would pay for text it never
    # shows. The agent's most recent reply, WHOLE — sent only when it is the
    # person's turn (agent spoke last, or a dialog is waiting), which is all the
    # feed renders — and whether the agent had the last word.
    last_reply: str = ""
    agent_spoke_last: bool = False
    # The mode (`manual` / `auto`) and origin of the FIRST claimed turn that drove
    # this session, on every row; "" when no turn has been claimed. Lets the feed
    # hold back work an agent did on its own, and the list label each card.
    turn_mode: str = ""
    turn_origin: str = ""
    # Is this session on the CALLER's supervisor feed — feed-only (`?reply=true`).
    # "waiting" = it is; "not_yours" / "parked" / "auto" = waiting, but held back
    # (someone else's runner, a paused/offline runner, an agent's own run); "" =
    # not waiting on anybody. "parked" needs EVIDENCE: a dialog (`waiting_on_you`)
    # or a turn queued/in flight that the box cannot take — an idle session on a
    # paused runner is "", not parked. The same rule decides who is pushed about a session
    # (canopy_sessions.feed), so the feed and the phone cannot disagree.
    feed_status: str = ""
    # What this conversation's host requires of the runner it runs on (e.g.
    # `sensitive_data`), so placement offers only runners whose `flags` include every one.
    # A malformed stored value comes back as an entry no runner can declare.
    runner_requirements: list[str] = []
    # Who opened it (a member's email; null for a contact's or a runner-discovered
    # session) and WHAT created it — the same record as TurnOut.provenance.
    created_by: str | None = None
    provenance: dict = {}
    parent_turn_id: uuid.UUID | None = None
    parent_session_id: uuid.UUID | None = None
    parent_task: str = ""
    parent_claude_session: str = ""
    # What the session DID, folded from its transcript as it was ingested: cwd(s),
    # repos, remotes, branches, PRs created/merge-requested, edited directories,
    # MCP tool counts. Shape and limits: apps/canopy_sessions/activity.py. The
    # list filters `?repo=`, `?branch=`, `?pr=` read it.
    activity: dict = {}
    # The Slack thread this session talks to — replies there reach it and its
    # replies post there — or null. Cleared by POST /slack/detach.
    slack: SlackBindingOut | None = None


class SessionSearchPageOut(Schema):
    """One page of a newest-activity-first walk over every visible session."""
    sessions: list[SessionOut]
    #: Opaque. Pass back as `?cursor=` (with the same filters) for the next
    #: page; null when the walk is complete.
    next_cursor: str | None = None


class SessionAudienceOut(Schema):
    """Who can see this session, in one line, from the same pieces as the session ACL."""
    label: str = Field(description='Who it is visible to, e.g. "Only you", "Everyone on this email thread (4)".')
    extras: list[str] = Field(default_factory=list,
                              description='Who else may, e.g. "Trellis may review (coaching)".')
    count: int = Field(description="How many people in the workspace can read it.")


class SessionDetailOut(SessionOut):
    messages: list[MessageOut]
    # The dialog itself, when `waiting_on_you`. Same payload shape whichever
    # half found it (transcript or screen read), so a client never grows two
    # readers — see canopy_transcript.questions.
    menu: dict | None = None
    # What the session's subagents are doing, one per `Agent` call
    # (`RunnerBinding.subagents`), from the same reader as the socket snapshot.
    subagents: list[dict] = []
    # Where the ask this session is waiting on currently stands — the same
    # projection Slack renders as a thread line and the chat kit as a status
    # (apps/harness/turn_status.py). Carried here as well as on the socket for
    # the same reason `menu` is: a client that reconnects, or one that only
    # reads over REST, still has to be able to tell "queued behind an offline
    # runner" from "working on it". Null when nothing has been asked yet.
    turn_status: dict | None = None
    # Your role here (owner / editor / viewer), from the one ACL
    # (canopy_sessions.access). The page reads it to decide whether to offer
    # sharing and the composer — the server enforces both regardless.
    my_role: str | None = None
    # Whether you may bring someone new in (`access.can_add_people`): the chat's
    # owner, or the agent's owner when they are in it.
    can_add_people: bool = False
    # Who can see this, from the same pieces as the ACL (`canopy_sessions.audience`).
    audience: SessionAudienceOut | None = None
    #: A private conversation you are not in: your latest request to be added
    #: (`pending` / `declined` / …), or "" when you have not asked.
    your_join_request: str = ""
    # May YOU move this session to another workspace (`POST /{id}/move`)? The
    # person whose session it is — its creator, or for a runner-discovered one
    # the runner's owner — and never an agent's session (canopy_sessions.filing).
    # The page offers "Move to workspace…" only when this is true and you can
    # edit in more than one workspace; the server enforces it regardless.
    can_move: bool = False
    # Tail-first cursor: the transcript ships the last N messages by default;
    # these tell the client whether earlier history exists and where the loaded
    # window starts, for scroll-back / "load full". See transcript_read.SESSION_TAIL_DEFAULT.
    has_more_before: bool = False
    oldest_loaded_turn_index: int | None = None
    # A SENSITIVE conversation's key, as you read it: null for an ordinary one, and
    # for anyone not in it (`sensitivity.sealed_key_for`).
    sealed_key: SealedKeyOut | None = None


class SessionBindingInfoOut(Schema):
    """The runner side of a session, for Session info: where it lives right now
    and how the runner names it. Every field is the binding's own, unprocessed."""
    runner_id: uuid.UUID | None = None
    runner_name: str | None = None
    runner_kind: str | None = None
    #: The macOS user@host whose emdash holds the session.
    host: str = ""
    #: The emdash project and task — the pair the runner resolves a transcript by.
    emdash_project: str = ""
    session_key: str = ""
    #: The reuse key a turn is matched to this session by (`<target>:<turn>`, a
    #: chat's own id, a phone topic).
    thread_key: str = ""
    #: The Claude session (the .jsonl stem) the transcript rows came from.
    transcript_id: str = ""
    task_uid: str = ""
    agent_task_ext_id: str = ""
    status: str = ""
    agent_status: str = ""
    summary: str = ""
    transferred_at: dt.datetime | None = None
    transferred_from_name: str | None = None
    reported_at: dt.datetime | None = None
    live_seen_at: dt.datetime | None = None
    last_interacted_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class SlashCommandOut(Schema):
    """A command the composer offers as `/name`."""

    name: str
    description: str = ""
    # What the command takes, shown after the name while it is typed.
    args_hint: str = ""
    # agent (the agent's launchable skill), user, plugin or acp (what the box found).
    source: str = ""


class SessionInfoOut(Schema):
    """Everything canopy stores about a session, in one read — for the Session
    info panel. `session` is the same shape the list and detail reads serve."""
    session: SessionOut
    #: What started it and where it belongs (the host's linkage, e.g. {"opp_slug": ...}, the
    #: site, the requirements): `api.INFO_METADATA_KEYS`, never canopy's own bookkeeping.
    metadata: dict = {}
    #: The contact a visitor's session belongs to, by display name; null for a member's.
    contact: str | None = None
    updated_at: dt.datetime
    #: Null when no runner has ever backed it (an unsent web chat).
    binding: SessionBindingInfoOut | None = None
    #: Every turn that drove this session, newest first, at most `turns_limit` —
    #: who asked, through which credential and client, in which mode, on which
    #: runner. Content you may not read is blanked (`content_hidden`), as on the
    #: turn routes.
    turns: list[TurnOut] = []
    turn_count: int = 0
    turns_limit: int = 0
    participant_count: int = 0
    message_count: int = 0


class SendOut(Schema):
    turn_id: uuid.UUID | None
    message: MessageOut


class SessionNotifyIn(Schema):
    every_completion: bool


class SessionTitleIn(Schema):
    #: The new title. Blank clears it, so the session is named again the way an
    #: untitled one is (its emdash task, or its first message).
    title: str = Field(max_length=200)


class SessionMoveIn(Schema):
    #: The workspace slug to file this session in. You must be an editor there.
    workspace: str


class ParticipantOut(Schema):
    """Someone explicitly given this chat. The creator is its owner."""
    user_id: int
    email: str
    display_name: str
    role: str
    joined_at: str | None = None
    #: A sensitive conversation only: they hold no copy of its key for their current
    #: vault key (given it before it became sensitive, or their vault key changed), so
    #: they cannot read it and are not on its live socket. Anyone in it can give them
    #: one from their browser (`POST /{id}/key-copies`).
    needs_key: bool = False
    #: An agent's own login holding its agent's TEST vault key, not a person's key
    #: (`apps/agents/credentials/login_test_key.py`).
    test_key: bool = False


class SealedKeyWrapIn(Schema):
    """A sensitive conversation's key, wrapped in your browser to the person you add:
    `{alg: "ecdh-es", epk, n, ct}` for info `canopy-chat/v1/wrap|<conversation>|<their
    user id>`, and the thumbprint of the vault key it is for
    (`GET /{id}/participants/recipient`)."""
    alg: Literal["ecdh-es"]
    epk: str
    n: str
    ct: str
    recipient_jkt: str


class ParticipantAddIn(Schema):
    """Give a teammate this chat. They must already be in its workspace. In a
    sensitive conversation `wrap` is their copy of its key, made in your browser;
    required unless they already hold a current one."""
    email: str
    role: Literal["editor", "viewer"] = "editor"
    wrap: SealedKeyWrapIn | None = None


class KeyCopyIn(Schema):
    """Someone already in a sensitive conversation, given their copy of its key from your
    browser: `wrap` is the key wrapped to their current vault key
    (`GET /{id}/participants/recipient`)."""
    user_id: int
    wrap: SealedKeyWrapIn


class MyKeyCopyOut(Schema):
    """One sensitive conversation whose key you hold a copy of: what your browser needs
    to re-wrap it to a new vault key, and enough to name it to you."""
    session_id: str
    conversation: str
    public_jwk: dict
    wrap: dict
    recipient_jkt: str
    agent_name: str = ""
    workspace: str = ""
    started_at: dt.datetime | None = None


class MyVaultKeyOut(Schema):
    """Your vault key's public half (`GET /my-vault-key`)."""
    user_id: str
    public_jwk: dict
    jkt: str
    #: The keyset to open it with in your browser (`sealed_key.keyset`'s shape: public half
    #: and seals, never a private key), so a browser that has not opened it before derives
    #: its public half itself before wrapping a new chat's key to it. Only to your own
    #: browser (canopy's page or the panel's frame); null otherwise.
    keyset: dict | None = None


class MyKeyCopyIn(Schema):
    session_id: uuid.UUID
    wrap: ConversationKeyWrapIn
    recipient_jkt: str


class MyKeyCopiesIn(Schema):
    """Your copies of your conversations' keys, re-wrapped in your browser to the vault
    key you just set up (`PUT /api/vault/keyset` with `replace`)."""
    copies: list[MyKeyCopyIn]


class MyKeyCopiesOut(Schema):
    stored: int


class ParticipantRecipientOut(Schema):
    """The vault key to wrap a sensitive conversation's key to, for the person you
    are about to add."""
    user_id: int
    email: str
    public_jwk: dict
    jkt: str
    #: The key is an agent login's TEST vault key, not a person's.
    test_key: bool = False


class StreamStateOut(Schema):
    """Whether the bound runner is being asked to stream this session live."""
    streaming: bool


class BackfillStateOut(Schema):
    """ready = already server-full; requested = runner asked; unavailable = offline."""
    status: str


class AttachmentOut(Schema):
    """An uploaded attachment. Carries no URL — the client builds the content
    path from the id, so there is no signed link to expire or leak."""

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    message_id: str | None = None

    @staticmethod
    def resolve_message_id(obj) -> str | None:
        return str(obj.message_id) if obj.message_id else None


class SealedAttachmentOut(Schema):
    """A file sent in a private conversation, as its people's browsers open it: no
    name, no exact size. `sealed_name` is a `cf1:` envelope (name, type, size) sealed to
    the conversation's key; the bytes are `GET /attachments/{id}/sealed`. `turn_index`
    is the person's line it was sent with, null until that line is stored."""

    id: uuid.UUID
    turn_index: int | None = None
    uploaded_by: int | None = None
    family: str
    size_bucket: int
    sealed_name: str


class ResetOut(Schema):
    """One session's reset outcome. `reason` is `ok` | `no_binding` |
    `runner_unreachable` — stable strings the UI renders as the refusal."""

    session_id: str
    title: str
    ok: bool
    reason: str
    rows_dropped: int
    runner: str


class ResetIn(Schema):
    """Bulk reset scope. Empty body = every session the caller can see, in the
    workspace the request resolved to."""

    prune_ghosts: bool = False
    dry_run: bool = False


class ResetSummaryOut(Schema):
    dry_run: bool
    reset: list[ResetOut]
    skipped: list[ResetOut]
    pruned: list[dict]
    rows_dropped: int


class MenuAnswerIn(Schema):
    """Which option to press on a blocked agent's dialog.

    `None` means refuse — sent as Escape. Deliberately nullable rather than a
    magic number: every dialog offers Esc, and it is the only answer that is
    safe when the option numbering is not what the client thought it was.

    `selections` carries the whole answer to an `AskUserQuestion`: one list of
    chosen option numbers per declared question, in declaration order, empty for
    a question left unanswered. A single `option` cannot express "Red and Blue",
    and cannot reach the tab holding question 2 at all — so a multi-select or a
    multi-question ask was unanswerable from the web until this field existed.
    `option` is still sent beside it (the first pick) purely so a runner older
    than this behaves exactly as it does today instead of pressing Escape.
    """
    option: int | None = None
    selections: list[list[int]] | None = None
    #: Free text per question, for the answer that is not on the menu — the
    #: TUI appends a "Type something" row to every question and it is often
    #: where the real answer goes. Empty string = no free text.
    #: `None` for a question left unanswered — the list is POSITIONAL against
    #: the declared questions, so a hole has to be expressible or every later
    #: answer shifts onto the wrong question.
    texts: list[str | None] | None = None


# --- page actions: the host's capabilities, and calls into them --------------


class PageActionSpec(Schema):
    """One thing the attached page says it can do.

    `parameters` is JSON-Schema, written by the HOST and passed through
    uninterpreted — canopy is not the party that knows what a host's action
    means. It is what lets an agent call `declineTasks` knowing it takes
    `{ids: string[]}`, rather than being told in prose.

    May also be sent as `inputSchema`, which is MCP's name for the same field;
    it is read back under `parameters` either way.
    """

    # Rationale, deliberately out of the docstring — a Ninja docstring is the
    # PUBLISHED OpenAPI description and ships into `generated.ts` (#757).
    #
    # Two spellings in, one out, rather than two fields that can disagree.
    # canopy publishes these declarations as MCP tools, so a host author
    # reading the MCP spec writes `inputSchema`; while only `parameters` was
    # accepted, such a declaration was dropped HERE, at the door — the action
    # still published, with no schema, and a missing required argument then
    # waited out the full 20s timeout and came back as "the page is probably
    # closed", which is neither true nor actionable.
    name: str
    description: str = ""
    parameters: dict = Field(
        default={},
        validation_alias=AliasChoices("parameters", "inputSchema"),
    )


class PageActionsDeclareIn(Schema):
    """What the page can do, replacing any previous declaration.

    Wholesale, never merged: a page has one current set of capabilities, and an
    action left over from a page the user navigated away from is one the agent
    would call into nothing.
    """

    actions: list[PageActionSpec] = []


class PageStateIn(Schema):
    """What the attached page currently shows.

    Replaces any previous declaration wholesale. Bounded on the server: a page
    that sends the rows it displays rather than the selection of them is
    refused, because the agent re-reads those rows itself under the caller's own
    permissions.
    """

    # Rationale out of the docstring on purpose — a Ninja docstring ships to
    # OpenAPI and generated.ts, so it is public API prose, not a design note.
    state: dict = {}


class PageStateOut(Schema):
    """The stored view, with the server-assigned `version` folded in.

    An empty `state` means no page is attached OR the page declares nothing.
    Those are the same to a reader and deliberately so; neither is "the screen
    is blank".
    """

    state: dict = {}
    version: int = 0


class RunAgentInputIn(Schema):
    """AG-UI's `RunAgentInput`, as the one way to declare a page.

    canopy has two endpoints for this — `page-state` and `page-actions` — which
    is fine for canopy's own widget and a poor answer for anyone else: a host
    integrating from the outside has to learn two canopy-specific shapes to say
    what every AG-UI client already knows how to say. This accepts the
    protocol's own object and translates.

    Only the two fields canopy can honour are read. `messages`, `run_id`,
    `parent_run_id`, `resume` and `forwarded_props` are accepted and IGNORED
    rather than rejected: a conforming client sends the whole object, and
    refusing it because canopy has no use for `resume` would make the protocol's
    own payload invalid here — which defeats the point of speaking it.
    """

    # Rationale out of the docstring on purpose — a Ninja docstring is published
    # API documentation, and this ships to generated.ts.
    state: dict = {}
    tools: list[PageActionSpec] = []
    thread_id: str = ""
    messages: list = []
    context: list = []
    run_id: str = ""
    parent_run_id: str | None = None
    forwarded_props: object | None = None
    resume: list = []


class RunAgentInputOut(Schema):
    """What canopy took from the input, so a client can see what was honoured."""

    state: dict = {}
    version: int = 0
    tools: list[PageActionSpec] = []


class PageActionInvokeIn(Schema):
    name: str
    args: dict = {}


class PageActionOut(Schema):
    id: str
    name: str
    status: str
    result: object | None = None
    error: str = ""


class PageActionResultIn(Schema):
    """The page reporting back.

    `error` non-empty means the host's callback refused or threw. A refusal is
    a FAILED action carrying its reason, never a quiet success — an agent that
    cannot tell those apart continues as though the page changed.
    """

    result: object | None = None
    error: str = ""


class SessionSecretIn(Schema):
    """A secret handed to this chat. Nothing is posted into the chat."""
    name: str
    value: str


class SessionSecretOut(Schema):
    """What anyone may know about a shared secret: never the value."""
    name: str
    created_by: str | None = None
    created_at: str
    updated_at: str
    last_used_at: str | None = None
    #: 30 minutes after it was shared; gone after that (`secrets.TTL`).
    expires_at: str


class SessionSecretValueOut(Schema):
    name: str
    value: str


class SessionExportOut(Schema):
    """A session's conversation as readable markdown, to pick it up elsewhere."""

    session_id: uuid.UUID
    title: str
    message_count: int
    markdown: str
