"""Agent-execution harness: runner registry, turns, and the turn-event ledger.

Framework tier. A Turn is the execution envelope for one unit of agent work;
Runners are executors that dial out (heartbeat + claim); TurnEvents are the
append-only ledger. See docs/superpowers/specs/2026-07-05-agent-execution-
control-plane-design.md.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from apps.common.token_hash import hash_token

# Owned here (not services.py) so Runner.live_status can use it without a
# models -> services import cycle (services already imports models). services.py
# imports this constant from here rather than declaring a second one.
HEARTBEAT_ONLINE_WINDOW = dt.timedelta(seconds=90)


class Runner(models.Model):
    """A paired executor (laptop emdash daemon, cloud container, remote box)."""

    EMDASH, CLOUD, REMOTE = "emdash", "cloud", "remote"
    # A person's COMPUTER used in plain Claude Code, with no runner app: the
    # lightweight runner a `PrivateRunner` gets when it is registered without one,
    # so the machine can hold a device key — sign its owner's local sessions in and
    # read a zero-knowledge agent's vault under their license. It has no program
    # behind it: it never heartbeats, claims or takes a pin, and it is not a fleet
    # runner, so every fleet list (supervisor, routing pickers, workspace order,
    # health, topology, drills) leaves it out. Always `private`, never enrolled.
    COMPUTER = "computer"
    KIND_CHOICES = [(EMDASH, "Emdash"), (CLOUD, "Cloud"), (REMOTE, "Remote"),
                    (COMPUTER, "Computer (Claude Code, no runner app)")]

    # Environment (first-class; the persistence tier derives from `location`).
    LOCAL = "local"
    CLOUD = "cloud"
    LOCATION_CHOICES = [(LOCAL, "Local"), (CLOUD, "Cloud")]
    # The session runtime a LAPTOP runner opens new sessions in (canopy-web#1188).
    # Set here by the runner's owner or admins, read back by the runner off every
    # heartbeat, so flipping it needs no restart and no shell on the box. It only
    # decides where NEW sessions go: a thread already running in one runtime keeps
    # its session there. Not a kind — the box, its routing and its repos are the
    # same either way. (A `desktop` kind briefly existed for a separate desktop
    # daemon; it was folded into the laptop runner as this setting.)
    ENGINE_EMDASH = "emdash"
    ENGINE_CLAUDE_DESKTOP = "claude-desktop"
    # The runner runs Claude itself, headless over ACP (the cloud runner's
    # executor), and never opens an app or needs emdash: a person opens a session
    # in the Claude desktop app from the IDE when they want it there. Runtimes are
    # named <vendor>-<surface>, so another vendor's CLI gets its own value.
    ENGINE_CLAUDE_CLI = "claude-cli"
    ENGINE_CHOICES = [(ENGINE_EMDASH, "emdash"), (ENGINE_CLAUDE_DESKTOP, "Claude desktop"),
                      (ENGINE_CLAUDE_CLI, "Claude CLI (headless)")]

    ONLINE, STALE, DISCONNECTED, DEGRADED, RETIRED = (
        "online", "stale", "disconnected", "degraded", "retired",
    )
    # DERIVED ONLY — never stored in `status`, which holds what the runner last
    # self-reported. Served by `live_status` so that every existing consumer of
    # "is this box online?" gets the pause for free (see `paused` below).
    PAUSED = "paused"
    STATUS_CHOICES = [
        (ONLINE, "Online"), (STALE, "Stale"), (DISCONNECTED, "Disconnected"),
        (DEGRADED, "Degraded"), (RETIRED, "Retired"), (PAUSED, "Paused"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    location = models.CharField(
        max_length=16, choices=LOCATION_CHOICES, default=LOCAL,
        help_text="Where the runner runs. Drives the session persistence tier.",
    )
    engine = models.CharField(
        max_length=32, choices=ENGINE_CHOICES, default=ENGINE_EMDASH,
        help_text="The agent engine this runner drives (not assumed to be emdash).",
    )
    capabilities = models.JSONField(default=dict, help_text='e.g. {"agents": ["echo"]}')
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=DISCONNECTED)
    status_note = models.CharField(max_length=255, blank=True, default="")
    # Can this runner actually FIRE a turn right now — distinct from being online.
    # Set from the heartbeat: the runner self-reports cdp_healthy() AND not-recently-
    # failed. `available = live_status == ONLINE and ready` (the Phase B cascade gate).
    # Defaults True so an un-upgraded runner reads as able to fire, matching prior behavior.
    ready = models.BooleanField(default=True)
    ready_note = models.CharField(max_length=200, blank=True, default="")
    # THE pause state — one value, settable from either end, and the source of
    # truth for both. Jonathan runs the fleet under two macOS accounts for
    # token-limit failover (see `host`), and moving work off a rate-limited account
    # means silencing its runner from the other one; nothing could do that before,
    # because ~/.canopy there is owned by the other account.
    #
    # Set it remotely via /pause + /unpause, or locally by dropping the runner's
    # `~/.canopy/PAUSED` sentinel (the menu-bar toggle). The local file is a CONTROL
    # SURFACE, not a second state: the runner turns a change to it into a call to
    # those same endpoints, and otherwise mirrors this column back down. That is why
    # they cannot drift — there is only ever one value, and this is it.
    #
    # The runner reports its local file as an EDGE (on change), never as a level on
    # every heartbeat. A level would re-assert "not paused" five seconds after any
    # remote pause and silently lift it.
    #
    # A pause is NOT a liveness state and NOT a decommission. `retire` was the only
    # reachable lever and is the wrong one: it deletes RunnerAssignment rows that
    # `unretire` explicitly does not restore, and it 404s the daemon's own
    # heartbeat/claim calls (it cost jj-mbp-cdp ten sessions on 2026-07-25). This is
    # reversible by construction and destroys nothing.
    #
    # Deliberately NOT touched by `heartbeat()`: the runner reports liveness, the
    # operator decides work. A box that keeps heartbeating stays paused until a
    # human unpauses it, which is the entire point.
    paused = models.BooleanField(
        default=False,
        help_text="Operator pause: the runner stays live but claims nothing.",
    )
    paused_note = models.CharField(max_length=200, blank=True, default="")
    paused_at = models.DateTimeField(null=True, blank=True)
    # WHO paused it: the signed-in caller of /pause — a person from the UI or
    # MCP, or the box's owner when the runner turns its local sentinel into that
    # same call. Named in what a turn the pause strands is told
    # (`claim.Reach.why`), because "paused" alone sends the reader hunting.
    paused_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # A scheduled UNPAUSE — a one-off job, not a kind of pause. The pause above is
    # the one state; this only says when the server will lift it. A runner sets it
    # on ITSELF when its Claude subscription hits a usage cap: the CLI names the
    # reset ("resets 2:30am (America/Denver)"), so the box is parked until then and
    # routing sends its work to a runner that can still spend tokens.
    #
    # Run by `services.wake_due_runners` on the server's own clock (every
    # heartbeat, from ANY runner), never by a runner and never as an agent turn:
    # a paused laptop fires no schedules, and for someone with one laptop the
    # only box that could fire it is the parked one. Null = no unpause scheduled;
    # /unpause and /pause-without-`until` both cancel it.
    unpause_at = models.DateTimeField(null=True, blank=True)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    # When this runner last posted a WHOLESALE open-session report. OBSERVED, never
    # typed — the same discipline `capabilities["projects"]` follows, and for the same
    # reason: it is a fact about what a box does, and a hand-declared version of it
    # would drift toward the answer that breaks things silently.
    #
    # It exists because session staleness needs an OBSERVER. `RunnerBinding.live_seen_at`
    # is only a liveness clock if somebody is winding it, and exactly one of the two
    # runner programs does: the laptop re-reports its whole open-emdash-task set every
    # heartbeat (empty set included), while the CLOUD runner posts no wholesale report
    # at all — it only `record-session`s once per turn, so `live_seen_at` there is a
    # creation stamp. Reading absence as retirement on a box that never reports would
    # have archived every live cloud chat 3 minutes after its last turn.
    #
    # NULL means "never reported", which is why the read is `isnull` and not a window:
    # a box that HAS reported and then died must still retire its sessions (that is the
    # long-standing behavior, and the point of polled liveness), so this gates whether
    # the signal exists at all — not whether it is fresh.
    sessions_reported_at = models.DateTimeField(null=True, blank=True)
    # macOS user @ hostname that owns this runner. Load-bearing for session reuse:
    # emdash sessions are per-macOS-account (separate emdash4.db, worktrees, transcripts),
    # so a live session is reusable ONLY by the runner whose host matches the one that
    # created it. Jonathan runs the fleet under two accounts (token-limit failover).
    host = models.CharField(max_length=200, blank=True, default="")
    # The git branch of the runner's OWN code checkout, self-reported on the heartbeat.
    # Empty when unknown (cloud/non-git). Load-bearing for a supervisor ALERT: a runner
    # on anything but `main` is silently executing stale/wrong code — it happens when
    # another process (e.g. a DDD run) checks out a branch in the runner's shared
    # checkout. The UI shouts when this is non-empty and != "main".
    code_branch = models.CharField(max_length=200, blank=True, default="")
    # The runner package's own version string, self-reported. Legible only — the
    # staleness comparison uses `code_sha` (a version number depends on a human
    # remembering to bump it, and is therefore decorative on the day they forget,
    # which is the day it matters).
    code_version = models.CharField(max_length=64, blank=True, default="")
    # The sha of the last commit that touched the runner's OWN source — NOT the
    # repo HEAD, which moves on every canopy-web commit and would mark every runner
    # stale on a CSS change. "Its own source" is per KIND, because the fleet runs
    # two programs: `runner/canopy_runner/canopy_runner/` for a laptop,
    # `runner/ec2/` for a cloud box. The server holds the matching pair
    # (`settings.RUNNER_CODE_SHA` / `RUNNER_CLOUD_CODE_SHA`, computed at
    # image-build time and served per row by RunnerOut), and the supervisor alerts
    # when both are non-empty and differ.
    #
    # Empty means UNKNOWN and must stay silent: a source checkout with no git, a
    # shallow clone, an unstamped install. A staleness alert fired on partial
    # information is worse than no alert — and this repo has paid for
    # NULL/empty-means-something predicates before (see the six tenancy predicates
    # that grew a "means allow" leg).
    code_sha = models.CharField(max_length=64, blank=True, default="")
    # Committer epoch of that same commit — the ORDER a sha cannot carry.
    #
    # `code_sha != expected` means DIFFERENT, and the supervisor was reporting it
    # as "behind": one of three possibilities (older, newer, divergent) stated as
    # if it were the only one. Observed 2026-07-29 — a laptop installed from
    # origin/main between a runner change landing and the deploy that ships it
    # was flagged "update this box" while being the most current in the fleet.
    # Telling someone to fix the newest box is how a real alert gets ignored.
    #
    # A timestamp, not a counter, because it needs no human to maintain it and no
    # release ceremony: `git log -1 --format=%ct` on the same path that already
    # yields %H. 0 means UNKNOWN and orders nothing — the alert then degrades to
    # today's direction-less "different" rather than going quiet, since a runner
    # too old to report this is the one most likely to be genuinely behind.
    code_committed_at = models.BigIntegerField(default=0)
    # What the box REPORTS about its own features, one named check each —
    # `{"checks": [{"name", "status": ok|warn|fail, "detail"}], "checked_at",
    # "bootstrapped_at", "received_at"}`. OBSERVED, never typed, like `projects`.
    #
    # Separate from `ready` on purpose: `ready` answers "can this box execute a
    # turn", this answers "which of its features actually work". Those came
    # apart on 2026-09-22 — cloud-ec2-1 read online + ready + code-current for
    # six hours while it wrote no durable chat rows, read no mail and could not
    # steer a turn, because a failed `git pull` skipped loading three packages
    # and the only record was a journald line. Empty = the runner does not
    # report health (the laptops, an older box): UNKNOWN, never "healthy".
    health = models.JSONField(default=dict, blank=True)
    # The mailboxes this box can actually READ, lowercased — what its own probe
    # found (a gog token for the address that answered a one-message search),
    # REPORTED on the heartbeat. OBSERVED, never typed, like `projects`: it
    # replaced a hand-kept `mailboxes` map in runner.json that the owner did not
    # know existed, and a runner with `{}` there was still rung by the doorbell
    # for every mail and silently ignored each ring (canopy-web#1087).
    #
    # NULL means NEVER REPORTED (an older runner) and is read as "unknown", never
    # as "reads nothing": the doorbell keeps ringing such a box, exactly as it did
    # before this column existed. [] is a real answer — "I hold no working token".
    mailboxes_readable = models.JSONField(null=True, blank=True, default=None)
    # Server clock, stamped when a report arrives — tells a current answer from
    # an old one, since a box that stops reporting keeps its last list.
    mailboxes_checked_at = models.DateTimeField(null=True, blank=True)
    # The slash commands a Claude session on this box can run (its account's
    # skills and commands, its enabled plugins', and what ACP announced), as
    # [{name, description, args_hint, source}] — REPORTED by the box when the list
    # changes (`canopy_runner.slash_commands`), never typed. Feeds the chat
    # composer's `/` autocomplete; kept off RunnerOut, since it runs to hundreds
    # of rows. NULL = never reported (an older runner).
    slash_commands = models.JSONField(null=True, blank=True, default=None)
    # An operator asked this box to refresh itself (re-run bootstrap: plugins,
    # the canopy CLI, Claude Code, agent provisioning). DURABLE, not a frame: a
    # control frame is a doorbell, and one sent while the socket is down is
    # simply lost (spec 2026-08-01). The runner reads `refresh_pending` off its
    # heartbeat reply and restarts itself once idle; the request is discharged by
    # the box reporting a bootstrap newer than it (`health.bootstrapped_at`), so
    # nothing has to clear it and a refresh that never happened stays visible.
    refresh_requested_at = models.DateTimeField(null=True, blank=True)
    # The usage report (`apps/events/usage.py`): when the box last posted one, when
    # a person asked it for a FULL rescan (Health → Runners → Check for missing
    # sessions; a durable ask the box reads off `usage-scope`, the doorbell rule),
    # and when it last finished one.
    usage_reported_at = models.DateTimeField(null=True, blank=True)
    usage_scan_requested_at = models.DateTimeField(null=True, blank=True)
    usage_scanned_at = models.DateTimeField(null=True, blank=True)
    # This runner's owner. Load-bearing for authz AND for tenancy:
    # `_runner_visibility_q` requires owner to be the caller (or NULL, the
    # legacy-ungated path it keeps open on purpose), and BOTH `_runner_schedule_qs`
    # and `claim_next_turn` derive the tenant from it — a runner may sync/fire/claim
    # in a workspace where its owner may run work AND that it is enrolled in
    # (`RunnerEnrollment`, 2026-10-11; `routing.runner_tenant_slugs`).
    #
    # OPERATIONAL CONSEQUENCE — deleting a pairing user permanently bricks their
    # runners. SET_NULL orphans the row rather than removing it; `owner_id`
    # becomes NULL, `_runner_schedule_qs` returns none() for a NULL owner, and
    # `claim_next_turn` resolves an empty workspace set — so the orphan can never
    # sync, fire, or claim anything tenanted again. It must be re-paired (a fresh
    # row) and the orphan retired.
    #
    # This fail-closed behaviour is CORRECT and must stay: a runner whose owner no
    # longer exists has no tenant to derive, and inferring one would be a privilege
    # escalation. Deactivate a departing user (`is_active=False`) rather than
    # deleting them if their runners should stay operable for a successor.
    #
    # `workspace` (below) is where the runner LIVES — it is NOT the claim
    # boundary, and must not be made one. Scoping claims to that single FK is
    # exactly the outage this fleet hit: one laptop runner serves an agent fleet
    # that deliberately spans workspaces, so FK-scoping left 4 of 5 agents unable
    # to execute any turn. The FK gates runner VISIBILITY (`_runner_visibility_q`,
    # `list_runners`); `owner` gates what a runner may WORK FOR.
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="runners",
        help_text="The tenant that owns this runner. Nullable for migration "
        "safety; the API assigns one at pairing (the owner's default workspace "
        "when unspecified). Mirrors Agent.workspace.",
    )
    owner = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    paired_at = models.DateTimeField(auto_now_add=True)
    meta = models.JSONField(default=dict, blank=True)
    # The MACHINE this runner is on: the canonical IAM role ARN its box proved
    # (`services.set_runner_machine`, from an approved aws-iam device key and from
    # pairing by role). Several runners — one per isolation unit — share a box,
    # and so share this. "" = not known (a laptop, or a box that never proved a
    # role). Read by `routing.unit_refusal` and the topology's grouping; never a
    # grant on its own.
    machine_id = models.CharField(max_length=300, blank=True, default="", db_index=True)
    #: A PRIVATE runner is its owner's own machine (canopy-web#1705): it takes no fleet
    #: work — nothing routed, no workspace order, no source rule — only what its owner
    #: puts on it (a pin, a session bound to it) and the local sessions opened on it
    #: (`apps/harness/local.py`). Set by its owner, never reported by the box.
    private = models.BooleanField(default=False)
    #: The agents this box is DEDICATED to: when any are listed it runs only their
    #: turns and chats — no other agent's, no repo turn, no agentless chat — however
    #: they reach it (an assignment, an inherited order, a pin). Empty = no
    #: restriction. Set by a person on the runner page, never reported by the box,
    #: and it only ever narrows what the box takes — the person-set counterpart of a
    #: box's own agent unit (`unit()`); both apply when both are set
    #: (`routing.dedicated_agent_slugs`).
    only_agents = models.ManyToManyField("agents.Agent", blank=True, related_name="dedicated_runners")
    #: Directed only: the box takes only what is PUT on it (`directed_only`). A
    #: runner administrator's setting (the runner page, `PUT /runners/{id}/directed`),
    #: or the invitation's for a box paired as a test box (`up.sh --test`) — never
    #: what the box says about itself.
    directed = models.BooleanField(default=False)
    #: The egress lock a person set for this runner (`PUT …/egress`, `apps/harness/egress.py`):
    #: locked, and the hosts it admits beyond the box's base set. Root on the box reads it
    #: by its instance role (`POST /api/harness/runners/box-egress`) and applies it. It
    #: only ever adds to the box's own floor (box.env), and a ZDR box is locked
    #: whatever it says. Never reported by the box.
    egress_locked = models.BooleanField(default=False)
    egress_allow = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [models.Index(fields=["kind", "status"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"runner:{self.name}:{self.kind}:{self.status}"

    def save(self, *args, **kwargs):
        """A NEW runner is enrolled in the workspace it was paired into, and in
        nothing else (`RunnerEnrollment`). Here rather than in the pairing route
        so every way a row comes into being gets the same single enrollment."""
        creating = self._state.adding
        super().save(*args, **kwargs)
        if creating and self.workspace_id:
            RunnerEnrollment.objects.get_or_create(
                runner=self, workspace_id=self.workspace_id,
                defaults={"enrolled_by_id": self.owner_id})

    def agent_slugs(self) -> list[str]:
        return list(self.capabilities.get("agents", []))

    #: Where `only_agent_slugs` keeps its answer; `routing.prime_dedicated_agents`
    #: fills it for a batch of runners in one query.
    ONLY_AGENTS_CACHE = "_only_agent_slugs"

    def only_agent_slugs(self) -> set[str]:
        """The slugs of `only_agents` (empty: no restriction) — from the cache when
        primed, else one query, cached on this instance."""
        cached = getattr(self, self.ONLY_AGENTS_CACHE, None)
        if cached is None:
            cached = set(self.only_agents.values_list("slug", flat=True)) if self.pk else set()
            setattr(self, self.ONLY_AGENTS_CACHE, cached)
        return cached

    def unit(self) -> dict:
        """The isolation unit this runner says it is (`capabilities.unit`):
        `{"kind": "tree", "root": <slug>}` or `{"kind": "agent", "slug": <slug>}`;
        {} for a runner that declares none. Box-declared, and only ever a
        restriction on what the box takes (`routing.unit_refusal`)."""
        unit = (self.capabilities or {}).get("unit")
        return unit if isinstance(unit, dict) else {}

    def observed_projects(self) -> list[str]:
        """Every repo this box reports it holds (emdash's projects table, a cloud
        box's configured list), whether or not its owner uses canopy there. Only
        an AGENT's repo is checked against this: an agent is canopy by definition.
        Empties are stripped: a session turn has project="", so a stray "" here
        would let a non-session runner match it via `project__in`."""
        return [p for p in (self.capabilities or {}).get("projects", []) if p]

    #: Where `project_names` caches the owner's opt-in (`ProjectOptIn`) on this
    #: instance: routing asks the same runner many times per claim.
    OPT_IN_CACHE = "_project_opt_in"

    def project_names(self) -> list[str]:
        """The repos this runner may drive for canopy: what it holds AND its owner
        opted into (`ProjectOptIn`, docs/architecture/runners.md § Opted-in
        projects). A repo outside the opt-in is not canopy's — no repo turn routes
        to it, no session or transcript is mirrored from it, no usage is read from
        it. A runner with no owner opts into nothing (fails closed). Like `agents`
        this is a routing fact, not a security boundary — the workspace is the
        gate, and the two intersect."""
        return [p for p in self.observed_projects() if self.opts_in(p)]

    def opts_in(self, project: str) -> bool:
        """Whether this runner's owner uses canopy in `project` (`ProjectOptIn`),
        held or not — the question for a session the box reports."""
        opt_in = getattr(self, self.OPT_IN_CACHE, None)
        if opt_in is None:
            opt_in = ProjectOptIn.for_user_id(self.owner_id)
            setattr(self, self.OPT_IN_CACHE, opt_in)
        return opt_in.covers(project)

    def refresh_pending(self) -> bool:
        """Asked to refresh, and has not bootstrapped since."""
        if self.refresh_requested_at is None:
            return False
        try:
            booted = float((self.health or {}).get("bootstrapped_at") or 0)
        except (TypeError, ValueError):
            booted = 0.0
        return booted < self.refresh_requested_at.timestamp()

    def expected_code_sha(self) -> str:
        """The sha the DEPLOYED server expects this runner's kind to be running.
        Per KIND, because the fleet runs two programs (see `code_sha` above); one
        derivation shared by RunnerOut and the heartbeat's update nudge so the
        alert and the nudge cannot disagree. Empty means UNKNOWN, never "stale"."""
        setting = "RUNNER_CLOUD_CODE_SHA" if self.kind == Runner.CLOUD else "RUNNER_CODE_SHA"
        return getattr(settings, setting, "") or ""

    def expected_code_committed_at(self) -> int:
        """Committer epoch of `expected_code_sha`, per kind. 0 = unknown (a
        malformed build arg too: the alert then falls back to "different")."""
        setting = "RUNNER_CLOUD_CODE_COMMITTED_AT" if self.kind == Runner.CLOUD else "RUNNER_CODE_COMMITTED_AT"
        try:
            return int(getattr(settings, setting, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def session_capable(self) -> bool:
        """Whether this runner executes chat-session turns (the interactive
        front-door — SP2b). Opt-in via `capabilities.sessions: true`. Cloud runners
        and laptop emdash runners both set it (`canopy-runner pair` declares it since
        2026-10-02); a box without it never receives a chat/Slack turn. Like the
        other capabilities, a hint gated by tenant."""
        return self.kind != Runner.COMPUTER and bool(self.capabilities.get("sessions", False))

    def directed_only(self) -> bool:
        """Whether this box takes only what is PUT on it: turns pinned to it and
        turns of sessions bound to it — no routed agent turn (even with an
        enabled assignment row), no repo turn, no unbound chat, no inherited
        workspace order. A private runner always; otherwise `directed`, a person's
        setting. It stays session-capable, so a chat can still be started on it,
        and that chat binds to it."""
        return self.private or self.directed

    @property
    def live_status(self) -> str:
        """What we can OBSERVE, not what the runner last claimed.

        `heartbeat()` writes ONLINE and nothing demotes it — a runner that dies
        never gets to tell us. So liveness is derived from heartbeat age here,
        and RunnerOut serves this rather than the raw column. `degraded` is
        different: the runner self-reports it, so it survives as long as the
        runner is still fresh enough to be reporting anything at all.

        PAUSED is served from here rather than as a separate flag consumers must
        remember to check, and that placement IS the feature. Every existing
        question of the form "is this box online?" — `claim_next_turn`'s first
        guard, `is_available`, the cascade's `reachable` split, the agents API's
        `online` column, the realtime snapshot — runs through `live_status`. Serving
        it here makes all of them honor the pause with no edit and, more to the
        point, leaves no way to write a NEW caller that misses it. The alternative
        (a `paused` bool beside an otherwise-`online` row) is precisely the shape
        that made this bug expensive in the first place: the runner's local sentinel
        keeps heartbeating `online` + `ready`, so every status-based check waved a
        parked box straight through.

        It ranks BELOW the liveness checks deliberately. A paused box whose
        heartbeat has lapsed is `stale` — "asleep" is the more useful and more
        honest answer than "parked", and the pause is still there when it wakes.
        """
        if self.status == self.RETIRED:
            return self.RETIRED
        if self.last_heartbeat_at is None:
            return self.DISCONNECTED
        if timezone.now() - self.last_heartbeat_at > HEARTBEAT_ONLINE_WINDOW:
            return self.STALE
        if self.paused:
            return self.PAUSED
        return self.status

    @property
    def is_available(self) -> bool:
        """Can this runner take work RIGHT NOW — online (fresh heartbeat) AND
        self-reported ready. The cascade's availability probe (spec 2026-07-24)."""
        return self.live_status == Runner.ONLINE and self.ready

    @property
    def signed_out(self) -> bool:
        """The box says its Claude login is gone (`canopy_transcript.SIGNED_OUT_NOTE`,
        set from the CLI's own "Not logged in" record). Unlike other not-ready
        notes this one means every turn it took would come back as that one line,
        so it claims nothing (`claim_next_turn`) and a turn waiting on it says why."""
        from canopy_transcript import SIGNED_OUT_NOTE

        return not self.ready and self.ready_note.startswith(SIGNED_OUT_NOTE)

    @property
    def remedy(self) -> str:
        """What a person does about this box being degraded or not ready, when
        its own note names a known cause; "" otherwise (the note is shown either
        way). Health, Topology, the supervisor and the menu bar all read this."""
        note = f"{self.status_note} {self.ready_note}"
        if self.signed_out:
            return ("Claude Code on that machine has no sign-in. Run `claude` there and "
                    "`/login`; the runner takes turns again once Claude answers.")
        if "CDP unreachable" in note:
            return ('Emdash is running without its debug port. On that machine, open '
                    '"Emdash CDP" (Spotlight), which reopens Emdash with it.')
        return ""

    @property
    def is_reachable(self) -> bool:
        """Is the daemon still THERE — a weaker question than `is_available`.

        ONLINE, DEGRADED and PAUSED are exactly the states `live_status` can only
        serve on a fresh heartbeat: degraded stops claiming (CDP down), paused
        stops starting anything new, but both daemons keep their poll loop and
        control channel up (a parked box that dies reads STALE, not PAUSED). This
        is the gate for work that FINISHES or reads what already exists — pressing
        a menu answer into a blocked session, shipping a transcript — never for
        starting new work, which is `is_available`'s question. Excluding PAUSED
        here made a parked runner refuse the very menu its own session report had
        just delivered (2026-07-31, ada blocked on a box parked for a rate limit).
        """
        return self.live_status in (self.ONLINE, self.DEGRADED, self.PAUSED)

    @property
    def flags(self) -> frozenset[str]:
        """What this box's owner has declared (`RunnerFlag`). Reads the prefetch
        cache when the caller used prefetch_related("declared_flags")."""
        return frozenset(f.flag for f in self.declared_flags.all())


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
    # A person's own Claude Code session on their private runner (`apps/harness/local.py`).
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
    ROUTING_CHOICES = [(PREFER_LOCAL, "Prefer local"), (LOCAL_ONLY, "Local only"), (ANY, "Any")]

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
    # This is a data-model gap, not a capability one: cdp_control.create_task is
    # already project-generic and has no concept of an agent.
    #
    # A pseudo-agent per repo was rejected: it would serialize repo work behind
    # one_executing_turn_per_agent (emdash gives every task its own worktree, so
    # repo work is *meant* to parallelize) and give every repo KPIs, a needs-you
    # inbox, and a skills catalog it has no meaning for.
    project = models.CharField(max_length=100, blank=True, default="")
    # The chat case: a session turn (the interactive front-door). Its tenancy
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
    # else names a capability from its declared interface, and the runner and
    # the agent's guard confine the session to it. Decided once, at enqueue,
    # from the initiator — see `apps/agents/interface.py::capability_for`.
    capability = models.CharField(max_length=48, blank=True, default="")
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
    # The two FKs are only set when the id resolved; an unknown id is kept raw in
    # `provenance["parent"]` instead and never refuses the request.
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
    report_title = models.CharField(max_length=300, blank=True, default="")
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
            # Serialize EXECUTION, not intake: queued turns stack freely.
            #
            # Stays AGENT-ONLY on purpose. An agent is one identity with one
            # continuous session, so serializing it is correct; a repo is not —
            # emdash gives every task its own worktree. Widening this to projects
            # would funnel all canopy-web work into a single lane.
            #
            # Project turns have agent=NULL and NULLs never compare equal, so they
            # do not participate here at all. That is load-bearing, not incidental,
            # so it has its own test.
            #
            # A LOCAL turn is excluded: it is a person's own session on their own
            # machine, not the agent's one continuous session, so it neither waits
            # behind the agent's runner turns nor holds them up — and two people
            # may use the agent locally at once (`routing.busy_agent_ids` agrees).
            models.UniqueConstraint(
                fields=["agent"],
                condition=(models.Q(status__in=["claimed", "running", "needs_human"])
                           & ~models.Q(origin="local")),
                name="one_executing_turn_per_agent",
            ),
            # A session serializes its own execution — one running turn per
            # conversation. Session turns have agent=NULL so they do not
            # participate in one_executing_turn_per_agent; NULLs never compare
            # equal, so agent/project turns do not participate here. A RIDER
            # (`rides_turn`) is exempt: it executes inside the running turn's
            # session, not as a second one.
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
    capability decides which tools exist.

    Bound to the conversation when there is one, because a long confined session
    outlives any single turn and its MCP connection is not re-made per turn.
    Only the hash is stored.
    """

    token_hash = models.CharField(max_length=64, unique=True)
    chat_session = models.ForeignKey("canopy_sessions.Session", on_delete=models.CASCADE,
                                     null=True, blank=True, related_name="caller_tokens")
    turn = models.ForeignKey("Turn", on_delete=models.CASCADE, related_name="caller_tokens")
    #: Minted for a FULL-profile turn asked by someone who is not the agent's
    #: owner, an admin or canopy (a `full:` rule, an editor): the session is not
    #: confined to a capability, but its canopy reads must still be the ASKER's
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


def _default_notify() -> list:
    """Callable default — a mutable literal would be shared across rows."""
    return ["inbox"]


class AgentSchedule(models.Model):
    """A recurring turn declaration — "Echo's weekly manager report, Fridays 9am ET".

    Config lives here (server-side, so it is visible and editable in the Agent
    UI); canopy-web fires it itself (schedule_clock.py, a loop in the web
    process), and a laptop runner may too (it syncs these rows, evaluates the
    cron locally, and POSTs back a slot) — both collapse on the slot key. Either
    way the server materializes a normal harness Turn via services.enqueue_turn — the scheduler is a producer
    of turns, not a second execution engine.

    The Turn IS the occurrence (origin=cron, origin_ref={schedule_id, slot},
    idempotency_key="sched:<id>:<slot>"); there is deliberately no occurrence
    table. See docs/superpowers/specs/2026-07-15-agent-scheduled-turns-design.md.

    An unattended (grace-released) occurrence raises a real Item (kind=review)
    whose `implement` re-runs the schedule's prompt — see
    services._raise_schedule_nag. There is no projection: the nag is a row.

    No workspace FK: a schedule is agent-owned and derives its tenant via
    agent.workspace, exactly as Turn does.
    """

    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="schedules")
    # WHO set this up / last changed it. Distinct from `agent` (which agent it fires)
    # and from tenancy (agent.workspace): a schedule is a person's standing request,
    # so the calendar can show "my schedules" and a MISSED occurrence can notify the
    # human who created it. Nullable — SET_NULL so a departed user doesn't cascade the
    # schedule away, and null for pre-attribution rows.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="schedules_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="schedules_updated",
    )
    name = models.CharField(max_length=200, help_text='e.g. "Weekly manager report"')
    prompt = models.TextField(help_text="What the turn is seeded with, e.g. /echo:manager-report")
    cron = models.CharField(max_length=120, help_text="5-field cron expression, e.g. '0 9 * * 5'")
    # A ONE-OFF: set, and the schedule fires exactly once, at this instant, then
    # disables itself. `cron` is still stored — derived from this in `timezone`
    # (schedule_services.one_off_cron) — so a runner that predates the field keeps
    # firing it correctly; `fire_after` is pinned just before this instant so the
    # derived cron's yearly repeats can never come due. Null = recurring.
    run_once_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Fire once at this instant, then disable. Null for a recurring schedule.",
    )
    timezone = models.CharField(max_length=64, default="UTC", help_text="IANA tz, e.g. America/New_York")
    enabled = models.BooleanField(default=True, help_text="Pause without deleting.")
    routing = models.CharField(max_length=15, choices=Turn.ROUTING_CHOICES, default=Turn.PREFER_LOCAL)
    grace_minutes = models.PositiveIntegerField(
        default=120,
        help_text="How long an unattended fired turn may hold the agent before it is "
        "released as MISSED. Guards one_executing_turn_per_agent: an abandoned "
        "session would otherwise wedge the agent indefinitely.",
    )
    always_run = models.BooleanField(
        default=False,
        help_text="Run a slot however late it is claimed. Off: a slot not claimed "
        "within LATE_SLOT_WINDOW_MINUTES of its time is skipped as MISSED, so a "
        "laptop reopened after a day away does not fire every schedule at once.",
    )
    notify = models.JSONField(
        default=_default_notify, blank=True,
        help_text='Channel ids resolved through the notify registry, e.g. ["inbox"].',
    )
    last_slot = models.DateTimeField(
        null=True, blank=True,
        help_text="Newest slot fired. The supersede + no-backfill anchor.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["agent", "enabled"])]
        constraints = [
            models.UniqueConstraint(fields=["agent", "name"], name="uniq_agent_schedule_name"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"sched:{self.agent.slug}:{self.name}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class RunnerCredential(models.Model):
    """Per-runner credentials the runner fetches at startup — CLOUD runners only.

    Laptop/emdash runners use their ambient auth (emdash already holds the Claude
    login) and never read this. A cloud runner boots knowing only its canopy-pat,
    then fetches this bundle over HTTPS authed by that PAT (owner == owner, the
    same gate as heartbeat/claim) and stages it into its environment: the runner's
    Claude login, and nothing else.

    NO GITHUB TOKEN. There was one — a shared fine-grained PAT, staged box-wide
    (`GH_TOKEN` in the runner's environment plus a global `~/.git-credentials`),
    so every agent on the box pushed as whoever made it, with whatever it could
    do; it could push branches and could not open pull requests (#747). GitHub is
    now the agent OWNER's identity lent to that one agent (`AgentDelegation`),
    handed to one turn at a time.

    NO 1PASSWORD KEY. There was one — a box-wide service-account token — and it
    was staged into the runner's own environment, which every turn inherits: one
    credential that reads EVERY agent's vault, handed to each of them. The key
    that opens an agent's vault belongs to that agent (`Agent.op_sa_token_enc`)
    and the tenant's to the workspace (`Workspace.shared_op_sa_token_enc`); the
    box is handed the right one per agent, at bootstrap, and holds none itself.

    The credentials themselves are `RunnerLogin` rows, one ordered list. This row
    is the bundle's stamp: `updated_at` moves on every change to that list, and a
    box that sees it move proves its credentials again.
    """

    runner = models.OneToOneField(Runner, on_delete=models.CASCADE, related_name="credential")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self) -> str:
        return f"credential:{self.runner_id}"


class RunnerLogin(models.Model):
    """One Claude credential of a cloud runner, in its place in ONE ordered list.

    Claude auth is an ordered cascade, not one credential: a subscription has a
    usage cap, and when it trips every agent on the box stops (2026-08-01). The
    box uses the first credential that works and moves down the list when one is
    capped or stops working. Any number of each kind, in any order a person sets:

    - `login`: a full `claude auth login` held ON the box. Its refresh token
      rotates, so it never lives here; it sits in its own config directory on the
      box (`~/.claude-logins/<key>`; `default` is the CLI's own `~/.claude`).
      `account` and `signed_in` are what the BOX reports, so nobody types a name
      that can disagree with the login it holds.
    - `token`: a `claude setup-token` value, encrypted in `value_enc`.
    - `api_key`: an Anthropic API key, encrypted in `value_enc`.

    A token or API key cannot say whose it is, so a person names it (`name`).
    Adding a login row starts a sign-in; deleting any row makes the box drop it
    on its next re-read; the order of the rows is the cascade order.
    """

    DEFAULT = "default"
    LOGIN = "login"
    TOKEN = "token"
    API_KEY = "api_key"
    KIND_CHOICES = [(LOGIN, "login"), (TOKEN, "token"), (API_KEY, "API key")]

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="logins")
    #: Names the entry (and a login's directory on the box). Short, opaque, never reused.
    key = models.CharField(max_length=32)
    # db_default as well: code from before the list, still serving mid-deploy,
    # inserts login rows without these columns.
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=LOGIN, db_default=LOGIN)
    position = models.PositiveIntegerField(default=0)
    #: A token's or API key's name, as a person gave it. Not a secret.
    name = models.CharField(max_length=200, blank=True, default="", db_default="")
    #: A token's or API key's value, encrypted (apps/common/encryption.py). Empty for a login.
    value_enc = models.TextField(blank=True, default="", db_default="")
    account = models.CharField(max_length=254, blank=True, default="")
    signed_in = models.BooleanField(default=False)
    reported_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["runner", "key"], name="uniq_runner_login_key"),
        ]

    def __str__(self) -> str:
        return f"login:{self.runner_id}:{self.key}"


class RunnerMint(models.Model):
    """One browser-driven re-authentication of a runner's Claude credential.

    A cloud runner's subscription token expires often, and when it does the box
    does not slow down — it stops, and stays stopped until a person signs in
    again. Fixing that used to mean a terminal on the box.

    It cannot be done entirely in canopy-web: `claude setup-token` runs a PKCE
    flow whose verifier is generated by whoever starts it, so canopy-web cannot
    exchange a code a browser collected — it is not the OAuth client and must not
    pretend to be. So the RUNNER runs the real CLI, and this row is the relay
    between it and a human's browser: the runner posts the authorize URL, a
    person opens it and pastes back the code, the runner completes the exchange.

    POLLED, not pushed, and deliberately so — the same lesson as `menu-answers`:
    a control frame published while the runner's WS channel is down goes to a
    group with no consumer and is silently discarded, while the runner keeps
    heartbeating over REST and reads ONLINE the whole time.

    The minted token NEVER passes through the browser. The runner sends it here
    on completion and it is written straight into RunnerCredential, so the only
    secret a human handles is the single-use authorization code.
    """

    REQUESTED = "requested"          # a human asked; the runner has not started
    AWAITING_CODE = "awaiting_code"  # URL is up; waiting on the human
    COMPLETING = "completing"        # code submitted; the runner is exchanging
    DONE = "done"
    FAILED = "failed"
    STATUS_CHOICES = [
        (REQUESTED, "requested"), (AWAITING_CODE, "awaiting code"),
        (COMPLETING, "completing"), (DONE, "done"), (FAILED, "failed"),
    ]
    #: Terminal states — a mint in one of these is history, not work.
    FINISHED = (DONE, FAILED)

    #: Which subscription login the minted token replaces. Named by the human who
    #: started it, never inferred: a sign-in used to land in the primary
    #: unconditionally, so "add a fallback" silently overwrote the working login.
    PRIMARY = "primary"
    SECONDARY = "secondary"
    #: A long-lived token for the runner's credential list: a new one at the end,
    #: or (with `login_key`) a new value for that token.
    TOKEN = "token"
    #: Not a bundle slot: a FULL `claude auth login` the box keeps for itself.
    #: The only login that carries claude.ai connectors (a setup-token is
    #: inference-only), and its refresh token rotates, so no value ever comes
    #: back here — the runner reports `logged_in` and puts it first.
    BOX = "box"
    SLOT_CHOICES = [(PRIMARY, "primary"), (SECONDARY, "secondary"), (BOX, "box"), (TOKEN, "token")]

    # UUID pk like Runner/Turn: a mint id travels in URLs and logs, and a
    # sequential one would leak how many sign-ins the fleet has needed.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="mints")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=REQUESTED)
    slot = models.CharField(max_length=16, choices=SLOT_CHOICES, default=PRIMARY)
    #: For a `box` sign-in: which `RunnerLogin` it signs in. Blank is the
    #: box's default login (a mint from before there was a list). For a `token`
    #: sign-in: the token it replaces; blank adds a new one.
    login_key = models.CharField(max_length=32, blank=True, default="")
    authorize_url = models.TextField(blank=True, default="")
    # The single-use code a human pastes back. Cleared the moment the runner
    # takes it: it is spent on first use, and a code left lying in a row is a
    # credential nobody is accounting for.
    code = models.TextField(blank=True, default="")
    detail = models.TextField(blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["runner", "-created_at"])]

    def __str__(self) -> str:
        return f"mint:{self.runner_id}:{self.status}"


class SignInRequest(models.Model):
    """One device-code sign-in a box put in front of its owner (AWS SSO today).

    The push notification alone was not enough. Google accepting a push only
    means it will try to deliver it, and on 2026-10-19 three real requests were
    accepted and never shown on the owner's phone, with nothing anywhere else
    saying a box was waiting. So the request is also kept here, and the
    supervisor feed shows the owner every one still open.

    Nothing secret is added by keeping it. canopy-web already saw the URL when
    it built the push, and the code in it only signs the box in as whoever
    approves it, which is why only the owner is shown it, in the push or here.
    The row lives as long as the code: `url` is cleared the moment the box
    reports the outcome, and a row whose box never reported is treated as
    expired once `expires_at` passes. What stays is the audit line: who asked,
    on which box, and why.
    """

    PENDING = "pending"
    APPROVED = "approved"
    FAILED = "failed"
    EXPIRED = "expired"
    STATUS_CHOICES = [(PENDING, "pending"), (APPROVED, "approved"),
                      (FAILED, "failed"), (EXPIRED, "expired")]
    #: What the box may report. EXPIRED is also what an unreported row becomes.
    OUTCOMES = (APPROVED, FAILED, EXPIRED)

    # UUID pk: the id travels in URLs, and the box echoes it back with the outcome.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="sign_in_requests")
    #: The person it was put in front of: the runner's owner when it was asked.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    provider = models.CharField(max_length=16)
    url = models.TextField(blank=True, default="")
    label = models.CharField(max_length=60, blank=True, default="")
    requested_by = models.CharField(max_length=60, blank=True, default="")
    reason = models.CharField(max_length=200)
    #: Devices the push reached when it was asked.
    sent = models.IntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["owner", "status", "expires_at"])]

    def __str__(self) -> str:
        return f"sign-in:{self.runner_id}:{self.status}"


class RunnerAdmin(models.Model):
    """Someone who may ADMINISTER this runner without speaking FOR it.

    `owner` was doing two jobs. It is the credential the box authenticates
    with — `claim_next_turn` derives a tenant from it, so heartbeating, claiming
    and drilling must stay bound to it — and it was also the only answer to "who
    may fix this box". For a shared cloud runner those are different questions,
    and conflating them means one human is a single point of failure: on
    2026-09-08 a signed-out cloud box could not be re-authenticated by the very
    identity it runs as, because someone else had run the pairing command.

    Ownership could not simply be moved. The box authenticates with the owner's
    PAT, so re-pointing `owner` 404s its own heartbeat until that PAT is
    rotated — a change in AWS, not here.

    Neither existing tier fits either, and it is worth writing down why. Workspace
    OWNER is too narrow (the dimagi workspace has exactly one, which is the
    problem being fixed). Workspace MEMBER is far too wide: that workspace
    auto-joins every `dimagi.com` address, so it would hand the fleet's Claude
    credentials to 22 people who never asked for them — and the tenancy predicates
    deliberately keep credentials out of the member tier for exactly that reason.

    So the grant is EXPLICIT and per-runner. Authorization is one place where
    being boring is the feature: a row here says precisely who was trusted with
    this box, by whom, and when.
    """

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="admins")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="+")
    granted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # One grant per person per runner — re-granting is idempotent, not a
        # second row that a later revoke would half-remove.
        constraints = [
            models.UniqueConstraint(fields=["runner", "user"], name="uniq_runner_admin"),
        ]

    def __str__(self) -> str:
        return f"admin:{self.runner_id}:{self.user_id}"


class RunnerDeviceKey(models.Model):
    """A hardware-bound (or software) key a runner proves possession of
    (Canopy Vault, canopy-web#1538 Phase 1a).

    The box generates an EC P-256 key — in the Secure Enclave or a TPM where it
    can — and registers only the PUBLIC half. It starts `pending`; a person who
    owns or administers the runner approves it from a browser (behind a passkey
    step-up), and from then on `/credentials/resolve` for an agent with
    `require_device_binding` accepts a DPoP proof signed by it (RFC 9449).

    Identified by `jkt`, the RFC 7638 thumbprint, which is unique: re-posting the
    same public key is idempotent, and one key never belongs to two runners.

    `kind=aws-iam` (Phase 1b, canopy-web#1549) is an EC2 box's instance ROLE
    rather than a key: it has no JWK, stores `role_arn` (canonical, path-free)
    and its `jkt` is the sha256 of that ARN. It proves itself with a signed
    `sts:GetCallerIdentity` request (`apps/harness/aws_iam_proof.py`).
    """

    SECURE_ENCLAVE, TPM, SOFTWARE, AWS_IAM = "secure-enclave", "tpm", "software", "aws-iam"
    KIND_CHOICES = [(SECURE_ENCLAVE, "Secure Enclave"), (TPM, "TPM"), (SOFTWARE, "Software"),
                    (AWS_IAM, "AWS IAM role")]
    PENDING, ACTIVE, REVOKED = "pending", "active", "revoked"
    STATUS_CHOICES = [(PENDING, "Pending approval"), (ACTIVE, "Active"), (REVOKED, "Revoked")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="device_keys")
    #: The public JWK, reduced to its required members (kty, crv, x, y). Empty
    #: for `aws-iam`, which has no key.
    public_jwk = models.JSONField(default=dict, blank=True)
    #: `aws-iam` only: the IAM role, as `arn:aws:iam::<acct>:role/<name>`.
    role_arn = models.CharField(max_length=300, blank=True, default="")
    #: Zero-knowledge vault (canopy-web#1550): a SEPARATE P-256 key the box
    #: does ECDH with — Secure Enclave keys are per-purpose, so the signing key
    #: cannot also agree keys; for `aws-iam` it is a 0600 key file on the box.
    #: It is approved on its OWN (`ecdh_approved_at`): a new ECDH key on an
    #: active key waits for a person, but the signing key stays active, so the
    #: box keeps signing in and running every other agent meanwhile.
    ecdh_public_jwk = models.JSONField(default=dict, blank=True)
    #: RFC 7638 thumbprint of `ecdh_public_jwk` — bound into every device wrap.
    ecdh_jkt = models.CharField(max_length=64, blank=True, default="", db_index=True)
    #: When a person approved THIS ecdh key (None: not yet, or none). Vault keys
    #: are wrapped, and values sealed, only to an approved one (`ecdh_ready`).
    ecdh_approved_at = models.DateTimeField(null=True, blank=True)
    ecdh_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                         null=True, blank=True, related_name="+")
    #: Where the box SAYS the ECDH private key lives — "secure-enclave", "tpm",
    #: "aws-kms" or "software" (a 0600 file); "" = not said. Self-reported and
    #: shown to people only ("key held by AWS"); nothing trusts it.
    ecdh_store = models.CharField(max_length=16, blank=True, default="")
    jkt = models.CharField(max_length=64, unique=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    label = models.CharField(max_length=120, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"devicekey:{self.runner_id}:{self.jkt[:12]}:{self.status}"

    @property
    def ecdh_ready(self) -> bool:
        """May vault keys be wrapped (and values sealed) to this key's ECDH key?"""
        return self.status == self.ACTIVE and bool(self.ecdh_jkt) and self.ecdh_approved_at is not None

    @property
    def ecdh_waiting(self) -> bool:
        """Active, signing in, but its (new) ECDH key awaits a person's approval."""
        return self.status == self.ACTIVE and bool(self.ecdh_jkt) and self.ecdh_approved_at is None


class RunnerAccessToken(models.Model):
    """A runner's API credential, bought with its device key — never a person's PAT.

    A box proves itself with an APPROVED `RunnerDeviceKey` (a DPoP proof from a key
    it holds, or its EC2 instance role) at `POST /api/runner-sessions/`, and gets
    one of these back: an hour long, bound to that runner, and good only on the
    runner protocol (`runner_auth.reaches`). When it runs out the box proves itself
    again. So nothing long-lived sits on the box to leak, and nothing a person
    tidies up in Settings → Tokens can cut a runner off by accident — the way a
    revoked PAT silently swallowed cloud-ec2-2's turns (2026-10-17). Revoking the
    device key, retiring the runner or deactivating its owner kills every token
    at once: `lookup` re-checks all three on each use.

    It authenticates AS the runner's owner, because every runner gate is
    owner-scoped (`Runner.owner`, `runner_may_hold_agent`); what it may reach is
    narrowed by path, not by a second identity.
    """

    PREFIX = "crt_"
    TTL = dt.timedelta(hours=1)

    token_hash = models.CharField(max_length=64, unique=True)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="access_tokens")
    device_key = models.ForeignKey(RunnerDeviceKey, on_delete=models.CASCADE,
                                   related_name="access_tokens")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["expires_at"])]

    def __str__(self) -> str:
        return f"runnertoken:{self.runner_id}:{self.expires_at:%Y-%m-%dT%H:%M}"

    @classmethod
    def mint(cls, device_key: RunnerDeviceKey) -> tuple[str, RunnerAccessToken]:
        """A fresh token for the key's runner. The raw value is returned once."""
        import secrets

        raw = cls.PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(
            token_hash=hash_token(raw),
            runner_id=device_key.runner_id, device_key=device_key,
            expires_at=timezone.now() + cls.TTL)
        # Tokens past their hour authenticate nothing; drop them as new ones arrive
        # rather than letting a box's hourly refresh grow the table forever.
        cls.objects.filter(runner_id=device_key.runner_id,
                           expires_at__lt=timezone.now() - cls.TTL).delete()
        return raw, token

    @classmethod
    def lookup(cls, raw: str) -> RunnerAccessToken | None:
        """The live token for `raw`: unexpired, its device key still ACTIVE, its
        runner not retired, its owner an active person. Else None."""
        if not raw or not raw.startswith(cls.PREFIX):
            return None
        return (
            cls.objects.select_related("runner__owner", "device_key")
            .filter(token_hash=hash_token(raw),
                    expires_at__gt=timezone.now(),
                    device_key__status=RunnerDeviceKey.ACTIVE,
                    runner__owner__is_active=True,
                    runner__owner__system_account__isnull=True)
            .exclude(runner__status=Runner.RETIRED)
            .first()
        )


class RunnerFlag(models.Model):
    """One property a runner's OWNER vouches for (spec 2026-09-30-zdr-runners).

    canopy cannot verify any of these -- `zdr` means "this box uses only
    zero-data-retention keys for Claude", which is not observable from here -- so
    each row is an attestation and records WHO made it. Set only through the
    flags route by a runner admin; nothing a runner REPORTS (heartbeat,
    capabilities) can create one.
    """

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="declared_flags")
    flag = models.CharField(max_length=32)
    declared_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, related_name="+")
    declared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["runner", "flag"],
                                               name="one_flag_row_per_runner")]


