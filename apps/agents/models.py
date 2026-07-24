"""First-class AI-agent workspace.

An `Agent` is a first-class entity (e.g. "Echo," a marketing agent) — distinct
from a code Project. Where a Project's work shows up as static markdown
shareouts, an Agent's periodic sync is a **Google Doc** (`AgentSync.doc_url`),
because an agent's sync spans both code/skill improvement AND work products and
is richer than a feed card. canopy-web stores the metadata, summary, and
self-grades for the feed; the body lives in the doc.
"""
from django.conf import settings
from django.db import models


class Agent(models.Model):
    """A first-class AI agent that publishes its work into canopy-web."""

    slug = models.SlugField(unique=True, max_length=80)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    persona = models.TextField(blank=True, default="", help_text="Who the agent is / its remit.")
    email = models.EmailField(blank=True, default="", help_text="The agent's own mailbox, if any.")
    avatar_url = models.URLField(blank=True, default="", max_length=500)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="agents",
        help_text="The human who operates the agent.",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="agents",
        help_text="The tenant that owns this agent. Nullable for migration "
        "safety; the API always assigns one (default workspace when unspecified).",
    )
    # --- Runtime registry (agent-runtime-registry design) ----------------
    # canopy-web is the entry point a runner asks "how do I run agent X?": it holds
    # the POINTER to the agent's repo (whose runtime.yaml is the declarative WHAT),
    # the secret REFERENCES (names, never values — resolved from 1Password on a
    # laptop / Secrets Manager on a cloud box), and the engine preference. The
    # declarative spec itself lives in the repo; secret values live in the store.
    EMDASH, CLOUD_P, ANY_ENGINE = "emdash", "cloud_p", "any"
    ENGINE_CHOICES = [(EMDASH, "emdash"), (CLOUD_P, "cloud-p (claude -p)"), (ANY_ENGINE, "any")]
    repo_url = models.CharField(
        max_length=300, blank=True, default="", help_text="Agent's git repo (holds runtime.yaml).",
    )
    repo_ref = models.CharField(max_length=120, blank=True, default="main")
    runtime_engine = models.CharField(max_length=10, choices=ENGINE_CHOICES, default=ANY_ENGINE)
    runtime_secrets = models.JSONField(
        default=list, blank=True,
        help_text="Secret-reference NAMES this agent needs (never values), e.g. "
        '["canopy-pat","echo-gog"]. The reconciler resolves each from the env store.',
    )
    # Ordered runner-KIND preference, e.g. ["cloud", "emdash"]. As of the
    # directed-runner-routing cascade (spec 2026-07-24), agent-turn claim
    # eligibility is decided by RunnerAssignment rank instead — this field is no
    # longer read by harness.services.claim_next_turn. Retained for the UI/API
    # surfaces that still display/edit it (see frontend runnerPriority.ts).
    runner_preference = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"agent:{self.slug}"


