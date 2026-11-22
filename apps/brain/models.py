"""The brain's own tables: what may be learned from (`BrainSource`), who is in it
(`SourceParticipant`), who agreed to what (`LearningConsent`, `SourceExclusion`) and what
learns (`BrainCapability`). `docs/architecture/brain.md` says how they connect; the one
question they answer together is `eligibility.eligible`.

Nothing here holds words. A source POINTS at a session; the session's own rows hold what
was said, and every rule about them (sensitive, a contact's, deleted by its person) is read
live from the session, never copied here."""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone

#: What a person can agree canopy learns from their sessions for. The class names are
#: the contract: a capability declares one, a consent lists some.
INDIVIDUAL, MEMORY, PROJECT, SYSTEM = "individual", "memory", "project", "system"
CLASSES = (INDIVIDUAL, MEMORY, PROJECT, SYSTEM)
#: Where a brain run's conversation records its run (`canopy_sessions.models.BRAIN_RUN_METADATA_KEY`).
BRAIN_RUN_KEY = "brain_run"
#: Classes that learn about ONE PERSON from their own words (only that person's consent
#: counts) versus the ones that learn about a project or canopy from everyone's (every
#: human participant must agree).
ABOUT_A_PERSON = frozenset({INDIVIDUAL, MEMORY})
SHARED = frozenset({PROJECT, SYSTEM})


def person_id(subject_type: str, subject: str) -> int | None:
    """The user id a (`subject_type`, `subject`) pair is about, or None: not about a person, or not a user id."""
    return int(subject) if subject_type == "person" and (subject or "").isdecimal() else None


def project_id(subject_type: str, subject: str) -> int | None:
    """The `AgentProject` pk a (`subject_type`, `subject`) pair is about, or None."""
    return int(subject) if subject_type == "project" and (subject or "").isdecimal() else None


class BrainSource(models.Model):
    """One thing canopy could learn from. A session today; later a meeting, a mail thread,
    a channel ingest, a document (`kind`). Holds no content."""

    SESSION = "session"
    KIND_CHOICES = [(SESSION, "Session")]
    OPEN, WITHDRAWN = "open", "withdrawn"
    STATE_CHOICES = [(OPEN, "Open"), (WITHDRAWN, "Withdrawn")]
    #: Why a source is shut to every use. Read LIVE from the session by `eligibility`;
    #: this column is what a screen shows and what the last refresh saw.
    SEALED_SENSITIVE, SEALED_CONTACT, SEALED_DELETED = "sensitive", "contact", "words_deleted"
    #: A learning run's own conversation (`BRAIN_RUN_KEY`): what it says is other people's sessions retold.
    SEALED_BRAIN_RUN = "brain_run"

    kind = models.CharField(max_length=24, choices=KIND_CHOICES, default=SESSION)
    #: The source's id within its kind (a session's uuid). The key; `session` is the
    #: typed pointer for the one kind that exists.
    ref = models.CharField(max_length=64)
    session = models.ForeignKey("canopy_sessions.Session", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="brain_sources")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default=OPEN)
    sealed_reason = models.CharField(max_length=16, blank=True, default="")
    #: When the associator should look at this source next (`associate.run_due`, the one column it asks, indexed
    #: over open sources): when it is made, then by an activity fold, so a burst of folds is one look; null once
    #: looked at.
    associate_due_at = models.DateTimeField(null=True, blank=True, default=timezone.now)
    #: When it was last looked at; null means never.
    associated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "ref"], name="brain_source_one_per_ref")]
        indexes = [models.Index(fields=["associate_due_at"], condition=models.Q(state="open"),
                                name="brain_source_due")]


class SourceParticipant(models.Model):
    """A person whose words are in a source, filled only from what canopy already knows
    (`participants.py`). Agents are never participants. A CONTACT is, and makes the source
    ineligible for every shared use."""

    CREATOR, PARTICIPANT, BOX_OWNER = "creator", "participant", "box_owner"
    HOW_CHOICES = [(CREATOR, "Started it"), (PARTICIPANT, "In it"), (BOX_OWNER, "Owns the box it ran on")]

    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                             related_name="+")
    contact = models.ForeignKey("contacts.Contact", null=True, blank=True, on_delete=models.CASCADE,
                                related_name="+")
    how = models.CharField(max_length=12, choices=HOW_CHOICES, default=PARTICIPANT)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(user__isnull=False, contact__isnull=True)
                           | models.Q(user__isnull=True, contact__isnull=False)),
                name="brain_participant_user_xor_contact"),
            models.UniqueConstraint(fields=["source", "user"], condition=models.Q(user__isnull=False),
                                    name="brain_participant_one_user"),
            models.UniqueConstraint(fields=["source", "contact"], condition=models.Q(contact__isnull=False),
                                    name="brain_participant_one_contact"),
        ]


