"""Turn, turn event and transcript schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from canopy_transcript import closeout as transcript_closeout
from ninja import Schema
from pydantic import Field, field_validator, model_validator

from apps.workspaces import links

from .. import clis as cli_vocab
from .. import invocations, sensitivity, session_sync
from .common import Origin, Routing, normalize_origin
from .sessions import LEGACY_SESSION_KEY, adopt_legacy_session_key


class ParentIn(Schema):
    """What the caller was running INSIDE when it asked for this work — recorded
    on the new turn/session as `parent_*` + `provenance.parent`. Every field is
    optional and none is checked: an id that does not resolve is recorded as
    given and never refuses the request. The `X-Canopy-Parent-Turn`,
    `X-Canopy-Parent-Session` and `X-Canopy-Claude-Session` headers say the same
    thing; a field here wins over its header. Nothing runner-specific: the parent
    turn names the runner that claimed it."""

    turn: str = ""
    session: str = ""
    claude_session: str = ""


class TurnIn(Schema):
    # Exactly one of agent_slug / project. Enforced in the view (422) rather than
    # by a validator so the error matches the rest of the harness's shape.
    agent_slug: str = ""
    project: str = ""
    origin: Origin
    idempotency_key: str
    prompt: str = ""
    origin_ref: dict = {}
    routing: Routing = "prefer_local"
    # Name the box explicitly. A pin bypasses assignments and source rules — never
    # the tenant gate. This is what retired the
    # `drill` origin: a drill is an api turn that names its runner, identified by
    # its RunnerDrill row rather than by a magic origin value.
    runner_id: uuid.UUID | None = None
    # Ask for the MODE this agent turn runs in, above every routing rule and the
    # agent's own switch (apps/harness/turn_mode.py). `manual` is open to anyone
    # who may enqueue — lowering autonomy is always safe. `auto` only from the
    # agent's owner or an admin (workspace owners included), signed in or on a
    # PAT; anyone else gets a 403. Refused on a project turn and on email, which a
    # runner posts on a stranger's behalf. Omit to let the rules decide.
    turn_mode: Literal["auto", "manual"] | None = None
    # Run this turn as a LOWER role than the caller holds (apps/workspaces/role_cap.py):
    # everything about it — whether it may be started, its mode, its envelope, its
    # credential reads — is judged as an `editor` or a `viewer`. Only lowers, so
    # anyone may ask; it ends with the turn. Omit to act as yourself.
    acts_as: Literal["", "editor", "viewer"] | None = None
    # What this request was made from (see ParentIn). Optional.
    parent: ParentIn | None = None
    # The agent CLI to run it on: one the agent is set up for (`clis.refusal`), or
    # Claude Code for a project turn. Omit for the agent's default.
    cli: cli_vocab.CliName | None = None
    # Who a turn nobody asked for is starting this one for: the email of ONE person who let this
    # agent review them. The turn reads their sessions alone (`turn_asker.READS_FOR`). Only that
    # agent's own login, inside such a turn, may name one.
    reads_for: str | None = None

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))


class InitiatorPersonOut(Schema):
    id: int
    email: str
    name: str


class InitiatorOnBehalfOut(Schema):
    user: InitiatorPersonOut
    relationship: str
    via_agent: str = ""
    turn: str = ""


class InitiatorOut(Schema):
    """Who asked for this turn, and how that was established."""

    kind: str
    via: str
    assurance: str
    user: InitiatorPersonOut | None = None
    contact: InitiatorPersonOut | None = None
    agent: str | None = None
    # The user is an AGENT'S OWN LOGIN (its slug), not a person; `self` when it is
    # this turn's own agent — the agent started this turn itself (e.g. testing).
    agent_login: str | None = None
    self: bool = False
    # On an agent's turn: the person it was working for — {user, relationship (to the
    # agent receiving the work), via_agent, turn} — or null when it acted on its own.
    # canopy-web works it out from the agent's turn login; no agent declares it.
    on_behalf_of: InitiatorOnBehalfOut | None = None
    # When one of the agent's own TRIGGERS fired the turn (a schedule, or alarm mail
    # from a bound sender): {id, name, kind, set_up_by}. `user` is then who set it
    # up and is accountable for it, not who wrote in (apps/harness/triggers.py).
    trigger: dict | None = None
    # WHICH credential the request that created the turn used — {type, id, label}
    # (`pat`, `oauth`, `session`, `delegated`, `contact`, `caller_token`) — and
    # the program that sent it (X-Canopy-Client). Null when canopy started it.
    credential: dict | None = None
    client: str = ""
    # The turn / session it was started from, and the runner that parent turn ran
    # on ({id, name, kind}) — see TurnOut.parent_*.
    parent: dict | None = None


class TurnOut(Schema):
    id: uuid.UUID
    # Exactly one of these is set — a turn targets an agent or a repo, never
    # both. Consumers should read `target` unless they specifically need to know
    # which kind it is.
    agent_slug: str | None
    project: str
    target: str
    # The tenant the runner must pass back to record/resolve a PROJECT session
    # link (the owner may belong to several workspaces; the turn knows its own).
    # Derived: agent turns report their agent's workspace, project turns their own.
    workspace_slug: str | None
    origin: str
    status: str
    routing: str
    prompt: str
    origin_ref: dict
    claimed_by_name: str | None
    enqueued_by_email: str | None
    initiator: InitiatorOut
    # manual | auto, decided at claim (apps/harness/turn_mode.py); "" until then.
    turn_mode: str = ""
    turn_mode_basis: str = ""
    # What the dispatcher ASKED for (TurnIn.turn_mode) and who, "" / null if
    # nothing. `turn_mode` above is what the claim actually decided.
    requested_turn_mode: str = ""
    requested_turn_mode_by_email: str | None = None
    # The runner this turn is PINNED to (TurnIn.runner_id), if any — only it may claim.
    pinned_runner_id: uuid.UUID | None = None
    pinned_runner_name: str | None = None
    # Set when this turn was claimed to be DELIVERED INTO its conversation's
    # running turn (that turn's id) rather than run on its own: the runner types
    # it into the live session and it finishes when that turn does.
    rides_turn_id: uuid.UUID | None = None
    # The agent CLI the claim chose for it (`harness.clis`): "claude-code" or
    # "codex"; "" until claimed (and again if it goes back to the queue).
    cli: str = ""
    # The agent CLI its starter or schedule asked for; "" = the agent's default.
    requested_cli: str = ""
    # WHAT created this turn (apps/harness/provenance.py): credential, client,
    # user_agent, request_id, mcp_tool, parent (raw), clicked_by. The client ip
    # is recorded but not served.
    provenance: dict = {}
    parent_turn_id: uuid.UUID | None = None
    parent_session_id: uuid.UUID | None = None
    parent_task: str = ""
    parent_claude_session: str = ""
    # The board task whose approved dispatch created this turn, if any — by its
    # ext_id, the way every route names a task (it is the agent's own task).
    raised_from_task_ext_id: str | None = None
    session_id: str
    # The canopy conversation this turn ran in — a chat turn's own session, or the
    # one its runner made for an agent/project turn (`Turn.canopy_session`). Null
    # until that exists (the turn is still queued, or no runner reported one).
    # NOT `session_id` above, which is the runner's own name for its live session.
    # Follow up there with `POST /api/canopy-sessions/{canopy_session_id}/send`.
    canopy_session_id: uuid.UUID | None = None
    # That conversation's page on canopy-web; null when it has no workspace.
    canopy_session_url: str | None = None
    result_note: str
    # A PLAIN JOB: the name of a command in the repo's `.canopy/jobs.yaml`, and the repo's
    # name. Blank for every other turn; such a turn has no prompt and no transcript, and its
    # page shows its output and `result_note` as a log.
    job: str = ""
    job_repo: str = ""
    # The closeout verdict an unattended incident turn ENDED on, lifted off
    # `result_note`'s `[closeout: <VERDICT>]` status line (canopy_transcript.closeout):
    # CLOSED | MONITORING | NEEDS_YOU | INCOMPLETE, "" when the turn printed none.
    # Code computed it (canopy `closeout record`), the turn did not type it — so a
    # session may be auto-closed on CLOSED alone, and `closeout_line` is the one
    # line a NEEDS_YOU shows its owner. Blank whenever `result_note` is.
    closeout_verdict: str = ""
    closeout_line: str = ""
    # True when `prompt`, `origin_ref` and `result_note` were blanked because
    # the caller may not read this turn's content (apps/harness/turn_access.py).
    content_hidden: bool = False
    created_at: dt.datetime
    claimed_at: dt.datetime | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None
    lease_expires_at: dt.datetime | None

    @staticmethod
    def resolve_closeout_verdict(obj) -> str:
        return transcript_closeout.find(getattr(obj, "result_note", "") or "")[0]

    @staticmethod
    def resolve_closeout_line(obj) -> str:
        return transcript_closeout.find(getattr(obj, "result_note", "") or "")[1]

    @staticmethod
    def resolve_agent_slug(obj) -> str | None:
        # None for project turns — dereferencing obj.agent unconditionally
        # would 500 the moment agent can be NULL.
        if obj.agent_id:
            return obj.agent.slug
        # A chat SESSION turn targets a Session, not an agent — but you chat WITH an
        # agent, so surface the session's agent as the emdash target the runner drives.
        cs = getattr(obj, "chat_session", None)
        return cs.agent.slug if cs and cs.agent_id else None

    @staticmethod
    def resolve_raised_from_task_ext_id(obj) -> str | None:
        if not getattr(obj, "raised_from_task_id", None):
            return None
        return obj.raised_from_task.ext_id

    @staticmethod
    def resolve_project(obj) -> str:
        # A project turn stores its repo on the column; a PROJECT chat session
        # carries it on the session (the Turn.project column stays empty — the
        # agent XOR project XOR session constraint forbids setting it there).
        if obj.project:
            return obj.project
        cs = getattr(obj, "chat_session", None)
        return cs.project if cs is not None else ""

    @staticmethod
    def resolve_workspace_slug(obj) -> str | None:
        # Agent turns derive tenancy via the agent; project turns store their own;
        # a session turn (agent-backed or project-backed) derives it from the session.
        if obj.agent_id:
            return obj.agent.workspace_id
        cs = getattr(obj, "chat_session", None)
        if cs is not None:
            return cs.workspace_id
        return obj.workspace_id

    @staticmethod
    def resolve_canopy_session_id(obj) -> uuid.UUID | None:
        return getattr(obj, "chat_session_id", None) or getattr(obj, "canopy_session_id", None)

    @staticmethod
    def resolve_canopy_session_url(obj) -> str | None:
        session = (obj.chat_session if getattr(obj, "chat_session_id", None)
                   else obj.canopy_session if getattr(obj, "canopy_session_id", None) else None)
        if session is None:
            return None
        return links.scoped_url_or_none(session.workspace_id, f"/sessions/{session.pk}")

    @staticmethod
    def resolve_claimed_by_name(obj) -> str | None:
        return obj.claimed_by.name if obj.claimed_by else None

    @staticmethod
    def resolve_enqueued_by_email(obj) -> str | None:
        return obj.enqueued_by.email if obj.enqueued_by_id else None

    @staticmethod
    def resolve_requested_turn_mode_by_email(obj) -> str | None:
        return obj.requested_turn_mode_by.email if obj.requested_turn_mode_by_id else None

    @staticmethod
    def resolve_pinned_runner_name(obj) -> str | None:
        return obj.pinned_runner.name if obj.pinned_runner_id else None

    @staticmethod
    def resolve_provenance(obj) -> dict:
        from ..provenance import public

        return public(getattr(obj, "provenance", None))

    @staticmethod
    def resolve_initiator(obj) -> dict:
        # Distinct from enqueued_by_email: that is the CALLER of the enqueue (a
        # runner, for an email turn); this is the person the turn is for.
        from ..initiator import describe

        return describe(obj)


class CallerContextOut(Schema):
    """Who asked for a turn and what canopy knows about them — the caller
    envelope (`apps/harness/caller_context.py`). Loosely typed on purpose: it is
    a versioned document the runner writes to disk for the agent, not a surface
    a client binds to field by field."""

    envelope: dict


class ClaimPageSiteOut(Schema):
    """A turn held on a host site's page (`published_profiles.page_turn`)."""

    # The site's name (the conversation's `embed_app`), which the agent serves.
    site: str
    # The agent's own credential names that are its login at that site
    # (`own_login:` in its interface). The runner leaves them out of this turn's
    # environment: on a page the agent reaches the site only as the person who sent.
    withheld: list[str] = []


