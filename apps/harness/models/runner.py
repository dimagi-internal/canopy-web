"""Runners and the per-runner records (flags, assignments, enrollment, projects).

Framework tier. Split from the former models.py; `apps.harness.models` re-exports.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from .. import canopy_release, clis

# Owned here (not services.py) so Runner.live_status can use it without a
# models -> services import cycle (services already imports models). services.py
# imports this constant from here rather than declaring a second one.
HEARTBEAT_ONLINE_WINDOW = dt.timedelta(seconds=90)


#: The capability that says what a runner serves (`Runner.serves`).
SERVES = "serves"


def serves_of(capabilities) -> dict:
    """What a capabilities dict says its runner serves: `capabilities["serves"]`. {} for none."""
    said = (capabilities or {}).get(SERVES)
    return said if isinstance(said, dict) else {}


class Runner(models.Model):
    """A paired executor (laptop emdash daemon, cloud container, remote box)."""

    EMDASH, CLOUD, REMOTE = "emdash", "cloud", "remote"
    # A person's COMPUTER used in plain Claude Code, with no runner app: the
    # lightweight runner a `PrivateRunner` gets when it is registered without one,
    # so the machine can hold a device key — sign its owner's direct sessions in and
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
    # The name the box paired as: what it still sends when it pairs again (a replaced
    # instance re-attaching). `name` is what people see and may rename; this never changes.
    paired_as = models.CharField(max_length=100, blank=True, default="", db_default="")
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
    # The operating system the box said it runs when it paired from the browser
    # (`RunnerPairing.PLATFORMS`), refreshed by every pairing that reconnects it; ""
    # when it never said (a PAT pairing, a cloud box). Self-reported and trusted for
    # nothing but wording: a remedy names the fix that exists on that OS.
    platform = models.CharField(max_length=10, blank=True, default="", db_default="")
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
    # When this runner last ASKED FOR WORK (`claim_next_turn`), as opposed to merely
    # heartbeating. The two differ: a laptop's daemon can heartbeat for hours while
    # the app that actually takes turns is asleep or stuck, and from the server an
    # `online` runner then looks like one that is about to pick a queued turn up
    # (2026-10-30: a turn pinned to an "online, ready" laptop sat 5h16m). Written at
    # most every POLL_RECORD_EVERY_S. Null = never since this column existed.
    last_claim_poll_at = models.DateTimeField(null=True, blank=True)
    # A FLEET member (`apps/harness/fleets.py`): one instance of an EC2 Auto Scaling
    # group a person approved once, joined on AWS's word that the instance is the
    # group's, never by a passkey per box. Interchangeable: canopy-web grows and
    # shrinks the group, and a member's chats move to another member when it goes.
    # NULL for every other runner.
    fleet = models.ForeignKey("RunnerFleet", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="members")
    #: A member's EC2 instance id (`i-…`): its identity, with the fleet's role.
    instance_id = models.CharField(max_length=32, blank=True, default="")
    #: The one credential list this runner reads instead of its own
    #: (`RunnerCredentialSet`): every member of a fleet reads its fleet's, and any
    #: other cloud runner of the set's owner may join one (`credential_sets.join`).
    credential_set = models.ForeignKey("RunnerCredentialSet", on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="runners")
    # Where the fleet scaler has a member, not whether it heartbeats: `live_status`
    # still answers that. ON (in service), DRAINING (idle, claims refused, scaled
    # in on the next tick if still idle), STOPPED (in the group's warm pool),
    # WAKING (coming into service). Always ON for a runner that is not a member.
    POWER_ON, POWER_DRAINING, POWER_STOPPED, POWER_WAKING = "on", "draining", "stopped", "waking"
    POWER_CHOICES = [(POWER_ON, "On"), (POWER_DRAINING, "Draining"),
                     (POWER_STOPPED, "Stopped"), (POWER_WAKING, "Waking")]
    power = models.CharField(max_length=10, choices=POWER_CHOICES, default=POWER_ON)
    power_changed_at = models.DateTimeField(null=True, blank=True)
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
    # The box's slash commands are NOT a column here — see RunnerSlashCommands.
    # An operator asked this box to refresh itself (re-run bootstrap: plugins,
    # the canopy CLI, Claude Code, agent provisioning). DURABLE, not a frame: a
    # control frame is a doorbell, and one sent while the socket is down is
    # simply lost (spec 2026-08-01). The runner reads `refresh_pending` off its
    # heartbeat reply and restarts itself once idle; the request is discharged by
    # the box reporting a bootstrap newer than it (`health.bootstrapped_at`), so
    # nothing has to clear it and a refresh that never happened stays visible.
    refresh_requested_at = models.DateTimeField(null=True, blank=True)
    # Who asked (NULL: canopy itself, e.g. a new agent's setup), what the box was
    # on when they did (`runner_refresh.versions`), and when the server saw the
    # request discharged — the page's before → after, and the one notice the
    # asker gets (`runner_refresh.finished`).
    refresh_requested_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Through what they asked (`audit.request_via`): an AI session calling with a
    # person's login is that person's `by` but not their request, so only `ui` is told.
    refresh_requested_via = models.CharField(max_length=16, blank=True, default="")
    refresh_before = models.JSONField(default=dict, blank=True)
    # What the box was on when the refresh finished: an update landing later is not
    # something this refresh changed.
    refresh_after = models.JSONField(default=dict, blank=True)
    refresh_finished_at = models.DateTimeField(null=True, blank=True)
    # When the box last posted its usage report (`apps/harness/usage.py`).
    usage_reported_at = models.DateTimeField(null=True, blank=True)
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
    # pairing by role). Several runners can share a machine, and so share this. "" = not known (a laptop, or a box that never proved a
    # role). Read by `routing.serves_refusal` and the topology's grouping; never a
    # grant on its own.
    machine_id = models.CharField(max_length=300, blank=True, default="", db_index=True)
    #: A PRIVATE runner is its owner's own machine (canopy-web#1705): it takes no fleet
    #: work — nothing routed, no workspace order, no source rule — only what its owner
    #: puts on it (a pin, a session bound to it) and the direct sessions opened on it
    #: (`apps/harness/local_turns.py`). Set by its owner, never reported by the box.
    private = models.BooleanField(default=False)
    #: The agents this box is DEDICATED to: when any are listed it runs only their
    #: turns and chats — no other agent's, no repo turn, no agentless chat — however
    #: they reach it (an assignment, an inherited order, a pin). Empty = no
    #: restriction. Set by a person on the runner page, never reported by the box,
    #: and it only ever narrows what the box takes — the person-set counterpart of a
    #: box declaring it runs one agent (`serves()`); both apply when both are set
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
    #: only ever adds to the box's own floor (box.env), and a sensitive-data box is locked
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
        if creating and not self.paired_as:
            self.paired_as = self.name
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

    def machine_name(self) -> str:
        """The machine this runner is on, as a person reads it: the instance role's
        name without its account or the generated `-RunnerRole-…` suffix — the
        stack's name (canopy-cloud-runner-2). "" when not known (a laptop)."""
        role = self.machine_id.rsplit("/", 1)[-1] if self.machine_id else ""
        return role.split("-RunnerRole", 1)[0]

    def serves(self) -> dict:
        """What this runner says it serves (`capabilities.serves`):
        `{"kind": "tree", "root": <slug>}` or `{"kind": "agent", "slug": <slug>}`;
        {} for a runner that declares none. Box-declared, and only ever a
        restriction on what the box takes (`routing.serves_refusal`)."""
        return serves_of(self.capabilities)

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
        """Asked to refresh, and has not bootstrapped since — by an operator, or by
        canopy itself when the box's canopy CLI is behind the release
        (`canopy_release.refresh_owed`): a stale canopy is fixed by a re-bootstrap,
        not by the runner-code updater, so it rides this request, not the
        `runner.update_available` doorbell."""
        return self.refresh_requested() or canopy_release.refresh_owed(self.health)

    def refresh_requested(self) -> bool:
        """A Refresh someone asked for (`refresh_requested_at`) that the box has not
        bootstrapped since — the request the runner page and its asker follow
        (`runner_refresh`), without canopy's own CLI-behind ask."""
        if self.refresh_requested_at is None:
            return False
        try:
            booted = float((self.health or {}).get("bootstrapped_at") or 0)
        except (TypeError, ValueError):
            booted = 0.0
        return booted < self.refresh_requested_at.timestamp()

    def expected_canopy_version(self) -> str:
        """The canopy release this box should run (`canopy_release.expected`).
        Empty means UNKNOWN, never "stale"."""
        return canopy_release.expected()

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
    def report_names(self) -> set[str]:
        """The names a box's own reports for this runner may be filed under: the name it
        paired as (`$RUNNER_NAME` on the box, which `AgentBootstrapReport.runner_name`
        records) and the name people see now. They differ once the runner is renamed."""
        return {n for n in (self.name, self.paired_as) if n}

    @property
    def is_available(self) -> bool:
        """Can this runner take work RIGHT NOW — online (fresh heartbeat) AND
        self-reported ready. The cascade's availability probe (spec 2026-07-24)."""
        return self.live_status == Runner.ONLINE and self.ready

    @property
    def wakeable(self) -> bool:
        """A fleet member that is not up but that the scaler brings into service for
        work (`fleets.tick`): its fleet active, it not paused, retired or signed out.
        Every reader of "will a runner take this?" (`claim.turn_reach`, the
        unclaimable report) counts it as on its way, so work for a fleet whose
        members are in its warm pool is neither refused by an admission rule nor
        reported stuck while one starts."""
        if self.fleet_id is None or self.paused or self.status == self.RETIRED:
            return False
        # Signed out of every agent CLI, as the claim reads it (`clis.reported`).
        if not any(r.signed_in for r in clis.reported(self).values()):
            return False
        if self.fleet.status != type(self.fleet).ACTIVE:
            return False
        return self.power != self.POWER_ON or self.live_status != self.ONLINE

    @property
    def signed_out(self) -> bool:
        """The box says its Claude login is gone (`canopy_transcript.SIGNED_OUT_NOTE`,
        set from the CLI's own "Not logged in" record). Unlike other not-ready
        notes this one means every turn it took would come back as that one line,
        so it claims nothing (`claim_next_turn`) and a turn waiting on it says why."""
        from canopy_transcript import SIGNED_OUT_NOTE

        return not self.ready and self.ready_note.startswith(SIGNED_OUT_NOTE)

    @property
    def setting_up(self) -> str:
        """Why this box is not asking for work just now, in its own words ("restarting",
        "installing updated tools"), or "". It resolves on its own within moments, so
        a queued turn says so rather than sounding stuck. Read from its last beat: a box
        that stops beating stops being ONLINE, and then this is not what anyone reads."""
        return str((self.health or {}).get("setting_up") or "")

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
            if self.platform == "windows":
                # No "Emdash CDP" launcher exists on Windows (it is an AppleScript app).
                return ("Emdash is running without its debug port, and Windows has no "
                        '"Emdash CDP" launcher to reopen it with one. Switch this runner '
                        "to Claude CLI (its page → Settings → Session runtime → Claude CLI), "
                        "or quit Emdash and start it with --remote-debugging-port=<the "
                        "cdp_port in its runner.json>.")
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