class LearningConsent(models.Model):
    """A person's agreement to canopy learning, by purpose class. One live row per person.

    The person, as themselves, makes or changes it (`consent.py`, `api.py`); a workspace's manager
    may only ADD classes for its members (`consent.admin_opt_in`), never take one away. `CoachingOptIn` is carried over as the
    `individual` class and still written beside this row until its readers move."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="learning_consents")
    #: Subset of `CLASSES`.
    classes = models.JSONField(default=list)
    #: Workspace slugs the consent covers; empty means every workspace the person is in.
    workspaces = models.JSONField(default=list, blank=True)
    #: With `workspaces` set: whether a workspace the person was not in at the last edit
    #: (not in `known_workspaces`) is covered too.
    include_new = models.BooleanField(default=True)
    #: The person's workspaces when `workspaces` was last set; what makes one "new".
    known_workspaces = models.JSONField(default=list, blank=True)
    #: Deny-only rules, each `{"capability"?, "purpose"?, "class"?, "workspace"?}`; a rule
    #: denies a use when every key it names matches (`eligibility.rule_denies`).
    standing_rules = models.JSONField(default=list, blank=True)
    #: Words a capability must be shown with every read of the person's sessions.
    directives = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [models.UniqueConstraint(fields=["user"], condition=models.Q(revoked_at__isnull=True),
                                               name="learning_consent_one_live")]


class SourceExclusion(models.Model):
    """One person taking one source out of learning. Its own row, so removing it later
    restores nothing the cascade already withdrew (`cascade.py`)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="exclusions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "source"], name="brain_exclusion_one")]


class BrainCapability(models.Model):
    """Something that learns: registered by a canopy admin (`registry.py`), in one purpose
    class, declaring the purposes it reads for. A capability may only NARROW its class's
    audience, never widen it."""

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    purpose_class = models.CharField(max_length=12)
    #: What the capability learns ABOUT: `person` or `project`; fixed by the class.
    subject_type = models.CharField(max_length=12, default="person")
    purposes = models.JSONField(default=list, blank=True)
    source_kinds = models.JSONField(default=list, blank=True)
    output_kinds = models.JSONField(default=list, blank=True)
    #: Who sees its outputs; derived from the class, may only narrow it.
    audience = models.JSONField(default=list, blank=True)
    operator = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    #: What a run of it starts with: one plugin command, `/<plugin>:<skill> [args]`, run by `operator`.
    entry = models.CharField(max_length=300, blank=True, default="")
    #: The canopy admin who registered it: whom its runs answer to (`runs.start`).
    registered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    enabled = models.BooleanField(default=False)
    #: How much of a session its runs read: a view of the core read (`transcript_read.VIEWS`), the whole
    #: session (`full`) unless narrowed. May only narrow (`registry.update`), and each change is an event.
    scope = models.CharField(max_length=16, default="full", db_default="full")
    created_at = models.DateTimeField(auto_now_add=True)


