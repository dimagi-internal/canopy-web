"""Turns, the event ledger, caller/agent tokens and act-as grants.

Framework tier. Split from the former models.py; `apps.harness.models` re-exports.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from .runner import Runner


def agent_turns_q(agent) -> models.Q:
    """Every turn that is this agent's work: the ones targeting it directly, and
    the ones targeting one of its SESSIONS (an email or Slack thread: enqueue_turn
    converts those to a session target, and the XOR constraint leaves `agent`
    NULL). The one definition, so `GET /api/harness/turns/?agent=` and the agent
    card's `turn_count` / `latest_turn_at` cannot count different sets (#359,
    #1087)."""
    return models.Q(agent=agent) | models.Q(agent__isnull=True, chat_session__agent=agent)


class Turn(models.Model):
    """One unit of agent work — the execution envelope around board commands."""

    QUEUED, CLAIMED, RUNNING, NEEDS_HUMAN = "queued", "claimed", "running", "needs_human"
    DONE, FAILED, LOST, MISSED, CANCELLED = "done", "failed", "lost", "missed", "cancelled"
    STATUS_CHOICES = [
        (QUEUED, "Queued"), (CLAIMED, "Claimed"), (RUNNING, "Running"),
        (NEEDS_HUMAN, "Needs human"), (DONE, "Done"), (FAILED, "Failed"),
        (LOST, "Lost"), (MISSED, "Missed"), (CANCELLED, "Cancelled"),
    ]
    TERMINAL = {DONE, FAILED, LOST, MISSED, CANCELLED}
    NON_TERMINAL = {QUEUED, CLAIMED, RUNNING, NEEDS_HUMAN}

    # THE SOURCE VOCABULARY (spec 2026-07-27-source-aware-runner-routing).
    # `origin` is not just provenance any more — it is what per-agent routing
    # rules key on, so the values are the words the operator sees in the routing
    # UI and the turn log. `api` stays the honest catch-all; everything that had
    # a real producer got named.
    ORIGIN_API, ORIGIN_ACE_WEB, ORIGIN_CANOPY_WEB_CHAT = "api", "ace_web", "canopy_web_chat"
    ORIGIN_CANOPY_SCHEDULER, ORIGIN_EMAIL, ORIGIN_SLACK = "canopy_scheduler", "email", "slack"
    # A person's own Claude Code session on their private runner (`apps/harness/local_turns.py`).
    # Opened by the session itself, never claimed, never routed — so it is neither
    # postable nor routable, and it does not serialize the agent (the constraint below).
    ORIGIN_LOCAL = "local"
    ORIGIN_CHOICES = [
        (ORIGIN_API, "API"), (ORIGIN_ACE_WEB, "ace-web"),
        (ORIGIN_CANOPY_WEB_CHAT, "canopy-web chat"),
        (ORIGIN_CANOPY_SCHEDULER, "canopy scheduler"),
        (ORIGIN_EMAIL, "Email"), (ORIGIN_SLACK, "Slack"),
        (ORIGIN_LOCAL, "Local"),
    ]
    # What an external caller may POST. The rest are set by exactly one in-repo
    # producer each, and letting a caller spell them would let it borrow another
    # source's routing rule. Enforced at the request boundary (schemas.Origin),
    # NOT in TurnSpec.from_dict — server-authored dispatch specs (the schedule
    # nag) legitimately carry `canopy_scheduler`.
    POSTABLE_ORIGINS = {ORIGIN_API, ORIGIN_ACE_WEB, ORIGIN_EMAIL, ORIGIN_SLACK}
    # What a caller may name on a CHAT send (a person's `SendIn`, a contact's
    # `ContactSendIn`): `email` and `slack` are channels canopy attests itself, so a
    # chat message calling itself one would borrow that channel's routing rule.
    CHAT_SEND_ORIGINS = {ORIGIN_API, ORIGIN_ACE_WEB}
    # Retired values the live fleet may still be posting. Normalized (same mapping
    # migration 0030 applied to existing rows) rather than 422'd, so shipping this
    # does not break Echo/Ada mid-flight. Applied BOTH at the request boundary and
    # in enqueue_turn: TurnSpec.from_dict parses stored Item JSON with no schema in
    # the path, and Items open across the deploy carry the old spellings. Remove
    # one release after the fleet is confirmed clean.
    LEGACY_ORIGIN_ALIASES = {
        "board": ORIGIN_API, "manual": ORIGIN_API, "drill": ORIGIN_API,
        "cron": ORIGIN_CANOPY_SCHEDULER,
    }
    # What a per-agent routing rule may name. Every value: a rule on a source
    # nothing produces is inert, not harmful, and `slack` is deliberately
    # reserved for the producer that does not exist yet.
    ROUTABLE_ORIGINS = [
        ORIGIN_ACE_WEB, ORIGIN_EMAIL, ORIGIN_CANOPY_SCHEDULER,
        ORIGIN_CANOPY_WEB_CHAT, ORIGIN_SLACK, ORIGIN_API,
    ]

    PREFER_LOCAL, LOCAL_ONLY, ANY = "prefer_local", "local_only", "any"
    # Only a cloud machine that reports it can run jobs (`capabilities.jobs`) takes
    # one: the turn of a plain scheduled job, which an older box would read as an empty
    # prompt and hand to a model. Not offered to a request (`schemas.common.Routing`
    # is its own list); a schedule with a job sets it, and nothing else does.
    CLOUD_ONLY = "cloud_only"
    ROUTING_CHOICES = [(PREFER_LOCAL, "Prefer local"), (LOCAL_ONLY, "Local only"), (ANY, "Any"),
                       (CLOUD_ONLY, "Cloud only")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # A turn targets EITHER an agent or a repo — exactly one (see the
    # target_is_agent_xor_project constraint).
    #
    # `agent.turns` — the ONE turn relation. It was `harness_turns` while
    # apps.agents.AgentTurn held the name for the close-out report; that model is
    # gone (merged into the report fields below), so the obvious name is free and
    # there is no second relation to confuse it with.
    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, related_name="turns",
        null=True, blank=True,
    )
    # The repo case. The session you want to revise from the phone is working on
    # canopy-web — a repo, not an agent (of 22 emdash projects, ~5 are agents).
    # This is a data-model gap, not a missing feature: cdp_control.create_task is
    # already project-generic and has no concept of an agent.
    #
    # A pseudo-agent per repo was rejected: it would serialize repo work behind
    # a lane (emdash gives every task its own worktree, so repo work is *meant*
    # to parallelize) and give every repo KPIs, a needs-you
    # inbox, and a skills catalog it has no meaning for.
    project = models.CharField(max_length=100, blank=True, default="")
    # The chat case: a session turn (the interactive way in). Its tenancy
    # derives from chat_session.workspace, and it serializes per SESSION (not per
    # agent) so two conversations with the same agent don't block each other.
    # Named chat_session (not session) — Turn already has a session_id CharField
    # (the emdash/claude live-session hint). String ref avoids an import cycle
    # (canopy_sessions.services imports harness.services).
    chat_session = models.ForeignKey(
        "canopy_sessions.Session", on_delete=models.CASCADE, null=True, blank=True,
        related_name="turns",
    )
    # Tenancy is DERIVED for agent turns (turn.agent.workspace) and session turns
    # (turn.chat_session.workspace) — denormalized tenancy drifts. A project turn has
    # no agent/session to derive from, so it is the one accepted exception: it
    # carries its own FK, read ONLY when agent and chat_session are both null.
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.PROTECT, null=True, blank=True,
        related_name="project_turns",
        help_text="Set only for project turns, which have no agent to derive tenancy from.",
    )
    #: The ask whose approval enqueued this turn — the other half of the cycle
    #: (AgentTask.raised_by points back). Null for turns with no decision behind
    #: them: the phone composer (a human asking directly), cron schedules, inbox
    #: polls.
    #:
    #: There was a second FK beside this one, `raised_from`, pointing at the
    #: retired `Item` model. Both were written for one release after #873 so a
    #: rollback kept its provenance; measured on labs before removal, every turn
    #: carrying the old edge carried this one too (0 exceptions).
    raised_from_task = models.ForeignKey(
        "agents.AgentTask", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dispatched_turns",
    )
    origin = models.CharField(max_length=32, choices=ORIGIN_CHOICES)
    origin_ref = models.JSONField(default=dict, blank=True)
    prompt = models.TextField(blank=True, default="")
    # A PLAIN JOB (Part 3 of the connections spec): the name of a command in
    # `.canopy/jobs.yaml` of the repo named by `job_repo`. Both blank on every ordinary
    # turn. canopy-web says only these two names; the command is read from the repo
    # by the box that runs it, and no payload carries one. Set by the schedule's fire
    # and "Run now", nowhere else.
    job = models.CharField(max_length=64, blank=True, default="", db_default="")
    job_repo = models.CharField(max_length=100, blank=True, default="", db_default="")
    routing = models.CharField(max_length=15, choices=ROUTING_CHOICES, default=PREFER_LOCAL)
    # Unique per WORKSPACE, never globally (`uniq_turn_idempotency_key_per_scope`):
    # the keys canopy mints are guessable (`sched:<id>:<slot>`, `task-<pk>-<i>`),
    # so a global key let a member of one workspace pre-claim another's future
    # key. Look a key up with `Turn.keyed(key, scope)`.
    idempotency_key = models.CharField(max_length=128)
    # The workspace the key is unique in: `tenant_slug` at creation, stamped by
    # `save()`. A uniqueness scope only — never read as the turn's tenancy, which
    # stays derived (`tenant_slug`). "" only for a row with no workspace at all.
    idempotency_scope = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=QUEUED)
    claimed_by = models.ForeignKey(
        Runner, on_delete=models.SET_NULL, null=True, blank=True, related_name="turns"
    )
    # A HARD pin: only this runner may claim (drills, chat "wait for X", directed
    # placement). Bypasses assignments/capabilities, never the tenant gate or the
    # one-executing-turn constraints. SET_NULL: a deleted runner degrades the pin
    # to normal routing instead of stranding the turn.
    pinned_runner = models.ForeignKey(
        Runner, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="pinned_turns",
    )
    # Where a LOCAL turn ran: the person's own machine (`PrivateRunner`), never a
    # runner. Null for every other origin.
    private_runner = models.ForeignKey(
        "PrivateRunner", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="turns",
    )
    claimed_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    session_id = models.CharField(max_length=64, blank=True, default="")
    result_note = models.TextField(blank=True, default="")
    # How many times this turn has been handed to a runner. Incremented ONLY by the
    # sessionless-failure requeue in services.finish_turn -- an ordinary claim does
    # not touch it, so `attempts > 0` reads as "this turn died before an agent ever
    # saw it, N times" and nothing else.
    attempts = models.PositiveSmallIntegerField(
        default=0,
        help_text="Sessionless retries so far — see services.MAX_SESSIONLESS_RETRIES.",
    )
    # The human who launched this turn, when a person did (a manual / phone-composer
    # enqueue). Null for cron / email / system origins and for dispatched turns.
    # Lets you filter "turns I launched" and notify the launcher when theirs fails.
    enqueued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="turns_enqueued",
    )

    # WHO ASKED, in one shape for every channel — see `apps/harness/initiator.py`
    # and the who-is-asking spec. Distinct from `enqueued_by`, which records the
    # CALLER of the enqueue: for an email that is the runner that posted it, not
    # the person who wrote in. Record-only today; the access phases read these.
    # WHICH PROFILE this turn runs in: "" is the agent's full profile; anything
    # else names its confined profile (read mode or a host site), and the runner and
    # the agent's guard confine the session to it. Decided once, at enqueue,
    # from the initiator — see `apps/agents/access.py::decide`.
    capability = models.CharField(max_length=48, blank=True, default="")
    # What this turn RAN AS: its Turn Context as JSON (`turn_snapshot`), written at each claim, so it holds the run
    # that last happened, and when it is refused before running. Null on a turn older than this: "not recorded".
    # What the rules say NOW is computed on read, never stored. Names and lists only, never the prompt, the
    # envelope's `person` block or a credential's value, and read only through
    # `GET /api/canopy-sessions/{id}/turns/{turn_id}/context`, never on a turn's own schema.
    resolved_context = models.JSONField(null=True, blank=True)
    # The agent CLI its starter or schedule asked for (`clis.refusal` checked it);
    # "" = the agent's default decides. Never written by the claim.
    requested_cli = models.CharField(max_length=32, blank=True, default="", db_default="")
    # The agent CLI the claim picked for it (`clis.choose`), "" while queued: a turn
    # that goes back to the queue is picked for again, so a box that failed to
    # start it on one CLI never holds it to that CLI.
    cli = models.CharField(max_length=32, blank=True, default="", db_default="")
    # A follow-up DELIVERED INTO a conversation's running turn rather than queued
    # behind it (canopy-web#1153): the runner holding that turn typed it into the
    # live session, where Claude Code takes it mid-turn or right after, as it
    # would anything a person typed. It executes beside the turn it rides — the
    # only exception to one_executing_turn_per_session — renews its lease with it
    # and finishes with it (`services.claim_next_turn`, `finish_turn`).
    rides_turn = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="riders",
    )
    initiator_kind = models.CharField(max_length=16, blank=True, default="")
    initiator_via = models.CharField(max_length=64, blank=True, default="")
    initiator_assurance = models.CharField(max_length=32, blank=True, default="")
    initiator_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="turns_initiated",
    )
    initiator_contact = models.ForeignKey(
        "contacts.Contact", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="turns_initiated",
    )
    initiator_agent = models.CharField(max_length=64, blank=True, default="")
    # WHAT created this turn and WHY — apps/harness/provenance.py. The initiator
    # above says who it is FOR; this says through which credential (type, id,
    # label), from which program (`client` = X-Canopy-Client, `user_agent`), on
    # which request (`request_id`, `ip`), via which MCP tool, and under which
    # parent. Stamped once at creation from the request in flight; {} for a turn
    # canopy started itself with no request behind it (a schedule firing).
    # Added after a script on a person's PAT was indistinguishable from the web
    # UI (2026-10-05).
    provenance = models.JSONField(default=dict, blank=True)
    # The turn / session / emdash task / Claude session this turn was started
    # FROM — from the X-Canopy-Parent-* headers, a `parent` object in the
    # payload, a confined session's caller token, or set by canopy itself (a
    # transfer, a re-ask of a lost turn, a dispatch from a task a turn raised).
    # The two FKs are only set when the id resolved; a header's unknown id is kept
    # raw in `provenance["parent"]`, a payload's is a 422 (`provenance.parent_fields`).
    parent_turn = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="child_turns",
    )
    parent_session = models.ForeignKey(
        "canopy_sessions.Session", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="child_turns",
    )
    # An emdash task (`task`) or `host:project:task`.
    parent_task = models.CharField(max_length=300, blank=True, default="")
    parent_claude_session = models.CharField(max_length=100, blank=True, default="")
    # THE MODE THIS TURN RUNS IN (manual | auto), decided once at CLAIM by
    # `apps/harness/turn_mode.py` — a routing rule's mode where one matches, else
    # the agent's own `turn_mode`. "" until claimed, and always "" for a project
    # turn (no agent, no mode). Stamped rather than derived because the agent
    # reads it mid-turn, and a rule edited while the turn runs must not flip the
    # posture of work already under way. `turn_mode_basis` says WHY, in words the
    # agent repeats in its opening line: "agent", "rule email/beth@x.org", or a
    # withheld rule ("rule email/beth@x.org: auto withheld, message not verified").
    turn_mode = models.CharField(max_length=8, blank=True, default="")
    turn_mode_basis = models.CharField(max_length=320, blank=True, default="")
    # A mode ASKED FOR at enqueue by whoever dispatched the turn (`TurnIn.turn_mode`),
    # and who asked. The top rung of `turn_mode.for_turn` — above every routing
    # rule and the agent's own switch — and read again on every claim, so it
    # survives a lease expiry. "" = no request; the ladder decides as before.
    # `auto` is accepted only from the agent's owner/admin on a verified
    # credential, and re-checked at claim (an admin revoked meanwhile gets manual).
    requested_turn_mode = models.CharField(max_length=8, blank=True, default="")
    requested_turn_mode_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="turn_modes_requested",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # ---- the report half (was apps.agents.AgentTurn, merged here 2026-08-11) ----
    #
    # A turn has two halves and they used to be two models: this one recorded the
    # DISPATCH (queued → a session exists), and agents.AgentTurn recorded the
    # agent's own CLOSE-OUT (what it did, what it produced). Nothing joined them,
    # so `/api/agents/<slug>/` served turn_count/latest_turn_at off the close-out
    # table while the queue said something else entirely — ada read `0 turns` with
    # 34 dispatched. Both numbers were "right"; they answered different questions
    # under one name. One model, both halves, so that cannot recur.
    #
    # Every report field is blank-by-default: a turn that was dispatched and never
    # closed out (the agent died, the session was killed) is the NORMAL case, not a
    # defect — it stays a valid row carrying only its dispatch half.
    #
    # `session_key` is the join: the session this turn drove, in the runner's own
    # terms — the same value `RunnerBinding.session_key` holds for that session.
    # A LAPTOP runner's key is the emdash task name (the closing agent recovers it
    # from its cwd, `…/<task>-<suffix>`); a CLOUD runner's is the Claude session id
    # (the closing agent reports it as `cli_session_id`). The runner stamps it the
    # moment the session exists (record-session `turn_id`) and again at finish,
    # first write wins. Before this, the only trace was prose inside result_note
    # ("created session 'hal-api-df02…'"), which is not a key. Not unique: a reused
    # session serves many turns.
    #
    # Named `emdash_task_id` until the cloud runner started writing Claude session
    # ids into it; the wire still accepts that spelling (schemas), the column no
    # longer carries it.
    session_key = models.CharField(
        max_length=200, blank=True, default="",
        help_text="The session this turn drove (emdash task or Claude session id) — the close-out join key.",
    )
    # The Claude Code session id the agent reports at close-out. Distinct from
    # `session_id` above, which is the runner's live-session hint and is in practice
    # only written on the reconciliation path (44/46 lost turns carried one; 13/211
    # done turns did). Kept separate rather than overloaded: one is observed by the
    # runner, the other is asserted by the agent, and conflating them would make a
    # missing value ambiguous.
    cli_session_id = models.CharField(max_length=100, blank=True, default="")
    # The canopy conversation an AGENT or PROJECT turn ran in: the Session its
    # runner's record-session made (or reused) for it, stamped the moment that
    # session exists, first write wins (`session_sync.link_turn_session`). It is
    # what lets someone who dispatched a turn follow up in the same conversation
    # (`POST /api/canopy-sessions/<id>/send`) holding only the turn id. A chat
    # turn leaves it null: `chat_session` already is its conversation, and
    # `TurnOut.canopy_session_id` answers with either.
    canopy_session = models.ForeignKey(
        "canopy_sessions.Session", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="driven_turns",
    )
    report_title =models.CharField(max_length=300, blank=True, default="")
    report_summary = models.TextField(blank=True, default="")
    # The request(s) this turn advanced — AgentTask.ext_id values (loose refs, not FKs).
    task_ext_ids = models.JSONField(default=list, blank=True)
    # Deliverables produced this turn, by url — the turn's own provenance (the
    # work-product table this once pointed at is gone; a project's deliverables
    # now live in its `links`).
    work_product_urls = models.JSONField(default=list, blank=True)
    # Optional transcript link (empty when the turn was packaged without upload).
    # The uploaded Session is owned by the human whose PAT uploaded it; only its
    # slug/token live here, so the two apps stay decoupled.
    session_slug = models.CharField(max_length=64, blank=True, default="")
    share_token = models.CharField(max_length=64, blank=True, default="")
    # When the agent closed out. The presence of this — not any status — is what
    # distinguishes "reported" from "dispatched only": status goes `done` seconds
    # after the session is created, long before the agent has done the work.
    reported_at = models.DateTimeField(null=True, blank=True)
    report_source = models.CharField(max_length=100, blank=True, default="")
    #: When retention (apps/retention) scrubbed this turn's CONTENT: prompt,
    #: result note, report summary, event ledger, raw transcript. The row itself
    #: stays, because that the turn happened is operational metadata that
    #: /activity, KPIs and the schedule lookup read. Null = content intact.
    content_purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["agent", "status"]),
            # The close-out join: newest unreported turn for (agent, session key).
            models.Index(fields=["agent", "session_key"]),
        ]
        constraints = [
            # Key first, so a lookup by key alone (a prefix scan) still uses it.
            models.UniqueConstraint(
                fields=["idempotency_key", "idempotency_scope"],
                name="uniq_turn_idempotency_key_per_scope",
            ),
            # One report per Claude session, carried over from AgentTurn's
            # uniq_agent_turn_session: re-running the close-out (e.g. once the
            # transcript finishes uploading) must update the turn, not add one.
            # PARTIAL — dispatch-only turns all have cli_session_id="" and must
            # not collide with each other.
            models.UniqueConstraint(
                fields=["agent", "cli_session_id"],
                condition=~models.Q(cli_session_id=""),
                name="uniq_turn_agent_cli_session",
            ),
            # An AGENT is not a lane. There used to be a partial unique index here,
            # `one_executing_turn_per_agent`, that let at most one of an agent's
            # turns execute at a time; it was removed (2026-10-30) because nothing
            # needed it and it made a single long-running turn hold every other
            # request for that agent -- a dispatch, a chat message -- queued for
            # hours behind it, on a runner with room to spare. Turns of one agent
            # run side by side, each in its own worktree on the cloud runner.
            #
            # The lanes that remain are the CONVERSATION's (below): two messages in
            # one chat session never execute at once. Whatever an agent's own turns
            # share -- its inbox, its board -- is that agent's procedure to
            # coordinate, not the scheduler's.
            # A session serializes its own execution — one running turn per
            # conversation; NULLs never compare equal, so a turn with no chat
            # session does not participate here. A RIDER (`rides_turn`) is exempt:
            # it executes inside the running turn's session, not as a second one.
            models.UniqueConstraint(
                fields=["chat_session"],
                condition=models.Q(status__in=["claimed", "running", "needs_human"],
                                   rides_turn__isnull=True),
                name="one_executing_turn_per_session",
            ),
            models.CheckConstraint(
                # Exactly one target: agent XOR project XOR chat_session.
                condition=(
                    models.Q(agent__isnull=False, project="", chat_session__isnull=True)
                    | models.Q(agent__isnull=True, chat_session__isnull=True) & ~models.Q(project="")
                    | models.Q(agent__isnull=True, project="", chat_session__isnull=False)
                ),
                name="turn_targets_agent_xor_project_xor_session",
            ),
            # A job names its repo and belongs to an agent's schedule: never a chat
            # session's or a repo's turn, so a conversation cannot become a job.
            models.CheckConstraint(
                condition=(
                    (models.Q(job="", job_repo="")
                     | (~models.Q(job="") & ~models.Q(job_repo="")
                        & models.Q(agent__isnull=False, chat_session__isnull=True)))
                ),
                name="turn_job_shape",
            ),
        ]

    @property
    def target(self) -> str:
        """The emdash project to drive — an agent's slug or a repo's name. The CDP
        layer underneath takes a project name either way. A session turn drives its
        session's agent when it has one, else the session's project (an agentless
        project chat), else a session marker (the cloud runner, SP2b, resolves the
        session directly)."""
        if self.agent_id:
            return self.agent.slug
        if self.chat_session_id:
            if self.chat_session.agent_id:
                return self.chat_session.agent.slug
            if self.chat_session.project:
                return self.chat_session.project
            return f"session:{self.chat_session_id.hex[:8]}"
        return self.project

    @property
    def agent_slug(self) -> str:
        """The owning agent, or "" for a project/session turn.

        `target` above answers the runner's question (what emdash project do I
        drive), which is NOT the same question — it falls back to a session's agent
        and then to a repo name. This one is strictly "whose agent turn is this",
        and it is what the /api/agents serialization reads. harness.TurnOut keeps
        its own resolver, which wins there; the two agree on agent turns.
        """
        return self.agent.slug if self.agent_id else ""

    @property
    def acting_agent(self):
        """The agent this turn runs AS — its own, or its chat's — else None (a
        repo turn, an agentless chat). The one answer routing, the hold gate,
        admission, turn mode and the caller envelope all ask."""
        if self.agent_id:
            return self.agent
        if self.chat_session_id and self.chat_session.agent_id:
            return self.chat_session.agent
        return None

    @property
    def tenant_slug(self) -> str | None:
        """The workspace this turn belongs to: its agent's (agent turns), its chat's
        (session turns), or its own FK (project turns, the one denormalized case).
        None when unset. The one definition — the realtime fan-out, turn access,
        auto-debug, artifact origin and analytics all ask this."""
        if self.agent_id:
            return self.agent.workspace_id  # workspace PK == slug
        if self.chat_session_id:
            return self.chat_session.workspace_id
        return self.workspace_id

    @staticmethod
    def scope_for(*, agent=None, session=None, workspace=None) -> str:
        """The idempotency scope of a turn for this target — its workspace, as
        `tenant_slug` would read it once the turn exists."""
        if agent is not None:
            return agent.workspace_id or ""
        if session is not None:
            return session.workspace_id or ""
        return getattr(workspace, "pk", workspace) or ""

    @classmethod
    def keyed(cls, key: str, scope: str):
        """The turn(s) holding `key` in `scope` — the only way to look a key up."""
        return cls.objects.filter(idempotency_key=key, idempotency_scope=scope)

    def save(self, *args, **kwargs):
        if self._state.adding and not self.idempotency_scope:
            self.idempotency_scope = self.tenant_slug or ""
        super().save(*args, **kwargs)

    def __str__(self) -> str:  # pragma: no cover
        return f"turn:{self.target}:{self.status}:{self.id.hex[:8]}"