class RunnerAssignment(models.Model):
    """One row of an agent's ordered runner list — THE routing authority for agent
    turns (spec 2026-07-24-directed-runner-routing). An agent with no rows is
    explicitly unroutable. Replaced routing-by-capabilities + kind preference."""

    agent = models.ForeignKey(
        "agents.Agent", on_delete=models.CASCADE, related_name="runner_assignments"
    )
    runner = models.ForeignKey(
        Runner, on_delete=models.CASCADE, related_name="agent_assignments"
    )
    rank = models.PositiveSmallIntegerField()  # 0 = first choice
    # A disabled row stays in the agent's list (rank preserved, visible greyed
    # in the UI) but must NEVER route: the claim path excludes it entirely, and
    # it neither claims nor counts as a better-ranked availability blocker for a
    # lower rank. Distinct from removal — the operator's "x" toggles this
    # instead of deleting the row, so re-enabling doesn't lose the rank.
    enabled = models.BooleanField(default=True)
    # THE SOURCE RULE (spec 2026-07-27). "" = this row belongs to the agent's
    # DEFAULT ordered list (the pre-existing behaviour). Non-empty = this row is
    # the single priority runner for that source, and `rank` is meaningless on it
    # (the uniqueness constraint below allows exactly one). The column is kept
    # rather than dropped so an ordered per-source list needs no second migration.
    #
    # Note `enabled` then means two things across the two row kinds: a runner
    # switched off in the default list is still live for any source that names
    # it, because the rule is its own row with its own toggle. Pinned by
    # tests/test_source_rules.py and stated in the Runners-tab UI.
    source = models.CharField(max_length=32, blank=True, default="")
    # THE ACTOR RULE (spec 2026-09-05). "" = this rule applies to any actor
    # (exactly what a source rule meant before actors existed). Non-empty = a
    # normalized, lowercased bare address, matched against `actors.actor_of(turn)`.
    #
    # Only meaningful alongside a non-empty `source`: a default-list row routes
    # every actor by definition, and the constraints below keep `actor` out of
    # its key. 254 is the RFC 5321 maximum address length.
    actor = models.CharField(max_length=254, blank=True, default="")
    # Only meaningful on a source row. False: the priority runner goes first and
    # the default list follows beneath it. True: that runner or nothing — the turn
    # waits rather than degrading, which is the point for a source whose work can
    # only happen on one box (mailbox credentials, local files).
    strict = models.BooleanField(default=False)
    # THE MODE RULE (spec 2026-09-23). Only meaningful on a source row, and
    # rule-level like `strict` (written to every row of the rule). "" = the rule
    # says nothing about mode, so the next rung decides: an actor rule defers to
    # the source rule, which defers to `Agent.turn_mode`. "manual" or "auto"
    # overrides. Resolved at claim by `apps/harness/turn_mode.py`, which withholds
    # an `auto` that names a person unless THIS message is verified — routing on a
    # forged From: only picks a box; auto on one would let a forger act unreviewed.
    turn_mode = models.CharField(max_length=8, blank=True, default="")

    class Meta:
        ordering = ["agent_id", "rank"]
        constraints = [
            # One DEFAULT row per runner (what one_assignment_per_agent_runner
            # meant before source rules existed) …
            models.UniqueConstraint(
                fields=["agent", "runner"],
                condition=models.Q(source=""),
                name="one_default_assignment_per_agent_runner",
            ),
            # … and, within one RULE — the rows sharing (agent, source, actor),
            # rank-ordered — each runner at most once. This REPLACED a constraint
            # that capped a rule at a single runner, which could not express
            # "either of my two boxes, never cloud" for an operator whose live
            # account rotates (spec 2026-09-05).
            models.UniqueConstraint(
                fields=["agent", "source", "actor", "runner"],
                condition=~models.Q(source=""),
                name="one_row_per_runner_per_agent_source_actor",
            ),
        ]


