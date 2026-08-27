"""Live chat sessions — the interactive front-door to a durable harness Turn.

A Session is a conversation thread. A "send" enqueues a harness Turn (target=
session); the assistant's output lands in the TurnEvent ledger and is projected
here as Message rows. Framework tier, agent-agnostic: `metadata` carries opaque
product linkage (e.g. ace-web's opp_slug) the framework never interprets.

See docs/superpowers/specs/2026-07-16-sp2-unified-execution-spine-design.md.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Session(models.Model):
    ACTIVE, ARCHIVED = "active", "archived"
    STATUS_CHOICES = [(ACTIVE, "Active"), (ARCHIVED, "Archived")]

    # Provenance: was the session started in-app (web) or discovered on a
    # runner (runner)? Independent of which runner backs it.
    ORIGIN_WEB = "web"
    ORIGIN_RUNNER = "runner"
    ORIGIN_CHOICES = [(ORIGIN_WEB, "Web"), (ORIGIN_RUNNER, "Runner")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # You chat WITH an agent (nullable — a session can be agent-agnostic).
    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="chat_sessions",
    )
    # The tenant. Unlike agent turns (which derive tenancy via agent.workspace),
    # a session carries its own workspace so an agent-less session still has one.
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, related_name="chat_sessions",
    )
    # The repo checkout this session drives (the emdash project name), for an
    # agentless PROJECT chat. A bare string mirroring Turn.project — NOT a FK to
    # projects.Project, so this framework-tier app never imports product code. A
    # session targets an agent XOR a project (or neither).
    project = models.CharField(max_length=100, blank=True, default="")

    @property
    def emdash_project(self) -> str:
        """The emdash PROJECT this session's worktree lives under.

        Not the same question as `project`, which is only set for an agentless
        repo chat. An agent chat leaves it blank — but its worktree is still
        under a project, and that project is the agent's own repo:
        `~/emdash/worktrees/hal/emdash/hal-canopy-web-chat-…`.

        This exists because the runner resolves a transcript by (project, task),
        and every caller that shipped a bare `session.project` silently sent ""
        for agent sessions. `resolve_transcript` then returned None and the
        caller `continue`d, so agent chats were never streamed and never
        backfilled — permanently, and with no error anywhere. Measured on labs
        2026-08-01: a fresh hal session sat at zero durable rows through a 7-minute
        backfill wait, and "load full session" could not work for any agent chat.
        """
        return self.project or (self.agent.slug if self.agent_id else "")
    title = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=ACTIVE)
    origin = models.CharField(max_length=10, choices=ORIGIN_CHOICES, default=ORIGIN_WEB)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+",
    )
    # Continuity hint for the real subprocess runner (SP2b): the claude CLI
    # session to --resume. Unused by the stub.
    cli_session_id = models.CharField(max_length=64, blank=True, default="")
    # Which transcript-ordinal scheme this session's Message rows were written
    # under. 0 = the original "turn_index == raw .jsonl record ordinal"; 1 = the
    # composite `record * BLOCK_STRIDE + block` that made a multi-block record's
    # tool calls addressable. A session still on an older scheme is re-derived
    # from its transcript on the next write (see _ensure_current_ordinal_scheme)
    # — the rows are a cache of a file on the runner's disk, so a rebuild is the
    # cheap, self-healing move, not a migration to hand-write.
    ordinal_scheme = models.PositiveSmallIntegerField(default=0)
    # Opaque product linkage (e.g. {"opp_slug": "..."}) — never interpreted here.
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                # Never both — you chat WITH an agent, or IN a project, not both.
                condition=models.Q(agent__isnull=True) | models.Q(project=""),
                name="chat_session_not_agent_and_project",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"session:{self.id.hex[:8]}:{self.status}"


class Message(models.Model):
    """A projected transcript row. User messages are written at send time; the
    rest are materialized from the TurnEvent ledger by the projection receiver.
    turn_index is monotonic per session (a session-wide order across turns)."""

    USER, ASSISTANT, TOOL_USE, TOOL_RESULT, SYSTEM = (
        "user", "assistant", "tool_use", "tool_result", "system",
    )
    ROLE_CHOICES = [
        (USER, "User"), (ASSISTANT, "Assistant"), (TOOL_USE, "Tool use"),
        (TOOL_RESULT, "Tool result"), (SYSTEM, "System"),
    ]

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="messages")
    # Null for user messages (a human send precedes any turn execution).
    turn = models.ForeignKey(
        "harness.Turn", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="messages",
    )
    turn_index = models.PositiveIntegerField()
    role = models.CharField(max_length=12, choices=ROLE_CHOICES)
    content = models.JSONField(default=dict, blank=True)
    plaintext = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["turn_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "turn_index"], name="message_index_unique_per_session"
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"msg:{self.session_id.hex[:8]}:{self.turn_index}:{self.role}"


class SessionParticipant(models.Model):
    """Durable membership + role in a session (SP3 multiplayer). Presence — who is
    here *right now* — is ephemeral and lives in the cache (apps/canopy_sessions/presence.py);
    this row is the authority for access and role."""

    OWNER, EDITOR, VIEWER = "owner", "editor", "viewer"
    ROLE_CHOICES = [(OWNER, "Owner"), (EDITOR, "Editor"), (VIEWER, "Viewer")]

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=EDITOR)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session", "user"], name="one_participant_per_session_user"
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"participant:{self.session_id.hex[:8]}:{self.user_id}:{self.role}"


class Draft(models.Model):
    """The shared, co-edited outgoing message (SP3 multiplayer). One OPEN draft
    (slot='next') per session; an optimistic `version` guards concurrent edits, and
    the soft-lock holder is derived (last_editor + updated_at + presence), not stored."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name="drafts")
    slot = models.CharField(max_length=16, default="next")
    body = models.TextField(blank=True, default="")
    version = models.PositiveIntegerField(default=0)
    last_editor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["session"],
                condition=models.Q(slot="next"),
                name="one_open_draft_per_session",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"draft:{self.session_id.hex[:8]}:v{self.version}"