class ProjectSignature(models.Model):
    """What a project looks like in a session's recorded activity: the parameters `associate.evaluate`
    tests. Seeded from the sessions certainly on the project (`associate.seed_signature`), then only
    ever added to. Metadata about code and tool names, never words."""

    project = models.OneToOneField("agents.AgentProject", on_delete=models.CASCADE, related_name="brain_signature")
    repo = models.CharField(max_length=100, blank=True, default="")
    remote = models.CharField(max_length=200, blank=True, default="")
    #: Repo-relative directories (`connect_labs/supply_chain`), matched on whole path segments.
    paths = models.JSONField(default=list, blank=True)
    exclude_paths = models.JSONField(default=list, blank=True)
    #: Matched against a tool's name after `mcp__<server>__` (`supply_chain_`).
    mcp_prefixes = models.JSONField(default=list, blank=True)
    #: fnmatch patterns over a session's branches: a WEAK signal.
    branch_patterns = models.JSONField(default=list, blank=True)
    #: Lower-case words in a session's title, branches and working folders: a WEAK signal.
    name_terms = models.JSONField(default=list, blank=True)
    #: The project's own PRs, `{"<owner/name>#<number>": {"cross_cutting": false}}`. A cross-cutting one is weak.
    prs = models.JSONField(default=dict, blank=True)
    mcp_min = models.PositiveSmallIntegerField(default=3)
    seeded_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class SourceLink(models.Model):
    """A source's tie to a subject. Metadata only: a link confers no read by itself, and only a `core`
    link ever opens the project read leg (`readers.py`). One row per (source, subject); the strongest layer
    wins (`associate.RANK`), and a person's own rejection sticks."""

    PERSON, PROJECT_SUBJECT = "person", "project"
    DISPATCHED, SIGNALS, JUDGED, DECLARED = "dispatched", "signals", "judged", "declared"
    HOW_CHOICES = [(DISPATCHED, "Dispatched from a task"), (SIGNALS, "Signals in its activity"),
                   (JUDGED, "A model's call"), (DECLARED, "Declared")]
    CORE, ADJACENT, CANDIDATE, REJECTED = "core", "adjacent", "candidate", "rejected"
    TIER_CHOICES = [(CORE, "Core"), (ADJACENT, "Adjacent"), (CANDIDATE, "Candidate"), (REJECTED, "Rejected")]

    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="links")
    subject_type = models.CharField(max_length=16, default=PROJECT_SUBJECT)
    #: The subject's id as text (an `AgentProject` pk for a project), so later subject types fit.
    subject = models.CharField(max_length=64)
    how = models.CharField(max_length=12, choices=HOW_CHOICES)
    tier = models.CharField(max_length=12, choices=TIER_CHOICES)
    confidence = models.FloatField(null=True, blank=True)
    #: Short reasons ("owns 1 project PR(s)", "task 41"): ids and counts, never a quote.
    evidence = models.JSONField(default=list, blank=True)
    #: The model that judged it, for `how == judged`.
    model = models.CharField(max_length=64, blank=True, default="")
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                           related_name="+")
    by_agent = models.ForeignKey("agents.Agent", null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    #: Everyone else who rejected it (user ids), besides `by`: each person's rejection sticks until THEY take it back,
    #: so when `by` does, the next of these holds it (`associate.undeclare`).
    rejected_by = models.JSONField(default=list, blank=True, db_default=[])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "subject_type", "subject"],
                                               name="brain_link_one_per_subject")]
        indexes = [models.Index(fields=["subject_type", "subject", "tier"], name="brain_link_by_subject")]


class ReportingLine(models.Model):
    """`manager` is `person`'s manager in `workspace`, set by a workspace owner (`reporting.py`). The
    one thing it opens is a manager reading their reports' `individual` OUTPUTS (never a transcript):
    canopy had no manager relationship before this. Ending one sets `ended_at`; every change is an
    event."""

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    manager = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    set_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["workspace", "person"], condition=models.Q(ended_at__isnull=True),
                                    name="brain_one_live_manager"),
            models.CheckConstraint(condition=~models.Q(person=models.F("manager")), name="brain_not_own_manager"),
        ]


