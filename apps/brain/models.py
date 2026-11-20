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

#: What a person can agree canopy learns from their sessions for. The class names are
#: the contract: a capability declares one, a consent lists some.
INDIVIDUAL, MEMORY, PROJECT, SYSTEM = "individual", "memory", "project", "system"
CLASSES = (INDIVIDUAL, MEMORY, PROJECT, SYSTEM)
#: Classes that learn about ONE PERSON from their own words (only that person's consent
#: counts) versus the ones that learn about a project or canopy from everyone's (every
#: human participant must agree).
ABOUT_A_PERSON = frozenset({INDIVIDUAL, MEMORY})
SHARED = frozenset({PROJECT, SYSTEM})


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

    kind = models.CharField(max_length=24, choices=KIND_CHOICES, default=SESSION)
    #: The source's id within its kind (a session's uuid). The key; `session` is the
    #: typed pointer for the one kind that exists.
    ref = models.CharField(max_length=64)
    session = models.ForeignKey("canopy_sessions.Session", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="brain_sources")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="+")
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default=OPEN)
    sealed_reason = models.CharField(max_length=16, blank=True, default="")
    #: When the associator should look at this source next (`associate.run_due`): set by an
    #: activity fold, so a burst of folds is one look; null once looked at.
    associate_due_at = models.DateTimeField(null=True, blank=True)
    #: When it was last looked at; null means never, which is also due.
    associated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "ref"], name="brain_source_one_per_ref")]


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

    Only the person, as themselves, makes or changes it (`consent.py`, `api.py`): never an
    admin, an agent, a turn's token or a runner. `CoachingOptIn` is carried over as the
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
    restores nothing the cascade already withdrew (P5)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="exclusions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "source"], name="brain_exclusion_one")]


class BrainCapability(models.Model):
    """Something that learns: registered by a canopy admin (P4's routes), in one purpose
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
    schedule = models.CharField(max_length=64, blank=True, default="")
    enabled = models.BooleanField(default=False)
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
    #: The project's own PRs, `{"owner/name#12": {"cross_cutting": false}}`. A cross-cutting one is weak.
    prs = models.JSONField(default=dict, blank=True)
    mcp_min = models.PositiveSmallIntegerField(default=3)
    seeded_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class SourceLink(models.Model):
    """A source's tie to a subject. Metadata only: a link confers no read by itself, and only a `core`
    link ever opens the project read leg (P4). One row per (source, subject); the strongest layer
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["subject_type", "subject", "state"], name="brain_output_by_subject")]


class OutputLineage(models.Model):
    """One output's one source: written by `brain_emit` from the turn's ACTUAL reads, so the cascade
    knows what to withdraw. `output_kind` names the store (`brain.output` now; an existing store later
    carries its own kind). `participant_scope` is the people whose words were in the source at the time:
    the consents the output carries."""

    output_kind = models.CharField(max_length=40, default="brain.output")
    output_id = models.CharField(max_length=64)
    source = models.ForeignKey(BrainSource, on_delete=models.CASCADE, related_name="lineage")
    participant_scope = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["output_kind", "output_id", "source"],
                                               name="brain_lineage_one")]
        indexes = [models.Index(fields=["output_kind", "output_id"], name="brain_lineage_by_output")]