class WorkspaceRunnerOrder(models.Model):
    """One row of a workspace's ordered runner list — the routing order for REPO
    turns (a project dispatch: agent NULL, chat_session NULL) in that workspace.

    A repo turn has no agent, so `RunnerAssignment` never ranked it: every online
    runner that declared the repo raced for it, and the first poll won (observed
    2026-10-02: a canopy-web dispatch landed on a second macOS account 126 ms after
    enqueue while the owner's own runner, first in every agent's order, sat online).

    Same semantics as an agent's default list: index = rank, the availability
    cascade with the same grace, and a disabled row is kept but never routes. A
    workspace with NO enabled rows routes repo turns exactly as before (any runner
    that declares the repo), so this is opt-in per workspace. Ranks are taken only
    among the listed runners that DECLARE the turn's repo — a better-ranked runner
    that does not serve the repo can never take it, so it must not block one that
    can. A per-project order can layer above this later.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="runner_order"
    )
    runner = models.ForeignKey(
        Runner, on_delete=models.CASCADE, related_name="workspace_order_rows"
    )
    rank = models.PositiveSmallIntegerField()  # 0 = first choice
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["workspace_id", "rank"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "runner"],
                                    name="one_order_row_per_workspace_runner"),
        ]


class RunnerEnrollment(models.Model):
    """A runner OPTS IN to serving a workspace — the box half of the tenant rule.

    A runner serves a workspace only when BOTH hold (`routing.runner_tenant_slugs`):
    it is enrolled there or in a workspace ABOVE it (a row here — enrollment
    covers the subtree, `routing.covered_slugs`), AND its owner may still run work there
    (`agent.work`, editor and above). Enrollment never grants more than membership
    does; it only narrows it. Before this table the membership half was the whole
    rule, so a box served every workspace its owner could work in — the day
    Jonathan became owner of a family workspace (`marshellis`, 2026-10-11), his
    Dimagi laptop and the cloud box would have started claiming its repo turns
    and unbound chats, and a family box would have claimed Dimagi's.

    Who adds rows: pairing enrolls the box in the workspace it paired into
    (`Runner.workspace`) and nothing else; the owner or a runner admin adds and
    removes the rest (`/runners/{id}/workspaces`). Joining a new workspace does
    NOT enroll your existing boxes — a box starts serving a tenant because
    someone said so, never as a side effect of a membership.
    Enrolling in a workspace covers every workspace below it, including ones
    created later; separate roots stay separate (`marshellis` is a root, so no
    enrollment in `dimagi` reaches it).
    Migration 0071 enrolled every runner that existed then in every workspace
    its owner could work in, except `marshellis`, so nothing changed for the fleet.
    """

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="enrollments")
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="runner_enrollments"
    )
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["runner_id", "workspace_id"]
        constraints = [
            models.UniqueConstraint(fields=["runner", "workspace"],
                                    name="one_enrollment_per_runner_workspace"),
        ]


class RunnerDrill(models.Model):
    """Latest readiness-drill outcome for one (runner, agent) pair. Reset to
    pending on each fan-out; resolved by the agent's report callback or by the
    drill turn failing. Freshness = finished_at age (no server TTL)."""

    OUTCOME_PENDING, OUTCOME_PASS, OUTCOME_FAIL = "pending", "pass", "fail"
    OUTCOME_CHOICES = [(OUTCOME_PENDING, "Pending"), (OUTCOME_PASS, "Pass"), (OUTCOME_FAIL, "Fail")]

    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="drills")
    agent = models.ForeignKey("agents.Agent", on_delete=models.CASCADE, related_name="runner_drills")
    turn = models.ForeignKey(Turn, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    outcome = models.CharField(max_length=8, choices=OUTCOME_CHOICES, default=OUTCOME_PENDING)
    summary = models.TextField(blank=True, default="")
    # default=timezone.now (not auto_now_add) so start_drill's update_or_create can
    # reset it on every re-drill — auto_now_add only stamps on the INITIAL insert.
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["agent__slug"]
        constraints = [
            models.UniqueConstraint(fields=["runner", "agent"], name="one_drill_row_per_runner_agent"),
        ]


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
    RESOLVED = "resolved"                # the debugger says it is fixed
    ESCALATED = "escalated"              # came back after a fix — Jonathan's now
    STATUS_CHOICES = [
        (OPEN, "Open"), (HELD, "Held by the rate cap"),
        (DEBUGGER_FAILED, "Debug turn failed"), (RESOLVED, "Resolved"),
        (ESCALATED, "Escalated"),
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [models.Index(fields=["status", "-last_seen"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"investigation:{self.pk}:{self.status}:{self.fingerprint[:8]}"


class FleetHold(models.Model):
    """The fleet-wide hold: while `held`, NO runner is handed a turn — anywhere.

    The per-runner pause (`Runner.paused`) parks one box; this parks the whole fleet,
    for the moment something is starting sessions nobody can account for (2026-10-07:
    people-digest turns appearing on a laptop). Turns keep ENQUEUING while held — that
    is the point: they pile up QUEUED, each carrying its origin, `origin_ref.trigger`
    and initiator, so `/harness/turns?status=queued` is the list of everything that
    tried to start, and `unclaimable_queued_turns` names the hold as the reason.

    Enforced in ONE place, `claim.claim_next_turn` (both the HTTP and the WebSocket
    claim run through it), so it binds every runner regardless of its binary. Like the
    runner pause it stops STARTING work, never finishing it: an executing turn keeps
    its lease, and no NEW claim, ride or pin is granted while held.

    What it cannot reach, because none of it passes through a claim: a laptop emdash
    session that keeps working after its turn was closed on delivery (stop that with
    `interrupt_session`); a menu answer pressed into a live session; and `claude -p`
    run directly by canopy CLI tools or a local launchd job.

    A single row (pk=1). Set and released only by a superuser — it spans every tenant.
    """

    SINGLETON_PK = 1

    held = models.BooleanField(default=False)
    note = models.CharField(max_length=500, blank=True, default="")
    held_at = models.DateTimeField(null=True, blank=True)
    held_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="+")

    @classmethod
    def current(cls) -> "FleetHold":
        row, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return row

    @classmethod
    def is_held(cls) -> bool:
        return cls.objects.filter(pk=cls.SINGLETON_PK, held=True).exists()


class RunnerAppPreference(models.Model):
    """Which page the Canopy Runner desktop app opens on, for one person.

    The app loads `/runner-app/` (`apps/harness/views.py::runner_app_home`), which
    redirects here — so a new page is a new choice, with no change on the box.
    No row means the supervisor.
    """

    SUPERVISOR, IDE = "supervisor", "ide"
    #: choice -> the page it opens (root paths; the view adds the script prefix)
    PAGES = {SUPERVISOR: "/supervisor", IDE: "/ide"}
    CHOICES = [(SUPERVISOR, "Supervisor"), (IDE, "IDE")]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="runner_app_preference")
    opens_on = models.CharField(max_length=32, choices=CHOICES, default=SUPERVISOR)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "runner_app_preferences"

    @classmethod
    def opens_on_for(cls, user) -> str:
        if not getattr(user, "pk", None):
            return cls.SUPERVISOR
        row = cls.objects.filter(user=user).first()
        return row.opens_on if row and row.opens_on in cls.PAGES else cls.SUPERVISOR


class ProjectOptIn(models.Model):
    """Which of a person's repos canopy works in — part of their account, never
    local config on a box.

    A person may use canopy for everything (`everything`), or only for the repos
    they name (`projects`); every other repo on their boxes is plain Claude Code
    and canopy leaves it alone. No row opts into NOTHING: canopy is opt-in.
    Enforced at one place, `Runner.project_names`, which every repo-routing,
    session-mirroring and usage read goes through. An agent's own repo is outside
    this choice — an agent is canopy by definition.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="project_opt_in")
    everything = models.BooleanField(default=False)
    projects = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_opt_ins"

    def __str__(self) -> str:  # pragma: no cover
        return f"project-opt-in:{self.user_id}:{'all' if self.everything else len(self.projects)}"

    def covers(self, project: str) -> bool:
        """`everything` covers even a task emdash could not place in a repo; a
        chosen list covers only the repos it names."""
        return self.everything or (bool(project) and project in (self.projects or []))

    @classmethod
    def for_user_id(cls, user_id) -> ProjectOptIn:
        """The person's opt-in; unsaved and empty (nothing) when they have none."""
        row = cls.objects.filter(user_id=user_id).first() if user_id else None
        return row if row is not None else cls(user_id=user_id, everything=False, projects=[])

    @staticmethod
    def clean_names(names) -> list[str]:
        return sorted({str(n).strip() for n in (names or []) if str(n).strip()})