class BrainOutput(models.Model):
    """What a learning feature produced, through `brain_emit`: about one subject, for its audience. The
    `body` is the output's own words; `OutputLineage` says which sources it came from, so a removal can
    reach it."""

    ACTIVE, WITHDRAWN, STALE = "active", "withdrawn", "stale"
    STATE_CHOICES = [(ACTIVE, "Active"), (WITHDRAWN, "Withdrawn"), (STALE, "A source was removed")]

    capability = models.ForeignKey(BrainCapability, on_delete=models.PROTECT, related_name="outputs")
    kind = models.CharField(max_length=40)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    #: Who or what it is about: `person` (a user id) or `project` (an `AgentProject` pk).
    subject_type = models.CharField(max_length=16)
    subject = models.CharField(max_length=64)
    title = models.CharField(max_length=300, blank=True, default="")
    body = models.JSONField(default=dict, blank=True)
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default=ACTIVE)
    turn_id = models.CharField(max_length=40, blank=True, default="")
    #: The run that emitted it, and a hash of what it said and cited, so emitting twice is one output.
    run = models.ForeignKey("brain.BrainRun", null=True, blank=True, on_delete=models.SET_NULL, related_name="outputs")
    fingerprint = models.CharField(max_length=64, blank=True, default="")
    #: Where it stands with the people who act on it (`findings.STATUSES`), apart from `state`, which is the cascade's.
    #: Set by a person who may act on it (`findings.may_set`); a repeat folds into an open or accepted one.
    status = models.CharField(max_length=12, default="open", db_default="open")
    status_at = models.DateTimeField(null=True, blank=True)
    status_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    #: What makes two outputs the same thing, as the run that emitted it names it (a short slug); "" never folds.
    key = models.CharField(max_length=200, blank=True, default="", db_default="")
    #: How many emits folded into it (the first counts), and when the last one did.
    seen = models.PositiveIntegerField(default=1, db_default=1)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    #: The output this one was merged into as the same problem (`findings.merge`, status `duplicate`): a repeat of its
    #: key folds there.
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="merged")
    #: The `done` finding of the same kind and key this one is a repeat of (`findings.came_back_of`): it came back after
    #: it was acted on, which is how a learner's fixes are measured.
    recurs_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="recurrences")
    #: When each emit saw it (`findings.SIGHTINGS_KEPT` newest, ISO times with the run): its sightings over time.
    sightings = models.JSONField(default=list, blank=True, db_default=[])
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def subject_user_id(self) -> int | None:
        """The person it is about (a user id), or None (`person_id`)."""
        return person_id(self.subject_type, self.subject)

    @property
    def subject_project_id(self) -> int | None:
        """The project it is about (an `AgentProject` pk), or None (`project_id`)."""
        return project_id(self.subject_type, self.subject)

    class Meta:
        indexes = [models.Index(fields=["subject_type", "subject", "state"], name="brain_output_by_subject"),
                   models.Index(fields=["capability", "workspace", "key"], name="brain_output_by_key")]
        constraints = [models.UniqueConstraint(fields=["run", "fingerprint"], condition=~models.Q(fingerprint=""),
                                               name="brain_output_once_per_run")]


class OutputLineage(models.Model):
    """One output's one source: written by `brain_emit` from the turn's ACTUAL reads, so the cascade
    knows what to withdraw. `output_kind` names the store (`brain.output` now; an existing store later
    carries its own kind). `participant_scope` is the people whose words were in the source at the time:
    the consents the output carries.

    An output carries a row for EVERY source its run had read when it was written, not only those it cites: what a
    run read is all in its context, so a removal of any of them reaches the output (as it reaches the run's artifacts
    and conversation). `cited` marks the ones it names as its evidence, which are what a screen shows."""

    output_kind = models.CharField(max_length=40, default="brain.output")
    output_id = models.CharField(max_length=64)
    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="lineage")
    participant_scope = models.JSONField(default=list, blank=True)
    cited = models.BooleanField(default=True, db_default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["output_kind", "output_id", "source"],
                                               name="brain_lineage_one")]
        indexes = [models.Index(fields=["output_kind", "output_id"], name="brain_lineage_by_output")]