class CallerToken(models.Model):
    """The credential a CONFINED session uses for canopy's own MCP — instead of
    the runner owner's personal token.

    Who-is-asking §7: a caller's session must reach canopy's tools as
    `agent ∩ caller`, never as the runner's owner. The runner gets
    one of these with each confined turn it claims, writes it into the session's
    profile (which the session itself cannot read), and the canopy plugin's MCP
    headers helper sends it in place of the PAT. canopy resolves it to the
    conversation's CURRENT turn: that turn's asker is who the tools run as (their
    own ACL, or no canopy user at all for an outside contact), and its
    profile decides which tools exist.

    Bound to the conversation when there is one, because a long confined session
    outlives any single turn and its MCP connection is not re-made per turn.
    Only the hash is stored.
    """

    token_hash = models.CharField(max_length=64, unique=True)
    chat_session = models.ForeignKey("canopy_sessions.Session", on_delete=models.CASCADE,
                                     null=True, blank=True, related_name="caller_tokens")
    turn = models.ForeignKey("Turn", on_delete=models.CASCADE, related_name="caller_tokens")
    #: Minted for a FULL-profile turn asked by someone who is not the agent's
    #: owner, an admin or canopy (an editor): the session is not
    #: confined, but its canopy reads must still be the ASKER's
    #: (who-is-asking phase 5). Such a token lists every tool and runs each as
    #: the conversation's current asker; a confined token never does.
    scoped = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    def __str__(self) -> str:  # pragma: no cover
        return f"caller token for turn {self.turn_id}"