class RunnerSlashCommands(models.Model):
    """The slash commands a Claude session on this box can run (its account's
    skills and commands, its enabled plugins', and what ACP announced), as
    [{name, description, args_hint, source, internal}] — REPORTED by the box when
    the list changes (`canopy_runner.slash_commands`), never typed. Feeds the chat
    composer's `/` autocomplete, which leaves out `internal` entries, and the skill
    catalog, which keeps them. No row = never reported (an older runner); no
    `internal` key = an older runner's report, and not internal.

    A table of its own, never a column on Runner: one list runs to ~460 rows and
    ~120 KB, and a Runner row is loaded on every poll a box makes and by every
    `select_related` that reaches one, so a column there is parsed into Python
    on nearly every request. Only the report endpoint and the composer read this.
    """

    runner = models.OneToOneField(Runner, on_delete=models.CASCADE, primary_key=True,
                                  related_name="+")
    commands = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"slash-commands:{self.runner_id}"


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


class RunnerFlag(models.Model):
    """One property a runner's OWNER vouches for (spec 2026-09-30-zdr-runners).

    canopy cannot verify any of these -- `sensitive_data` means "this box runs Claude
    only on a ZDR API key, under a locked harness", which is not observable from here -- so
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
    turn = models.ForeignKey("harness.Turn", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
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


class RunnerProjectRequest(models.Model):
    """A person asked a LAPTOP runner to make a repo one of its emdash projects.

    One row per (runner, project name): asking again re-arms the same row. The ask
    rides the box's heartbeat reply (`RunnerOut.add_projects`, the durable-ask rule
    — never a frame); the box clones the repo under its own account, opens it in
    emdash through emdash's own Add Project dialog, and reports back
    (`POST …/projects/{id}/result`). A pending ask is also discharged by
    observation: the moment the box's heartbeat reports the project
    (`capabilities.projects`), it is ADDED, whoever made it.

    A failed add marks only this row failed. It never touches the box's
    readiness: a missing repo is a per-agent setup gap, not box health.
    """

    PENDING, ADDED, FAILED = "pending", "added", "failed"
    STATUS_CHOICES = [(PENDING, "Pending"), (ADDED, "Added"), (FAILED, "Failed")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    runner = models.ForeignKey(Runner, on_delete=models.CASCADE, related_name="project_requests")
    #: The emdash project name — an agent's slug when it is the agent's repo.
    name = models.CharField(max_length=100)
    repo_url = models.CharField(max_length=500)
    agent = models.ForeignKey("agents.Agent", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="+")
    requested_by = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=PENDING)
    #: The box's own words for what happened (a git error, the dialog that would
    #: not take the path) — shown to the person as the reason.
    detail = models.TextField(blank=True, default="")
    requested_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "runner_project_requests"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["runner", "name"], name="one_project_request_per_runner_name"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"runner-project:{self.runner_id}:{self.name}:{self.status}"


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