#: How long a pairing invitation stays open: long enough for `up.sh` to bring a
#: box up and the operator to touch their passkey, short enough that a forgotten
#: one is no standing door.
INVITATION_TTL = dt.timedelta(hours=1)


class RunnerInvitation(models.Model):
    """A person invites ONE new cloud box, named, into ONE workspace, bound to the
    box's EC2 instance role — so the box pairs holding no long-lived secret.

    `up.sh` creates it with the operator's own token once the stack's role exists.
    The operator approves it from a browser, behind a passkey step-up
    (`/api/harness/runner-invitations/{id}/approve`) — a token cannot, so neither
    `up.sh` nor the box can approve its own. The box then proves its role
    (`POST /api/harness/runners/pair-by-role`, `aws_iam_proof`) and canopy-web
    creates the Runner (owned by the invitation's creator, in its workspace) with
    an ACTIVE aws-iam device key, and marks the invitation consumed. Single use.

    One live (unconsumed) invitation per (role, name): a new one replaces it.
    """

    PENDING, APPROVED, CONSUMED, EXPIRED = "pending", "approved", "consumed", "expired"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: The Runner.name the box will pair as (`runner/ec2/_lib.sh::runner_name_ok`).
    name = models.CharField(max_length=63)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="runner_invitations")
    #: The person inviting the box: they approve it, and they own the runner.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="+")
    #: The box's instance role, canonical (`aws_iam_proof.canonical_role_arn`).
    role_arn = models.CharField(max_length=300)
    #: sha256 of `role_arn` — the `jkt` the box's aws-iam device key will carry.
    role_jkt = models.CharField(max_length=64, db_index=True)
    #: The box's zero-knowledge ECDH public key (the `<stack>-ecdh` KMS key), when
    #: `up.sh` could read it: one approval covers both. {} when none.
    ecdh_public_jwk = models.JSONField(default=dict, blank=True)
    #: The isolation units the box will pair as, one runner each
    #: (`docs/architecture/runners.md § Isolation units`): `[{name, workspace, kind, slug,
    #: ecdh_public_jwk, runner_id}]`. `kind` is "tree" or "agent" (`slug` names the
    #: agent). Empty for a single-runner invitation, which pairs as `name` in
    #: `workspace` by its instance role (`units_of`). `runner_id` is filled in when
    #: the box pairs.
    units = models.JSONField(default=list, blank=True)
    #: Pair its runners directed-only (`Runner.directed`): a test box (`up.sh
    #: --test`) is directed from the moment it pairs, before anyone could route to it.
    directed = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    consumed_at = models.DateTimeField(null=True, blank=True)
    runner = models.ForeignKey(Runner, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["role_jkt", "name"], condition=models.Q(consumed_at__isnull=True),
                                    name="one_live_runner_invitation_per_role_and_name"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"runner-invitation:{self.name}:{self.status}"

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.expires_at = (self.created_at or timezone.now()) + INVITATION_TTL
        super().save(*args, **kwargs)

    @property
    def expired(self) -> bool:
        return self.consumed_at is None and timezone.now() >= self.expires_at

    @property
    def status(self) -> str:
        if self.consumed_at is not None:
            return self.CONSUMED
        if self.expired:
            return self.EXPIRED
        return self.APPROVED if self.approved_at is not None else self.PENDING


#: How long a laptop pairing waits for its approval and claim.
PAIRING_TTL = dt.timedelta(minutes=15)
#: Added to `expires_at` on approval, so a slow claim after a late approval works.
PAIRING_APPROVAL_GRACE = dt.timedelta(minutes=10)


class RunnerPairing(models.Model):
    """A laptop box asks to be paired, and a person approves it in their browser
    (`apps/harness/api_runner_pairings.py`, spec 2026-10-19-runner-installers).

    The box makes its device key first and opens a pairing with a DPoP proof of
    it (anonymous: no token exists yet). The person opens `/pair/<user_code>`,
    picks a workspace and a name, and approves behind a passkey step-up. The
    approval's one-time secret goes back ONLY through a redirect to the box's
    loopback (`redirect_port`), so whoever approves must be at that machine. The
    box then claims with the secret and a proof of the same key, and gets a
    Runner whose device key is already ACTIVE. Single use; one live pairing per
    key (`jkt`).
    """

    PENDING, APPROVED, CONSUMED, DECLINED, EXPIRED = "pending", "approved", "consumed", "declined", "expired"
    PLATFORMS = ("macos", "windows", "linux")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    #: What the person checks against the box's window: `ABCD-EFGH`.
    user_code = models.CharField(max_length=9, unique=True)
    #: The box's signing key (reduced public JWK) and its RFC 7638 thumbprint.
    public_jwk = models.JSONField(default=dict)
    jkt = models.CharField(max_length=64, db_index=True)
    kind = models.CharField(max_length=16)
    ecdh_public_jwk = models.JSONField(default=dict, blank=True)
    ecdh_store = models.CharField(max_length=16, blank=True, default="")
    #: Self-reported by the box: shown to the approver, trusted for nothing.
    hostname = models.CharField(max_length=100, blank=True, default="")
    account = models.CharField(max_length=100, blank=True, default="")
    platform = models.CharField(max_length=10)
    has_emdash = models.BooleanField(default=False)
    redirect_port = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="+")
    #: The Runner.name the approver chose.
    name = models.CharField(max_length=63, blank=True, default="")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    #: sha256 of the approval's one-time secret; the raw value is only ever in
    #: the loopback redirect.
    secret_hash = models.CharField(max_length=64, blank=True, default="")
    declined_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    runner = models.ForeignKey(Runner, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    #: The runner this box says it already IS (its runner.json), when its token stopped
    #: working: approved by that runner's owner, the claim adds the key to that runner
    #: instead of making a new one. The box's word alone changes nothing (canopy-web#1705).
    reconnect_runner = models.ForeignKey(Runner, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover
        return f"runner-pairing:{self.user_code}:{self.status}"

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.expires_at = (self.created_at or timezone.now()) + PAIRING_TTL
        super().save(*args, **kwargs)

    @property
    def status(self) -> str:
        if self.consumed_at is not None:
            return self.CONSUMED
        if self.declined_at is not None:
            return self.DECLINED
        if timezone.now() >= self.expires_at:
            return self.EXPIRED
        return self.APPROVED if self.approved_at is not None else self.PENDING

class PrivateRunner(models.Model):
    """A PERSON's own machine: plain Claude Code with the canopy plugin, no runner app.

    On its own it never claims a turn and nothing routes to it. It exists so a session a person
    opens there with an agent is a real, recorded turn (`Turn.ORIGIN_LOCAL`,
    `apps/harness/local.py`): who ran it, on which machine, under which license.
    It belongs to the person, not to an agent or a workspace — one private runner serves
    every agent they use. canopy-web#1704. When the machine also runs the runner app,
    `runner` links the two, and that runner is marked `private` (canopy-web#1705).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="private_runners")
    #: What the person calls it (`canopy private-runner register --name`), e.g. aath-mbp.
    name = models.CharField(max_length=100)
    #: The hostname it registered from — a hint for the person, never an identity.
    host = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    #: Last time a local turn opened or renewed from it.
    last_seen_at = models.DateTimeField(null=True, blank=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    #: The runner this machine also is (canopy-web#1705): one machine, one object with
    #: two powers — local sessions here, and the runner app's turns. Its device key is
    #: what a local turn proves itself with.
    runner = models.OneToOneField("harness.Runner", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="private_runner")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["owner", "name"], name="one_private_runner_name_per_owner"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"private_runner:{self.name}"


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