class TurnEvent(models.Model):
    """Append-only per-turn ledger. seq is monotonic per turn (assigned in services)."""

    turn = models.ForeignKey(Turn, on_delete=models.CASCADE, related_name="events")
    seq = models.PositiveIntegerField()
    ts = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=20)  # status|assistant|tool_start|tool_end|question|error|heartbeat
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["seq"]
        constraints = [
            models.UniqueConstraint(fields=["turn", "seq"], name="turnevent_seq_unique_per_turn")
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"evt:{self.turn_id}:{self.seq}:{self.kind}"


class TurnTranscript(models.Model):
    """The retained raw `claude -p` JSONL for one turn, gzipped.

    Sibling of TurnEvent (append-only ledger), NOT a column on Turn — blobs
    don't belong on the hot row. TurnEvent stays a deliberately reduced live
    stream (assistant text, tool start/end); this is the durable artifact
    cost/structure features re-derive from, so it must hold the CLI's output
    byte-for-byte. canopy stores it unparsed — interpreting it is the consumer's
    business (see services.append_transcript / read_transcript); canopy's own
    one reader is the bounded reading view, services.transcript_messages.

    `raw_jsonl_gz` may hold MULTIPLE concatenated gzip members (one per
    `append_transcript` batch) rather than a single re-compressed blob —
    `gzip.decompress` reassembles a concatenated multi-member stream
    transparently, so this is format-compatible with earlier single-member
    rows. See services.append_transcript for why (O(1) appends instead of
    decompress-all + recompress-all under the Turn row lock).

    RETENTION (cross-turn): not this model's job. `apps/retention` deletes a
    turn's row here once its rule's `keep_days` pass (spec
    2026-10-05-content-retention-design). With no rule, or enforcement off,
    the table still grows forever.

    SIZE (per-turn, bounded — security review 2026-07-26, F2): a single turn's
    content IS capped at `services.TRANSCRIPT_TURN_MAX_BYTES` — a different
    axis from the retention gap above. `raw_jsonl_gz` is Postgres `bytea`
    (1GB hard limit) and `bytes_raw` a `PositiveIntegerField` (2GB), and both
    would otherwise eventually raise a raw DB error mid-turn for a
    long-running/tool-heavy session with no upstream signal. `truncated`
    latches permanently once a turn crosses the ceiling — `append_transcript`
    writes one synthetic marker line at the crossing point and silently drops
    everything after, rather than 4xx-ing a turn that is still executing (its
    transcript getting long is not a reason to fail a live run).

    `last_batch_id` is a single-slot idempotency guard (F5): if a runner's
    flush POST commits but the response is lost, its retry re-sends the SAME
    batch — recognized here and dropped rather than double-appended (this
    store exists to derive cost, so silent duplication would inflate it with
    no way to detect it after the fact). Only guards against replaying the
    IMMEDIATELY PRECEDING batch, not an arbitrary older one replayed later.
    """

    turn = models.OneToOneField(Turn, on_delete=models.CASCADE, related_name="transcript")
    raw_jsonl_gz = models.BinaryField()
    line_count = models.PositiveIntegerField(default=0)
    bytes_raw = models.PositiveIntegerField(default=0)
    truncated = models.BooleanField(default=False)
    last_batch_id = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"transcript:{self.turn_id}:{self.line_count}L:{self.bytes_raw}B"


class PendingMail(models.Model):
    """Mail on a thread held for its trigger's quiet window (`AgentSchedule.
    mail_quiet_minutes`, apps/harness/mail_coalesce.py): the newest message's
    enqueue, kept until `due_at`, when ONE turn starts carrying all `count` of them.
    One per thread session; deleted when its turn is enqueued."""

    session = models.OneToOneField("canopy_sessions.Session", on_delete=models.CASCADE,
                                   related_name="pending_mail")
    trigger = models.ForeignKey("harness.AgentSchedule", null=True, on_delete=models.SET_NULL,
                                related_name="+")
    # The newest message's enqueue_turn arguments, replayed at due_at.
    payload = models.JSONField(default=dict)
    # How many messages are held, and their idempotency keys (a re-post is no new message).
    count = models.PositiveIntegerField(default=1)
    keys = models.JSONField(default=list)
    due_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"pending-mail:{str(self.session_id)[:8]}:{self.count}@{self.due_at:%H:%M}"


class FailureInvestigation(models.Model):
    """One KIND of turn failure, and what the fleet's debugger agent did about it.

    Jonathan, 2026-10-07: an ACE Slack turn ended FAILED with the runner's
    "Couldn't confirm your message was delivered…" note, Slack showed "❌ ace
    could not finish this", and nothing in the fleet looked at it until a human
    did. A board task would have waited for a turn to read it; the ask was that a
    failure IMMEDIATELY starts a turn of the conductor agent (`ada`) to
    investigate, learn and fix — `apps/harness/auto_debug.py` is that hook, and
    this row is its memory.

    Keyed on a FINGERPRINT of the failure (the result note with every quoted
    string, id, number, path and name normalized away), not on the turn: the same
    breakage on three agents is one thing to fix, so it is one row whose
    `occurrences` grows — never three turns. The row is also what keeps the
    feedback loop from running away: a repeat only counts, a fix that did not hold
    escalates once and then only counts, and a fleet-wide cap holds anything over
    the limit here (`held`) instead of starting a turn.
    """

    OPEN = "open"                        # a debug turn has it (or is about to)
    HELD = "held"                        # wanted a turn; the rate cap said no
    DEBUGGER_FAILED = "debugger_failed"  # the debug turn itself failed
    RESOLVED = "resolved"                # fixed, and (if a fix commit was named) live
    #: Fixed by `fix_commit`, which some runner it hit is not running yet. A
    #: recurrence on such a runner only counts; it closes on its own once every
    #: runner it hit runs the fix (`auto_debug.close_live_fixes`).
    PENDING_DEPLOY = "pending_deploy"
    #: With a PERSON (`handler`): the debugger handed it off, or it came back on
    #: a runner that runs the fix. Only a person — or the fix going live — closes
    #: it; the debugger's resolve is refused. (Value kept from when this meant
    #: "came back after a fix".)
    ESCALATED = "escalated"
    STATUS_CHOICES = [
        (OPEN, "Open"), (HELD, "Held by the rate cap"),
        (DEBUGGER_FAILED, "Debug turn failed"), (RESOLVED, "Resolved"),
        (PENDING_DEPLOY, "Fix pending deploy"), (ESCALATED, "With a person"),
    ]

    #: The debugger agent's workspace. Every failure it hears about is in this
    #: workspace or below it (auto_debug refuses the rest), so this is the tenant
    #: the evidence belongs to — NOT NULL, the nullable-tenant-FK bug class.
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="failure_investigations",
    )
    fingerprint = models.CharField(max_length=64, unique=True)
    normalized_note = models.TextField(blank=True, default="")
    #: The latest raw note, verbatim — what the debugger actually reads.
    sample_note = models.TextField(blank=True, default="")
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    occurrences = models.PositiveIntegerField(default=1)
    #: The most recent failed turns (ids, newest last), capped — evidence, not a log.
    turn_ids = models.JSONField(default=list, blank=True)
    agents = models.JSONField(default=list, blank=True)
    runners = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=OPEN)
    #: It recurred after being resolved: the next (or this) debug turn is framed
    #: "the fix did not hold — escalate to Jonathan", and the row ends ESCALATED.
    recurred_after_resolve = models.BooleanField(default=False)
    #: The latest debug turn that carries this investigation (its own, or one it
    #: rode along in while held).
    debug_turn = models.ForeignKey(
        Turn, on_delete=models.SET_NULL, null=True, blank=True, related_name="investigations",
    )
    #: Debug turns started for it so far — the `n` in its idempotency key.
    triggers = models.PositiveSmallIntegerField(default=0)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    #: The commit that fixes it, when the fix is to the runner code that failed —
    #: and its committer time, the ORDER a sha cannot carry (`Runner.code_committed_at`).
    #: A recurrence is judged against the failing runner: without the fix it only
    #: counts. Investigations 1, 2 and 10 (2026-10-22..23) were resolved "merged,
    #: NOT yet deployed" (#1488, #1504, #1527, #1965); investigation 2's 10-23
    #: recurrence ran on a box without the fix and was escalated as "the fix did
    #: not hold".
    fix_commit = models.CharField(max_length=40, blank=True, default="")
    fix_committed_at = models.DateTimeField(null=True, blank=True)
    #: The PR that carries the fix, as the debugger gave it. Legible only.
    fix_pr = models.CharField(max_length=300, blank=True, default="")
    #: The person it is with (status `escalated`), and the one line they are asked.
    handler = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    ask = models.CharField(max_length=300, blank=True, default="")
    #: What canopy knew about the failing runner when it saw the failure
    #: (`auto_debug.runner_snapshot`) — refreshed on each trigger and rendered
    #: into the debug prompt. Diagnostic fields only; nothing a person typed.
    evidence = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [models.Index(fields=["status", "-last_seen"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"investigation:{self.pk}:{self.status}:{self.fingerprint[:8]}"


class TurnAgentToken(models.Model):
    """An agent login a runner was given for ONE turn it holds (turn-scoped agent token).

    `POST /api/harness/runners/{rid}/turns/{tid}/agent-token` mints a `PersonalToken`
    for the turn's `Agent.user` and records it here, so every token of a turn can be
    revoked when the turn stops executing on that runner (`turn_agent_tokens`). It
    replaces the agent's stored, never-expiring `canopy-pat` for runner turns. A laptop
    turn finishes as soon as its session exists, so there the login is SESSION-scoped
    instead (`session`): it lives while that session is live on the runner.
    Spec: docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md.
    """

    turn = models.ForeignKey(Turn, on_delete=models.CASCADE, related_name="agent_tokens")
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="+")
    token = models.OneToOneField("tokens.PersonalToken", on_delete=models.CASCADE,
                                 related_name="turn_grant")
    #: Set for a SESSION-scoped login (a laptop turn whose session outlives it): held
    #: while this session stays active and bound to `runner`, not while the turn runs.
    #: `turn` is then the turn that opened it — who started the work it acts in.
    session = models.ForeignKey("canopy_sessions.Session", on_delete=models.CASCADE, null=True,
                                blank=True, related_name="agent_tokens")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"turn-agent-token:{self.turn_id}"


class ActAsGrant(models.Model):
    """An agent, in a turn a person started, acting AS that person on canopy-web — once
    they approved it with their passkey (`apps/harness/act_as.py`).

    The agent asks (`POST /api/me/act-as-requests/`, with its turn's own login); canopy-web
    works out who the person is from that login (`turn_asker`), never from anything the
    agent sends. The person approves at `/act-as/<id>` with a fresh WebAuthn assertion,
    choosing read-only or read + write; from that touch the agent's token signs in as
    them for `act_as.LIFETIME` and never longer, and only while `turn` is still executing.
    The token is returned to the agent once and only its hash is kept.
    """

    READ, WRITE, EITHER = "read", "write", "either"
    REQUESTABLE = (READ, WRITE, EITHER)
    GRANTABLE = (READ, WRITE)
    PENDING, APPROVED, DECLINED, EXPIRED, STOPPED = "pending", "approved", "declined", "expired", "stopped"
    STATUSES = (PENDING, APPROVED, DECLINED, EXPIRED, STOPPED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The person the agent acts as: the turn's verified human asker (`turn_asker`).
    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                               related_name="act_as_grants")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="act_as_grants")
    #: The turn it was asked from. The token dies with it.
    turn = models.ForeignKey(Turn, on_delete=models.CASCADE, related_name="act_as_grants")
    #: The turn login (`TurnAgentToken.token`) that asked: only it reads the status and
    #: collects the token.
    login_token_id = models.BigIntegerField()
    #: The conversation it was asked in (the turn's chat session, or a laptop's
    #: session-scoped login's session), if any. The request and the permission then live
    #: while that session is active and its newest turn is still the person's — not only
    #: while `turn` executes — and any later turn login of the agent there may collect it.
    login_session = models.ForeignKey("canopy_sessions.Session", on_delete=models.CASCADE,
                                      null=True, blank=True, related_name="+")
    login_runner = models.ForeignKey(Runner, on_delete=models.CASCADE, null=True, blank=True,
                                     related_name="+")
    #: The agent's words, verbatim — the person's to read; never put in an admin event.
    reason = models.CharField(max_length=500)
    requested_scope = models.CharField(max_length=8)
    #: What the person granted (`read` | `write`); blank until approved.
    scope = models.CharField(max_length=8, blank=True, default="")
    status = models.CharField(max_length=10, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    #: A request nobody acted on lapses at this time.
    request_expires_at = models.DateTimeField()
    #: The passkey touch that approved it; the 30 minutes run from here.
    approved_at = models.DateTimeField(null=True, blank=True)
    #: approved_at + LIFETIME, set once at approval and never moved.
    expires_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    #: Who stopped it: "agent" | "person".
    stopped_by = models.CharField(max_length=8, blank=True, default="")
    #: The token's hash. Set when the agent collects it — once.
    token_hash = models.CharField(max_length=64, unique=True, null=True, blank=True)
    token_issued_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    #: Devices the push service accepted the request's notification for; 0 none, -1 failed,
    #: null not tried.
    push_sent = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["person", "status"]), models.Index(fields=["status", "expires_at"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"act-as:{self.pk}"