class RunnerBinding(models.Model):
    """The live pointer from a Session to the runner currently backing it, plus
    the cheap tail read-model. Absorbs the old harness.EmdashSession. Null when
    nothing is live for the session."""

    session = models.OneToOneField(
        Session, on_delete=models.CASCADE, related_name="runner_binding"
    )
    runner = models.ForeignKey(
        "harness.Runner", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="session_bindings",
    )
    # Engine-agnostic handle the runner uses to resume/inject (was emdash_task).
    session_key = models.CharField(max_length=255, blank=True, default="")
    # The other half of that handle: the emdash PROJECT the task lives under, i.e.
    # a cache of `session.emdash_project` (`project`, or the agent's slug for an
    # agent chat). It is here because `session_key` alone is NOT an identity —
    # emdash task names are scoped to a project, and the same name in two projects
    # is two different conversations.
    #
    # Every OTHER part of the system already keys on the pair: the runner resolves a
    # transcript by (project, task), which is why `get_session_streams` ships
    # `session.emdash_project` alongside `session_key`. Only the report loop's own
    # upsert keyed on the bare name, so the two collapsed into one row — and because
    # the report deduplicates before it upserts, the loser was not merely mislabelled
    # but dropped from the report entirely, invisible on the web with no error
    # anywhere. Measured on labs 2026-08-27: a session titled "issues" reported
    # `project: "ace"` while serving a connect-labs transcript (its live tail was
    # `gh issue close 1195 -R dimagi-internal/connect-labs`), because an `issues`
    # task under `ace` on 2026-08-14 had claimed the name first.
    #
    # Denormalised rather than joined because it is half of a UNIQUE constraint, and
    # a constraint cannot span tables. Backfilled from `session.emdash_project` in
    # 0021; written by the report loop and by `record_session` thereafter. Blank is a
    # real value (a runner that sends no project), and blanks match each other, so a
    # deployment that never reports a project degrades to exactly today's behaviour.
    emdash_project = models.CharField(max_length=100, blank=True, default="")
    # Durable thread identity (absorbed from SessionLink). For a chat session this
    # is str(session.id); for a phone/agent/project thread it's the topic key
    # (e.g. "phone:jj:canopy-web" or "<target>:<turn_id>"). The reuse lookup keys on
    # (session's target, thread_key).
    thread_key = models.CharField(max_length=255, blank=True, default="", db_index=True)
    # The macOS host that owns the live session — emdash is per-macOS-account, so a
    # session is reusable ONLY by the runner whose host matches (two-account failover).
    host = models.CharField(max_length=200, blank=True, default="")
    # Durable board-task context carried for rehydration (was SessionLink.agent_task_ext_id).
    agent_task_ext_id = models.CharField(max_length=255, blank=True, default="")
    # WHICH transcript this session's Message rows were derived from — the Claude
    # session uuid (the .jsonl stem), reported by the runner on every ship.
    #
    # `session_key` is the emdash task NAME, and names are reused. `emdash_project`
    # above settles the CROSS-project half of that (two live `issues` tasks in two
    # repos are now two rows); this field owns the half no key can settle — reuse
    # within one project OVER TIME. Close a task called "bednet" and start another
    # under the same name in the same repo and this binding is legitimately
    # re-pointed at the new conversation, while the old one's rows stay attached.
    # That alone renders one session as another; worse, `turn_index` is a PER-FILE ordinal, so the
    # first_index/last_index markers computed off the old file are meaningless
    # against the new one. Measured on prod 2026-08-14 (issue #615): a 593-record
    # predecessor left last_index=37,696 against a live 384-record transcript whose
    # highest possible ordinal was 24,575, so every record of the live session sat
    # BELOW the marker and the runner shipped nothing — the panel was pinned to a
    # day-old conversation with no way to self-heal.
    #
    # Recording the identity makes the mismatch detectable, which is the whole
    # point: on a change the derived rows are dropped and re-derived (see
    # services.ensure_transcript_identity), exactly as `Session.ordinal_scheme`
    # already does for a change of ordinal SCHEME. Blank = never reported (an old
    # runner, or a session whose runner has not shipped since this landed).
    transcript_id = models.CharField(max_length=100, blank=True, default="")
    # Liveness: a viewer is attached, so the bound runner should stream this
    # session's events up live. Toggled by the attach registry on the 0<->1 edge.
    stream_desired = models.BooleanField(default=False)
    # On-demand history promotion: the client asked for full history on a local
    # session with no Message rows. The bound runner ships its transcript, the
    # server writes rows once, and clears this. (Server-full is then inferred from
    # Message existence — no second flag.)
    backfill_requested = models.BooleanField(default=False)
    tail = models.JSONField(default=list)          # last N conversational messages
    # The dialog this session is blocked on, or None. Re-derived from the
    # transcript on every session report, so it is a CACHE of what the agent is
    # waiting on rather than a record — the same relationship `tail` has, and
    # for the same reason: the transcript is the durable thing.
    #
    # It lives here, not on Session, because it is a property of the LIVE
    # session on a particular box. A session with no binding has no screen to be
    # blocked on, and one whose binding moved is answered on the new runner.
    pending_question = models.JSONField(null=True, blank=True, default=None)

    # A human's answer, waiting for the runner to press it.
    #
    # `{"id": "<uuid>", "option": <int|null>, "at": <epoch>}`.
    #
    # The answer used to exist ONLY as a WebSocket control frame. When that
    # channel is down the frame lands in a Channels group with no consumer and is
    # discarded — while the runner still heartbeats over REST, so it reads ONLINE,
    # `is_reachable` is true, and the API answers `ok:true`. Nothing anywhere
    # records that the tap was lost. Measured on labs 2026-08-01: the control
    # channel reconnected at 10:16 and again at 10:58, and an answer sent at 10:50
    # never reached the runner at all — no keystroke, no refusal, no log line.
    # That is the purest form of "clicking does nothing".
    #
    # So the frame becomes the doorbell and this is the record: the runner drains
    # it on the poll tick it already runs, exactly as it does for backfills and
    # streams. A dropped channel now costs one tick of latency instead of the
    # answer. Same "push is the doorbell, the timer is the auditor" shape as
    # inbound mail and runner updates.
    #
    # Carries an `id` because applying an answer twice means a SECOND keystroke
    # into a session that has moved on: the runner echoes the id back and the
    # server clears only that one.
    pending_answer = models.JSONField(null=True, blank=True, default=None)

    # A close the runner has not carried out yet — same reasoning as
    # `pending_answer`, for the other verb that only ever existed as a WS frame.
    # Verified 2026-08-01 by sending a real close: the API answered
    # `{"ok":true,"closing":true}` and the runner logged nothing at all — no
    # attempt, no error — so the emdash task stayed open and the session stayed
    # active forever. `/close`'s own fallback ("the task's plain absence from the
    # following report retires it anyway") assumes the runner DELETED the task,
    # which never happens if the frame is lost.
    close_requested = models.BooleanField(default=False)
    summary = models.TextField(blank=True, default="")
    status = models.CharField(max_length=40, blank=True, default="")
    # The ENGINE's own answer to "is this session working right now" — emdash's
    # per-conversation `agent_status` ("working" | "awaiting-input"), reported every
    # tick. Blank means the runner could not answer (predates the field, no emdash,
    # drifted schema), NOT "idle": `is_session_running` falls back to activity
    # recency for a blank, and only trusts a non-blank value.
    agent_status = models.CharField(max_length=40, blank=True, default="")
    # The runner's dissent from `agent_status`: it says "not working", but the session
    # is still writing. emdash's flag has no way back to "working" without a human
    # prompt, so a turn that ended only to hand off to a background subagent leaves it
    # pinned at "completed" for the rest of the session — see
    # `is_session_running` and canopy_runner.sessions.annotate_engine_staleness.
    agent_status_stale = models.BooleanField(default=False)
    last_interacted_at = models.DateTimeField(null=True, blank=True)
    live_seen_at = models.DateTimeField(null=True, blank=True)
    # Stamped ONLY by the report loop (apps/harness/services.py::
    # replace_reported_sessions) — deliberately NOT by record_session, which BOTH
    # runners call. That asymmetry is the whole point: it makes "a runner is
    # reporting an emdash task for this session" an observable fact rather than
    # something inferred from Runner.kind (which answers "what program is this",
    # a different question, and is already deprecated as a behavioural input).
    # `live_seen_at` cannot answer it — the cloud runner's record_session stamps
    # that too, and writes a Claude session id into session_key where the laptop
    # writes an emdash task name. Read against the same stale_cutoff() the session
    # list uses, so "reported" and "live" can never mean different windows.
    reported_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_interacted_at"]
        constraints = [
            # (runner, emdash_project, session_key) — NOT (runner, session_key).
            # See `emdash_project` above: the bare name is not an identity, and a
            # constraint on it forces two projects' same-named tasks to share one
            # binding. Widening the key can never break an existing row.
            models.UniqueConstraint(
                fields=["runner", "emdash_project", "session_key"],
                condition=models.Q(runner__isnull=False) & ~models.Q(session_key=""),
                name="one_binding_per_runner_project_session_key",
            ),
        ]

    def __str__(self) -> str:
        return f"binding<{self.session_key}>"

    def reusable_by(self, runner) -> bool:
        """True if this runner owns the live session (same runner + same macOS host)
        and a concrete session_key is recorded. The runner STILL verifies the task
        exists in its own emdash before driving it — this is the server-side gate.
        Ported verbatim from the retired SessionLink.reusable_by."""
        return bool(
            self.session_key
            and self.runner_id == runner.id
            and self.host
            and self.host == runner.host
        )