class BrainRun(models.Model):
    """One run of a learning feature: a conversation of its operator agent, whose turn is an ordinary full-profile
    turn of that agent set up by the learning system (`context`), reading the sessions due only through `brain_read`
    and recording what it makes against them (`lineage`). The server makes it (`runs.start`); nothing a turn says
    makes one."""

    OPEN, DONE, FAILED = "open", "done", "failed"
    STATE_CHOICES = [(OPEN, "Open"), (DONE, "Done"), (FAILED, "Failed")]

    capability = models.ForeignKey(BrainCapability, on_delete=models.PROTECT, related_name="runs")
    #: What it is about: `person` (a user id), `project` (a project pk) or `system` (its `scope`).
    subject_type = models.CharField(max_length=16)
    subject = models.CharField(max_length=64, blank=True, default="")
    #: The one purpose this run reads for (one of its feature's declared purposes).
    purpose = models.CharField(max_length=40)
    #: The workspace the run was started for (a slug; "" for none): it reads only eligible sources of that workspace
    #: and the ones below it (`reading.candidates`), and what it emits is that workspace's (`emit`), seen by its
    #: owners and its ancestors' (`outputs.visible_to`). A `system` run's subject is this slug too, so one run is
    #: open per feature and workspace (`brain_one_open_run`).
    scope = models.CharField(max_length=64, blank=True, default="", db_default="")
    #: Unused: what a run reads is bounded by its target's window of recent activity (`reading.window_start`), never by
    #: a source id. Kept only because migrations are additive.
    floor = models.BigIntegerField(default=0)
    #: Its conversation (`canopy_sessions.Session`, `metadata.brain_run`).
    session = models.ForeignKey("canopy_sessions.Session", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    state = models.CharField(max_length=8, choices=STATE_CHOICES, default=OPEN)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    #: The target it ran for (`BrainTarget`), when one started it: its schedule's fire or its Run now.
    target = models.ForeignKey("BrainTarget", null=True, blank=True, on_delete=models.SET_NULL, related_name="runs")
    #: The schedule slot it answers (null for a Run now): one run per target and slot, however many firers report it.
    slot = models.DateTimeField(null=True, blank=True)
    #: The context document the run was started with (`context.document`, typed by `schemas.BrainRunContext`, with
    #: its `version`): what its turn was told, kept as it was told. Ids and names, never a word of a session.
    context = models.JSONField(default=dict, blank=True, db_default={})

    @property
    def subject_user_id(self) -> int | None:
        """The person it is about (a user id), or None (`person_id`)."""
        return person_id(self.subject_type, self.subject)

    class Meta:
        #: One open run per feature and subject: two workers starting the same run race on this, not on a read.
        constraints = [models.UniqueConstraint(fields=["capability", "subject_type", "subject"],
                                               condition=models.Q(state="open"), name="brain_one_open_run"),
                       models.UniqueConstraint(fields=["target", "slot"], condition=models.Q(slot__isnull=False),
                                               name="brain_one_run_per_slot")]
        #: What a feature already learned about a subject (`reading.learned`), and a target's day (`budget`).
        indexes = [models.Index(fields=["capability", "subject_type", "subject", "state"], name="brain_run_by_subject"),
                   models.Index(fields=["target", "created_at"], name="brain_run_by_target_day")]


class BrainTarget(models.Model):
    """Where a learner runs: a workspace (its subtree) or a workspace project (the sessions `core`-tied to it). A learner
    with no target never runs. Each target has its own runs, what they read (`reading.learned`) and findings, and is
    started by its own `schedule`: a normal canopy schedule on the learner's operator agent (`harness.AgentSchedule`),
    whose fire starts a brain run instead of a plain turn (`targets.announce`, registered in `harness.schedule_hooks`).
    Deleting the schedule deletes the target. Added and removed on the target workspace's brain page by its owners and
    admins (or an ancestor's), `targets.add` / `targets.remove`."""

    capability = models.ForeignKey(BrainCapability, on_delete=models.CASCADE, related_name="targets")
    #: The workspace it covers (with its subtree); for a project target, the project's own workspace.
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    #: A workspace project; null for a workspace target.
    project = models.ForeignKey("agents.AgentProject", null=True, blank=True, on_delete=models.CASCADE,
                                related_name="+")
    schedule = models.OneToOneField("harness.AgentSchedule", on_delete=models.CASCADE, related_name="brain_target")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    #: Its budget (`budget.py`): sessions a run reads and outputs it writes; runs, outputs and tokens per UTC day.
    #: A run stops cleanly at a cap: `brain_read` serves nothing more, `brain_emit` refuses, a fire starts nothing.
    max_sources_per_run = models.PositiveIntegerField(default=50, db_default=50)
    max_outputs_per_run = models.PositiveIntegerField(default=20, db_default=20)
    max_runs_per_day = models.PositiveIntegerField(default=4, db_default=4)
    max_outputs_per_day = models.PositiveIntegerField(default=50, db_default=50)
    #: Null: no token cap. Checked before a run starts, from what the boxes reported (`events.SessionUsage`).
    max_tokens_per_day = models.PositiveBigIntegerField(null=True, blank=True)
    #: How far back its runs read: only sessions active in the last this many days (`budget.lookback_days`; null: the
    #: kind's default). And how long an open finding nobody re-sees stays fresh (`budget.fade_days`; null: the default).
    lookback_days = models.PositiveIntegerField(null=True, blank=True)
    fade_days = models.PositiveIntegerField(null=True, blank=True)
    #: The fade window by a finding's type (`budget.fade_days_for`): {type: days}, over `budget.FADE_BY_TYPE`.
    fade_days_by_type = models.JSONField(default=dict, blank=True, db_default={})
    #: How long a dismissed finding's repeats fold into it and stay dismissed (`budget.dismiss_cooldown_days`; null:
    #: the default).
    dismiss_cooldown_days = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["capability", "workspace"], condition=models.Q(project__isnull=True),
                                    name="brain_one_workspace_target"),
            models.UniqueConstraint(fields=["capability", "project"], condition=models.Q(project__isnull=False),
                                    name="brain_one_project_target"),
        ]