class InvocationOut(Schema):
    """The plugin command a turn's prompt opens with (`harness.invocations`)."""

    plugin: str
    skill: str
    args: str = ""
    # Every line after the command's, as written.
    rest: str = ""


class ClaimedTurnOut(TurnOut):
    """A turn as its CLAIMING runner receives it: everything `TurnOut` has, plus
    the caller envelope, so the runner can hand it to the agent without a second
    round trip. Only the claim returns it — it carries what canopy knows about a
    person, which a turn LISTING has no reason to spread."""

    caller_context: dict
    # (`job` / `job_repo` are on `TurnOut`. The command itself is never sent: the box
    # reads it from the repo.)
    # For a CONFINED turn only: the token the session uses for canopy's MCP in
    # place of the runner owner's PAT (harness.models.CallerToken). Null otherwise.
    mcp_token: str | None = None
    # For a CHAT turn: the chat's key (canopy_sessions.ChatKey), which the runner
    # gives only to the Claude session driving this chat. Presented in the
    # `X-Canopy-Chat-Key` header, it reaches this chat's secrets and page and
    # nothing else. Null for a turn that is not a chat's.
    chat_key: str | None = None
    # True when the turn is in a chat whose conversation holds a host's sensitive
    # data (`sensitivity.is_sensitive`). Such a conversation's turns are all confined
    # to the same profile, so a sensitive-data runner resumes the Claude session
    # it kept for the conversation's previous turn rather than start fresh.
    sensitive_conversation: bool = False
    # For a turn in a sensitive conversation: {Claude session id: how many of that
    # transcript's records canopy-web holds rows through} for each transcript an
    # earlier turn of it shipped (`session_sync.turn_end_held`). A runner resuming one
    # ships what comes after, so a ship that failed before is sent again.
    transcript_held: dict[str, int] = {}
    # The model to start that CLI on, from the agent's `cli_models`; "" = the
    # CLI's own default.
    cli_model: str = ""
    # The plugin command its prompt opens with, parsed once here so each turn
    # executor renders it in its own CLI's terms (Claude Code types `prompt` as
    # is); null for a prompt of plain words.
    invocation: InvocationOut | None = None
    # For a turn whose conversation is held on a host site's page that its agent
    # serves: the site, and the agent's own credentials for it that the runner must
    # leave out of the turn (`published_profiles.page_turn`). Null for every other turn.
    page_site: ClaimPageSiteOut | None = None

    # Nothing about a visitor's HOST credential is ever here (host grant
    # contract v1): the gateway (`site_call`) attaches it inside canopy-web, so
    # it cannot leak through a runner. No canopy-minted `on_behalf_of`
    # assertion rides this response either.

    @staticmethod
    def resolve_mcp_token(obj) -> str | None:
        return getattr(obj, "mcp_token", None)

    @staticmethod
    def resolve_invocation(obj) -> InvocationOut | None:
        inv = invocations.parse(getattr(obj, "prompt", ""))
        return InvocationOut(plugin=inv.plugin, skill=inv.skill, args=inv.args,
                             rest=inv.rest) if inv else None

    @staticmethod
    def resolve_chat_key(obj) -> str | None:
        return getattr(obj, "chat_key", None)

    @staticmethod
    def resolve_sensitive_conversation(obj) -> bool:
        session = getattr(obj, "chat_session", None)
        return session is not None and sensitivity.any_sensitive(session, obj)

    @staticmethod
    def resolve_transcript_held(obj) -> dict[str, int]:
        session = getattr(obj, "chat_session", None)
        if session is None or not sensitivity.any_sensitive(session, obj):
            return {}
        return session_sync.turn_end_held(session)

    @staticmethod
    def resolve_page_site(obj) -> dict | None:
        from apps.agents.published_profiles import page_turn  # noqa: PLC0415 — as resolve_caller_context: the interface reads models, loaded with a call

        return page_turn(obj)

    @staticmethod
    def resolve_caller_context(obj) -> dict:
        from ..caller_context import build

        return build(obj)