class Attachment(models.Model):
    """A file a human attached to a chat — today, a screenshot they want the agent
    to look at.

    Bytes live in S3 (`storage_key`), never in the row: they are handed to the
    browser to render inline AND downloaded by the runner into the agent's
    workspace, so both readers stream from one place.

    The lifecycle is upload-then-bind. An attachment is created UNBOUND
    (`message` null) the moment the file lands, because the composer uploads
    while you are still typing — the message it belongs to does not exist yet.
    Sending binds it. An unbound attachment is therefore either in-flight or
    abandoned (you attached, then closed the tab); it is scoped to its session so
    it can never leak into another conversation, and is safe to sweep.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        Session, on_delete=models.CASCADE, related_name="attachments"
    )
    # SET_NULL, not CASCADE: losing the message must not silently destroy bytes
    # the agent may still be reading. The row becomes unbound and sweepable.
    message = models.ForeignKey(
        Message, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="attachments",
    )
    uploaded_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="chat_attachments",
    )
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    # Where the bytes are in the bucket. Stored rather than derived so the key
    # scheme can change without orphaning everything written under the old one.
    storage_key = models.CharField(max_length=500)
    # When this attachment was included in a send. `message` cannot carry that
    # on its own: a RUNNER-origin session writes NO user Message row (the
    # transcript is its durable source), so those attachments would stay
    # message=NULL forever and be swept into every subsequent send. "Pending"
    # is therefore sent_at IS NULL, not message IS NULL.
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["session", "message"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"attachment:{str(self.id)[:8]}:{self.filename}"