class BrainRunRead(models.Model):
    """A source a run actually read: what `brain_emit` checks every cited source against, and the
    lineage an output is written from."""

    run = models.ForeignKey(BrainRun, on_delete=models.CASCADE, related_name="reads")
    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="+")
    read_at = models.DateTimeField(auto_now_add=True)
    #: How far into the session it read: the last row (`Message.turn_index`) served. A session that grew well past it
    #: since a clean read is due again, from here (`reading.REREAD_ROWS`). Null: unknown (read before it was kept).
    through = models.IntegerField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "source"], name="brain_run_reads_once")]


class RederiveRequest(models.Model):
    """An output derived from several sources lost one: it is marked `stale` (hidden, words emptied) and asks to
    be derived again from what is left. The next run of its feature for its subject reads those sources again
    (`reading.candidates`), and a clean end of that run closes the request (`runs.finish`, `done_at`)."""

    output = models.ForeignKey(BrainOutput, on_delete=models.CASCADE, related_name="rederive_requests")
    reason = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    done_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["output"], condition=models.Q(done_at__isnull=True),
                                               name="brain_one_open_rederive")]


class RevocationNotice(models.Model):
    """What a learner that holds something derived from `source` is told when it goes: the source is
    removed, so what came from it is to be dropped. One per (learner, source); served to the learner's next
    run with its first `brain_read` (`reading.read_next`, `revoked`), which sets `delivered_at`."""

    capability_slug = models.CharField(max_length=64)
    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="revocations")
    reason = models.CharField(max_length=40, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["capability_slug", "source"], name="brain_one_revocation")]


class DisclosureDay(models.Model):
    """A person's disclosure summary, one row per (person, learner, day): how many times what they said was
    read and how many of their sources it covered. Compacted from the raw `brain.read` events
    (`disclosure.compact`) and recomputable from them; ids and counts only, never a word."""

    person = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    capability_slug = models.CharField(max_length=64)
    day = models.DateField()
    reads = models.PositiveIntegerField(default=0)
    sources = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["person", "capability_slug", "day"], name="brain_disclosure_one")]


class WorkspaceLearningReads(models.Model):
    """"Learning reads" for one workspace: whether learners may read its sessions at all. No row means unset, which
    inherits from the nearest ancestor that sets it, and off at the root (`learning_reads.effective`). Set on the
    workspace's brain page by its owners and admins (or an ancestor's), each change audited
    (`api_admin.set_learning_reads`)."""

    workspace = models.OneToOneField("workspaces.Workspace", on_delete=models.CASCADE, primary_key=True,
                                     related_name="learning_reads")
    on = models.BooleanField()
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class BrainRunArtifact(models.Model):
    """Something a learning run's turn made in canopy-web, or reported making outside it (a PR, an issue, a link).
    Recorded by the server from the turn's own credential (`lineage`), never by the skill, with `OutputLineage`
    (`output_kind` `brain.artifact`) to every source the run had read when it was made. When a removal reaches one of
    those sources, the cascade WITHDRAWS what canopy can undo (a task, a draft) and FLAGS what it cannot (a sent email, a
    pull request): flagged, it is listed on its workspace's brain page and raised to its owner (the person the run
    answered to) until they acknowledge it (`alerts_removal`). Ids and a link only, never a word of what it says."""

    LIVE, WITHDRAWN, FLAGGED = "live", "withdrawn", "flagged"
    STATE_CHOICES = [(LIVE, "Live"), (WITHDRAWN, "Withdrawn"), (FLAGGED, "Flagged")]

    run = models.ForeignKey(BrainRun, on_delete=models.CASCADE, related_name="artifacts")
    #: `<app_label>.<model>` for a row canopy-web holds, else `link` (`github.pull`, `github.issue` for those).
    kind = models.CharField(max_length=80)
    #: The row's pk, or the URL.
    ref = models.CharField(max_length=500)
    #: Where it shows: the row's own workspace when it has one, else the run's.
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    #: Whom a flag is raised to: the person the run answered to.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+")
    state = models.CharField(max_length=10, choices=STATE_CHOICES, default=LIVE)
    #: Why it was withdrawn or flagged (`cascade.removed_reason`), in words.
    reason = models.CharField(max_length=200, blank=True, default="")
    changed_at = models.DateTimeField(null=True, blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                        related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "kind", "ref"], name="brain_artifact_one")]
        indexes = [models.Index(fields=["workspace", "state"], name="brain_artifact_by_ws")]