class TurnEventIn(Schema):
    kind: str
    payload: dict = {}


class TurnEventsIn(Schema):
    events: list[TurnEventIn]


class TurnEventOut(Schema):
    seq: int
    ts: dt.datetime
    kind: str
    payload: dict


class TurnEventsOut(Schema):
    events: list[TurnEventOut]


class TurnEventCountOut(Schema):
    count: int


class TurnStartIn(Schema):
    session_id: str = ""


class TurnAbortIn(Schema):
    """End a turn FAILED because it cannot do its work (`turn_abort.abort`)."""
    reason: str  # what failed, in a sentence: the tool's own error is the best evidence
    tool: str = ""  # the tool that failed, e.g. mcp__plugin_ace_ace-gdrive__resolve_opp_path


class TurnFinishIn(Schema):
    status: str  # done|failed|cancelled — a runner's own cancel_turn interrupt
    # finishes the turn cancelled (see the CDP-interrupt cancel flow); done and
    # failed remain the normal completion outcomes.
    result_note: str = ""
    # The session this turn drove, when it drove one (an emdash task on a laptop, a
    # Claude session id on a cloud runner). Written to the turn so the agent's
    # close-out can be matched to it; a failed turn that never got a session simply
    # omits it — which is also what marks it safe to re-run.
    session_key: str = ""
    emdash_task_id: str = LEGACY_SESSION_KEY

    _legacy_key = model_validator(mode="after")(adopt_legacy_session_key)