class AgentSync(models.Model):
    """A periodic manager sync — a Google Doc covering code/skill improvement AND
    work products. Body lives in `doc_url`; canopy-web keeps the summary +
    self-grades for the feed. Idempotent per (agent, period_start, period_end,
    source): re-posting the same window from the same source replaces it."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="syncs")
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    title = models.CharField(max_length=200)
    summary = models.TextField(blank=True, default="")
    doc_url = models.URLField(max_length=500, help_text="The Google Doc holding the full sync.")
    # Structured self-grades, e.g. {"work": "C+", "skills": "B-"}.
    self_grades = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_end", "-created_at"]
        indexes = [models.Index(fields=["agent", "-period_end"])]

    def __str__(self):
        return f"sync:{self.agent.slug}:{self.period_start}..{self.period_end}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class AgentWorkProduct(models.Model):
    """A deliverable the agent produced — a gdoc, form, story, etc. Idempotent
    per (agent, url, source)."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="work_products")
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=40, blank=True, default="", help_text="doc / form / story / …")
    url = models.URLField(max_length=500)
    description = models.TextField(blank=True, default="")
    tags = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "url"], name="uniq_agent_workproduct_url"),
        ]

    def __str__(self):
        return f"work:{self.agent.slug}:{self.title}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class AgentTurn(models.Model):
    """A packaged unit of work: one turn the agent ran, tied to the request(s)
    it advanced (`task_ext_ids`), what it did (`summary`), the deliverables it
    produced (`work_product_urls`), and — optionally — a link to the reduced
    session transcript (`session_slug` + `share_token`, rendered at
    `/share/<token>`). The transcript is one artifact of the turn, not the point:
    a turn can be packaged with no upload at all.

    Idempotent per (agent, cli_session_id): one turn per Claude session, so
    re-packaging the same session (e.g. after the transcript is uploaded) updates
    the record in place rather than duplicating it. The uploaded `Session` (in the
    sessions app) is owned by the human whose PAT uploaded it; this row only holds
    its slug/token, so the two apps stay decoupled."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="turns")
    cli_session_id = models.CharField(max_length=100, help_text="Claude Code session id — the dedup key.")
    title = models.CharField(max_length=300, help_text="What the turn did, in one line.")
    summary = models.TextField(blank=True, default="", help_text="The close-out summary.")
    # The request(s) this turn advanced — AgentTask.ext_id values (loose refs, not FKs).
    task_ext_ids = models.JSONField(default=list, blank=True)
    # Deliverables produced this turn — AgentWorkProduct urls (loose refs).
    work_product_urls = models.JSONField(default=list, blank=True)
    # Optional transcript link (empty when the turn was packaged without upload).
    session_slug = models.CharField(max_length=64, blank=True, default="", help_text="Uploaded Session.slug, if any.")
    share_token = models.CharField(max_length=64, blank=True, default="", help_text="Public /share/<token>, if shared.")
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "cli_session_id"], name="uniq_agent_turn_session"),
        ]
        indexes = [models.Index(fields=["agent", "-created_at"])]

    def __str__(self):
        return f"turn:{self.agent.slug}:{self.cli_session_id}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class AgentSkill(models.Model):
    """An entry in the agent's skill catalog: what the skill does, a link to its
    definition (SKILL.md), and the latest improvement note. The catalog is
    replaced wholesale on each publish (PUT) so it always mirrors the repo."""

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="skills")
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    url = models.URLField(max_length=500, blank=True, default="", help_text="Link to the SKILL.md.")
    improvement_note = models.TextField(blank=True, default="")
    # Which skills are human entry points. The catalog mirrors the repo, and most
    # of what a mature agent publishes is not launchable: a pre-send discipline
    # another skill invokes, a superseded skill, a reference doc, a one-time
    # per-machine setup. Rendering the whole catalog as phone buttons puts
    # `/echo:setup` one thumb away. Defaults False so an agent that has not
    # adopted the field publishes nothing launchable — fail closed.
    launchable = models.BooleanField(
        default=False, help_text="Offer this skill as a command on the phone composer."
    )
    args_hint = models.CharField(
        max_length=120, blank=True, default="",
        help_text='What the command takes, e.g. "topic (optional)". Renders as the input placeholder.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "name"], name="uniq_agent_skill"),
        ]

    def __str__(self):
        return f"skill:{self.agent.slug}:{self.name}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class AgentTask(models.Model):
    """A task in the agent's tracker. Source of truth is a Google Sheet the
    agent maintains; Echo syncs rows here so canopy-web can render a board.
    `status` is normalized to one of the board columns. Synced wholesale per
    agent, keyed by `ext_id` (stable sheet-row id) so edits/removals
    propagate."""

    # Suggested = the agent proposed it; a human must validate (Linear "Triage").
    # No "To do" (the agent would just do it) and no "Blocked" column — "waiting
    # on a person" is expressed by `assigned` being a human, shown as an overlay.
    SUGGESTED, IN_PROGRESS, DONE, DECLINED = "suggested", "in_progress", "done", "declined"
    STATUS_CHOICES = [
        (SUGGESTED, "Suggested"),
        (IN_PROGRESS, "In progress"),
        (DONE, "Done"),
        (DECLINED, "Declined"),
    ]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="tasks")
    ext_id = models.CharField(max_length=64, help_text="Stable id from the source sheet.")
    # A task can mean "execute this run" — the run-lifecycle backing for the
    # board (spec §5). String FK ref ("agent_runs.AgentRun") so this framework
    # app doesn't import apps.agent_runs at module load (both are framework and
    # agent_runs imports apps.agents.models — a hard import here would cycle).
    # SET_NULL: deleting a run leaves the task, just unlinked.
    run = models.ForeignKey(
        "agent_runs.AgentRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tasks",
        help_text="The run this task executes, if the task is run-backed.",
    )
    title = models.CharField(max_length=300, help_text="The outcome.")
    next_action = models.CharField(max_length=300, blank=True, default="", help_text="The single concrete next step (verb-first).")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=SUGGESTED)
    owner = models.CharField(max_length=120, blank=True, default="", help_text="Human stakeholder who owns the outcome — never the agent.")
    assigned = models.CharField(max_length=120, blank=True, default="", help_text="Who the next action waits on — the agent or a person.")
    confidence = models.CharField(max_length=10, blank=True, default="", help_text="high / low — how sure the agent is about a suggestion.")
    # The agent's self-assessment, captured WHEN the task is marked done — so a
    # manager sync reads the score from completion time instead of re-grading
    # weeks later. `score` is a short grade ("A-", "B+", "4/5"); `review` is the
    # one-line why. Blank until the task is completed.
    score = models.CharField(max_length=8, blank=True, default="", help_text="Self-grade at completion — e.g. A-, B+, 4/5.")
    review = models.TextField(blank=True, default="", help_text="One-line self-review captured when the task was completed.")
    # Context the agent stores when it suggests a task, so it doesn't re-derive
    # from email when a human says "go do it."
    rationale = models.TextField(blank=True, default="", help_text="Why the agent suggested this / why now.")
    source_url = models.URLField(max_length=500, blank=True, default="", help_text="Originating thread / report / link.")
    plan = models.TextField(blank=True, default="", help_text="Proposed first steps.")
    due = models.DateField(null=True, blank=True)
    links = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True, default="")
    position = models.IntegerField(default=0, help_text="Order within a status column.")
    source = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["status", "position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["agent", "ext_id"], name="uniq_agent_task_extid"),
        ]
        indexes = [models.Index(fields=["agent", "status"])]

    def __str__(self):
        return f"task:{self.agent.slug}:{self.ext_id}:{self.status}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug


class AgentTaskCommand(models.Model):
    """A human action from the board that the agent drains on its next turn.

    Some kinds apply immediately server-side (decline/reassign/edit/done) AND/OR
    queue agent work (accept/dispatch). The agent reads `status=pending`, does the
    work under its normal guardrails, then marks the command applied."""

    ACCEPT, DECLINE, DISPATCH, REASSIGN, EDIT, COMMENT, DONE = (
        "accept", "decline", "dispatch", "reassign", "edit", "comment", "done")
    KIND_CHOICES = [(k, k) for k in (ACCEPT, DECLINE, DISPATCH, REASSIGN, EDIT, COMMENT, DONE)]
    PENDING, APPLIED, DISMISSED = "pending", "applied", "dismissed"
    STATUS_CHOICES = [(PENDING, "Pending"), (APPLIED, "Applied"), (DISMISSED, "Dismissed")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="commands")
    task = models.ForeignKey(AgentTask, on_delete=models.SET_NULL, null=True, blank=True, related_name="commands")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    payload = models.JSONField(default=dict, blank=True, help_text="reason / assignee / next_action / note …")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=PENDING)
    created_by = models.CharField(max_length=200, blank=True, default="", help_text="Who clicked it (email).")
    result_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["agent", "status"])]

    def __str__(self):
        return f"cmd:{self.agent.slug}:{self.kind}:{self.status}"

    @property
    def agent_slug(self) -> str:
        return self.agent.slug

    @property
    def task_ext_id(self) -> str:
        return self.task.ext_id if self.task_id else ""

    @property
    def task_title(self) -> str:
        return self.task.title if self.task_id else ""