class TranscriptAppendIn(Schema):
    """Raw `claude -p` JSONL lines to append — one element per JSONL record,
    verbatim (see services.append_transcript). Never re-encoded or parsed."""

    lines: list[str]
    # Optional per-batch idempotency key: if
    # this matches the LAST batch actually applied to the turn, the append is
    # a no-op (a retry after a lost response), not a double-append. Omit to
    # skip dedup entirely — older/simpler callers are unaffected.
    batch_id: str = ""


class TurnMessageOut(Schema):
    turn_index: int
    role: str
    content: dict
    plaintext: str


class TurnMessagesOut(Schema):
    messages: list[TurnMessageOut]
    #: The view stopped at services.TRANSCRIPT_VIEW_MAX_MESSAGES; the raw
    #: transcript route has the rest.
    truncated: bool


class TranscriptAppendOut(Schema):
    line_count: int
    bytes_raw: int
    # True once this turn's transcript has hit services.TRANSCRIPT_TURN_MAX_BYTES
    # (F2) — every batch from here on is silently dropped, so a runner can stop
    # bothering to flush further content for this turn.
    truncated: bool


class TurnSpecIn(Schema):
    """One deferred Turn enqueue. `target_agent=""` means the task's own agent —
    self-dispatch is the default; Ada's fan-out is this field set."""

    prompt: str = ""
    target_agent: str = ""
    origin: Origin = "api"
    origin_ref: dict[str, Any] = Field(default_factory=dict)
    routing: Routing = "prefer_local"

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))
