"""Agent models: work."""

from django.conf import settings
from django.db import models
from django.db.models.functions import Lower

from .agent import Agent


class AgentProject(models.Model):
    """A project: a piece of work with an end, owned by a workspace.

    **The workspace owns it** (`workspace`), and it is addressed there by its
    `handle`: `dimagi/unga-2026`, page `/w/dimagi/projects/unga-2026`. Any agent
    of the workspace (or of one above it) may file tasks into it; the agents
    working on it are its `member_agents`, and the person accountable is its
    lead (`owner_user`).

    **Its lead agent (`agent`) holds its files.** The layout in
    `agent-core/deliverables.md` is per agent: one Drive root per agent,
    `Projects/<name>/` beneath it. That is a good home for FILES and a poor one
    for state, so the folder stays in the lead agent's Drive and this row points
    AT it (`drive_folder_*`). A project nobody's agent leads has no folder yet.
    The lead agent may belong to the workspace or one above it (an org's agent
    leading a division's project: Eva, of dimagi, leads `connect/givewell-kb`).

    **`ext_id` is an agent's alias** ("P5"): projects were once numbered per
    agent, and the fleet still names them that way through
    `/api/agents/{slug}/projects/P5/`. The alias belongs to the agent that
    numbered it (`created_by_agent`) and survives a change of lead. A project
    made outside an agent's route has none.

    This is canopy's one project system. A project names a repo it touches only
    as a slug (`repo_slug`); there is no repo registry to point at.
    """

    #: `proposed` — an agent proposed it (from a huddle, a chat, its own turn) and
    #: nobody has started or declined it yet; its decision card ("Start this
    #: project?") is how a person does either (`apps/agents/proposals.py`).
    PROPOSED, PLANNED, ACTIVE, PAUSED = "proposed", "planned", "active", "paused"
    DONE, CANCELED, ARCHIVED = "done", "canceled", "archived"
    STATUS_CHOICES = [(PROPOSED, "Proposed"), (PLANNED, "Planned"), (ACTIVE, "Active"),
                      (PAUSED, "Paused"), (DONE, "Done"), (CANCELED, "Canceled"),
                      (ARCHIVED, "Archived")]
    #: Status -> category, the only thing progress, health and lists read.
    CATEGORY = {PROPOSED: "triage", PLANNED: "unstarted", ACTIVE: "started", PAUSED: "paused",
                DONE: "completed", CANCELED: "canceled", ARCHIVED: "canceled"}
    #: Statuses a project is still open in, in the order a list shows them.
    OPEN_STATUSES = (ACTIVE, PAUSED, PLANNED, PROPOSED)
    HANDLE_RE = r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$"

    #: The workspace that owns it. The lead agent's workspace or one below it
    #: (`services.may_hold_workspace_project`).
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="projects")
    #: Its address in its workspace ("givewell-kb"), lower case.
    handle = models.CharField(max_length=60)
    #: The LEAD AGENT: keeps it current and holds its Drive folder. Null for a
    #: project no agent leads.
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="projects")
    #: The agents working on it. The lead agent is always one (`services`).
    member_agents = models.ManyToManyField(Agent, blank=True, related_name="member_projects")
    #: The agent whose `P<n>` alias `ext_id` is.
    created_by_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="filed_projects")
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="filed_projects")
    #: `created_by_agent`'s alias for it ("P5"), unique per that agent; blank for
    #: a project made outside an agent's route.
    ext_id = models.CharField(max_length=64, blank=True, default="",
                              help_text="An agent's alias (P1, P2 …), unique per that agent.")
    name = models.CharField(max_length=200, help_text="What the project is called — and its Drive folder's name.")
    outcome = models.TextField(blank=True, default="", help_text="What done looks like.")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=ACTIVE)
    #: 0 none, 1 urgent, 2 high, 3 medium, 4 low — the same scale as a task's.
    priority = models.PositiveSmallIntegerField(default=0)
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField(null=True, blank=True)

    #: The LEAD: the person accountable for the outcome. A real person where
    #: canopy knows them, so "waiting on you" can reach someone; free text
    #: (`owner_note`) otherwise.
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="agent_projects",
    )
    owner_note = models.CharField(max_length=200, blank=True, default="")
    #: Its lead agent may publish its own drafted updates (`ProjectUpdate`)
    #: instead of waiting for a person. Off by default; a workspace admin turns
    #: it on (`projects.decide`, audited as a setting).
    lead_agent_may_publish = models.BooleanField(default=False)

    #: The agent's own `Projects/<name>` folder. The id is what `gog drive` needs;
    #: the url is what a human clicks. Blank until the agent files something.
    drive_folder_id = models.CharField(max_length=128, blank=True, default="")
    drive_folder_url = models.URLField(max_length=500, blank=True, default="")

    #: The repo this work touches, as a slug, if any — the same bare repo name
    #: `Turn.project` carries; there is no repo registry to FK.
    repo_slug = models.CharField(max_length=100, blank=True, default="")

    notes = models.TextField(blank=True, default="")
    links = models.JSONField(default=list, blank=True)
    #: Where its knowledge base's objects are stored (`kb_store.root`): fixed for good,
    #: so a new handle or workspace moves nothing (#2047). Blank: `kb-projects/<pk>/`.
    #: A project made before then keeps the address-keyed root it stored under.
    storage_root = models.CharField(max_length=200, blank=True, default="")
    #: The page that proposed this project — a huddle, a thread — the same idea
    #: as `AgentTask.source_url`. Indexed: a huddle page lists the projects whose
    #: `source_url` is that page.
    source_url = models.URLField(max_length=500, blank=True, default="", db_index=True)
    #: When it was last proposed (or revised). A proposed project nobody starts or
    #: declines within `proposals.EXPIRES_AFTER` of this is archived, not taken up.
    proposed_at = models.DateTimeField(null=True, blank=True)
    #: The task this project grew out of — a check-in's proposed task that turned
    #: out to be a project's worth of work (`projects/propose/ from_task`). The
    #: task shows "spawned → this project"; this one shows "from <task> in <project>".
    spawned_from = models.ForeignKey("agents.AgentTask", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="spawned_projects")
    #: The PEOPLE this work involves (fleet brain v1.1, canopy#804) — not the
    #: owner (one human who owns the outcome) but everyone canopy has seen take
    #: part: the subject of a fact filed against the project, or the human whose
    #: conversation raised one of its tasks. Filled automatically
    #: (`apps/agents/participants.py`); see `ProjectParticipant`.
    participants = models.ManyToManyField(
        "contacts.Person", through="agents.ProjectParticipant", related_name="agent_projects",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["created_by_agent", "ext_id"], condition=~models.Q(ext_id=""),
                                    name="project_agent_alias_unique"),
            models.UniqueConstraint(fields=["workspace", "handle"], name="project_handle_unique"),
            models.CheckConstraint(condition=~models.Q(handle=""), name="project_has_handle"),
        ]
        indexes = [models.Index(fields=["agent", "status"]), models.Index(fields=["workspace", "status"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}/{self.handle} {self.name}"

    def save(self, *args, **kwargs):
        """Fill what a row may not lack, whoever builds it: its workspace (its
        lead agent's), a handle (free, from its name), and — carrying an alias —
        the agent the alias is of. Its lead agent is always a member."""
        creating = self.pk is None
        if not self.workspace_id and self.agent_id:
            self.workspace_id = self.agent.workspace_id
        if not self.handle:
            from ..services import free_handle  # noqa: PLC0415 — services imports models

            self.handle = free_handle(self.workspace_id, self.name, fallback=self.ext_id or "project")
        if self.ext_id and not self.created_by_agent_id and creating:
            self.created_by_agent_id = self.agent_id
        super().save(*args, **kwargs)
        if creating and self.agent_id:
            self.member_agents.add(self.agent_id)

    # Schemas here are plain pydantic (`StrictModel`), not ninja `Schema`, so a
    # `resolve_*` on the schema is ignored — the models carry these instead, the
    # way `AgentTask.agent_slug` already does.
    @property
    def agent_slug(self) -> str | None:
        """Its lead agent's slug, or None when no agent leads it."""
        return self.agent.slug if self.agent_id else None

    @property
    def steward_slug(self) -> str | None:
        return self.agent_slug

    @property
    def shared_with(self) -> list[str]:
        """The workspaces it is shared with (`ProjectShare`), sorted."""
        if self.pk is None:
            return []
        return sorted(sh.workspace_id for sh in self.shares.all())

    @property
    def shared_in(self) -> bool:
        """True on a row a list shows only because it is shared with the
        reader (`services.mark_shared_in`); never stored."""
        return getattr(self, "_shared_in", False)

    @property
    def category(self) -> str:
        return self.CATEGORY.get(self.status, "started")

    @property
    def member_slugs(self) -> list[str]:
        if hasattr(self, "_member_slugs"):
            return self._member_slugs
        if self.pk is None:
            return []
        return sorted(a.slug for a in self.member_agents.all())

    # Health and progress: a list route computes them for every row at once
    # (`work.annotate_projects`) and caches them here; a single project reads
    # its own.
    @property
    def health(self) -> str | None:
        if hasattr(self, "_health"):
            return self._health
        from ..work import latest_update  # cycle: agents.work -> agents.models  # noqa: PLC0415

        latest = latest_update(self)
        return latest.health if latest else None

    @property
    def update_missing(self) -> bool:
        if hasattr(self, "_update_missing"):
            return self._update_missing
        from ..work import update_missing  # cycle: agents.work -> agents.models  # noqa: PLC0415

        return update_missing(self)

    @property
    def done_task_count(self) -> int:
        cached = getattr(self, "_done_task_count", None)
        return self.tasks.filter(status=AgentTask.DONE).count() if cached is None else cached

    @property
    def address(self) -> str:
        """How every caller names it: `<workspace>/<handle>`."""
        return f"{self.workspace_id}/{self.handle}"

    @property
    def url(self) -> str:
        """Its one page."""
        return f"/w/{self.workspace_id}/projects/{self.handle}"

    # ---- what it serves (`ProjectService`) --------------------------------
    # A knowledge base is the one kind served from canopy itself; these read it
    # for the project's list shape (`AgentProjectOut`), so a page needs no second
    # fetch to show "serves a knowledge base".

    @property
    def knowledge_base(self) -> "ProjectService | None":
        """Its knowledge base, when it serves one (read once per instance)."""
        if not hasattr(self, "_knowledge_base"):
            cached = getattr(self, "_prefetched_objects_cache", {}).get("served")
            rows = cached if cached is not None else self.served.filter(kind=ProjectService.KB)
            self._knowledge_base = next((r for r in rows if r.kind == ProjectService.KB), None)
        return self._knowledge_base

    @property
    def mcp_kind(self) -> str:
        kb = self.knowledge_base
        return ProjectService.KB if kb is not None and kb.kb_key else ""

    @property
    def kb_published_at(self):
        kb = self.knowledge_base
        return kb.kb_published_at if kb else None

    @property
    def kb_version_number(self) -> int | None:
        kb = self.knowledge_base
        return kb.kb_version.number if kb and kb.kb_version_id else None

    @property
    def kb_drive_replica(self) -> bool:
        kb = self.knowledge_base
        return bool(kb and kb.kb_drive_replica)

    @property
    def mcp_path(self) -> str:
        """Where its own MCP server answers — a project serving a published
        knowledge base, else "". Under `/api/mcp/` so the same sign-in (a personal
        token, or OAuth) reaches it: one server per project, so a reader adds just
        this one and not the whole canopy surface."""
        if self.mcp_kind:
            return f"/api/mcp/projects/{self.workspace_id}/{self.handle}/"
        return ""

    @property
    def owner_email(self) -> str | None:
        return self.owner_user.email if self.owner_user_id else None

    @property
    def spawned_from_ref(self) -> dict | None:
        """The task this project grew out of, as the page links it (None for most)."""
        if not self.spawned_from_id:
            return None
        t = self.spawned_from
        return {"agent": t.agent_slug or "", "ref": t.ref, "key": t.key, "title": t.title,
                "status": t.status,
                "project_ref": (t.project.ext_id or t.project.handle) if t.project_id else "",
                "project_name": t.project.name if t.project_id else "",
                "url": t.url}

    @property
    def task_count(self) -> int:
        # The list route counts every project in one query and caches it here;
        # a single fetched project counts its own.
        cached = getattr(self, "_task_count", None)
        return self.tasks.count() if cached is None else cached

    @property
    def open_task_count(self) -> int:
        cached = getattr(self, "_open_task_count", None)
        if cached is not None:
            return cached
        return self.tasks.filter(status__in=AgentTask.LIVE_STATUSES).count()

    @property
    def waiting_task_count(self) -> int:
        """Live tasks somebody has to act on (`services.waiting_q`) — an open
        ask or a task parked on a person. Not per viewer: the Projects page
        asks "how much of this is stuck on people", not "on me"."""
        cached = getattr(self, "_waiting_task_count", None)
        if cached is not None:
            return cached
        from apps.agents.services import waiting_q  # cycle: agents.services -> agents.models  # noqa: PLC0415

        return self.tasks.filter(waiting_q()).count()


class ProjectShare(models.Model):
    """A project shared with a workspace of its own tree: the project is open to
    that workspace's members and agents, and to those of the workspaces below it
    (`services.may_join_workspace_project`, `services.project_reader_slugs`).

    The shared workspace is in the project's own organisation (same tree root)
    and neither the project's own workspace nor one above it, which already
    reach it; nothing is ever shared across organisations
    (`services.share_project`). A share makes the project readable there and
    lets that side's agents join it as members; it does not let them lead it.
    """

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="shares")
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="shared_in_projects")
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["workspace_id"]
        constraints = [models.UniqueConstraint(fields=["project", "workspace"],
                                               name="project_share_unique")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id} shared with {self.workspace_id}"


class ProjectService(models.Model):
    """Something a project serves to its workspace (design 2026-10-24, "What a
    project serves"). A project is the workspace's and spans its agents and
    people, so it is where what they share hangs: its people may use it, its
    lead agent (or an editor) keeps it current, and the workspace directory
    (`GET /api/mcp-servers/`) lists it.

    Kinds:
      * `kb` — a knowledge base canopy serves as the project's own MCP server
        (`/api/mcp/projects/<ws>/<handle>/`), published by its lead agent's
        curator (`kb_store`, `kb_versions`). One per project; every `kb_*` field.
      * `mcp` — a remote MCP server the workspace shares: `url`, and how to add it.
      * `skill` — a Claude Code skill: its `instructions`, written here or
        published by the lead agent (`url`: where it came from, optional).
      * `plugin` — a Claude Code plugin (`url`: its repo) for the workspace.
    The projects connection (`apps/mcp/project_gateway.py`) serves every project's
    through one fixed set of tools, so nothing here reaches a session until the
    work asks for it. canopy serves `kb` and `skill`; an `mcp` or `plugin` is
    declared and listed, with how to add it.
    """

    KB, MCP, SKILL, PLUGIN = "kb", "mcp", "skill", "plugin"
    KINDS = (KB, MCP, SKILL, PLUGIN)
    KIND_CHOICES = [(KB, "Knowledge base"), (MCP, "MCP server"), (SKILL, "Skill"), (PLUGIN, "Plugin")]

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="served")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    #: Its name in the project ("kb" for the knowledge base; a server's, skill's
    #: or plugin's own name otherwise), unique per kind.
    name = models.CharField(max_length=80)
    description = models.TextField(blank=True, default="")
    #: An MCP server's URL, a skill's or plugin's repository URL. Blank for a KB.
    url = models.URLField(max_length=500, blank=True, default="")
    #: A skill's instructions (its SKILL.md body): what an agent reads when it
    #: uses the skill. Blank for the other kinds.
    instructions = models.TextField(blank=True, default="")
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ---- a knowledge base (kind `kb`) ------------------------------------------
    #: The published kb.sqlite.gz: its object key in the KB bucket, content hash
    #: (what a serving instance caches by) and size. Set by `kb_store.publish`.
    kb_key = models.CharField(max_length=300, blank=True, default="")
    kb_sha256 = models.CharField(max_length=64, blank=True, default="")
    kb_bytes = models.BigIntegerField(null=True, blank=True)
    kb_published_at = models.DateTimeField(null=True, blank=True)
    #: The head of its version log (`KbVersion`): the version `kb_key` serves. Null for
    #: a KB published before versions existed.
    kb_version = models.ForeignKey("agents.KbVersion", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    #: Off by default. On: after each version, canopy-web writes its kb.sqlite.gz into
    #: the project's Drive folder (`kb_versions`); no reader needs it — readers call the MCP.
    kb_drive_replica = models.BooleanField(default=False)
    kb_drive_replica_file_id = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["project", "kind", "name"], name="uniq_project_service"),
            models.CheckConstraint(condition=models.Q(kind__in=["kb", "mcp", "skill", "plugin"]),
                                   name="project_service_kind_known"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}:{self.kind}:{self.name}"

    @property
    def kb_version_number(self) -> int | None:
        return self.kb_version.number if self.kb_version_id else None


class ProjectParticipant(models.Model):
    """A person taking part in an agent's project (fleet brain v1.1, canopy#804).

    One row per (project, person). `source` says how canopy learned it, and the
    FIRST answer is kept: a later fact or turn about someone already linked
    changes nothing, so `source` reads "how did they first come in".

    Same tenancy as everything else about a person: a project belongs to one
    agent, which belongs to one workspace, so a participant row is only ever
    served to members of that workspace (`people.known_in` gates the person
    side of it).
    """

    FACT, TURN, MANUAL, SUMMARY, REVIEW = "fact", "turn", "manual", "summary", "review"
    BACKING, REPLY, CHECKIN = "backing", "reply", "checkin"
    SOURCE_CHOICES = [
        (FACT, "A fact about them was filed against the project"),
        (TURN, "A conversation with them raised one of its tasks"),
        (MANUAL, "Added by a person"),
        (SUMMARY, "An agent wrote their contributor page from their own words"),
        (REVIEW, "They commented in a feedback round on one of its storyboards"),
        (BACKING, "They backed (or passed on) the project"),
        (REPLY, "They replied on one of its tasks"),
        (CHECKIN, "They take part in one of its check-ins"),
    ]
    ROLE_MAX = 80

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE,
                                related_name="participant_rows")
    person = models.ForeignKey("contacts.Person", on_delete=models.CASCADE,
                               related_name="project_participations")
    #: Free text, e.g. "program lead", "LLO contact". Blank when canopy filled
    #: the row itself and nobody has said what part they play.
    role = models.CharField(max_length=ROLE_MAX, blank=True, default="")
    source = models.CharField(max_length=8, choices=SOURCE_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    #: Their CONTRIBUTOR PAGE (hal T69): what they are trying to achieve on this
    #: project, in their own words, with a link to where they said each thing,
    #: and which of the project's tasks are theirs. It is JUDGMENT — which of
    #: someone's 400 prompts say what they want — so an agent writes it
    #: (`contributors.write_summary`, `PUT …/contributors/{person}/summary/`) and
    #: the page only renders it. Per project because what someone wants from
    #: Supply says nothing about what they want from anything else.
    #:
    #: It is something an agent LEARNED about a person, so it follows the
    #: person's own switches: written only while they let agents learn about
    #: them (record) and this agent holds their grant; read by an agent only
    #: while they let agents use it (use). Empty dict = never written.
    summary = models.JSONField(default=dict, blank=True)
    summary_written_at = models.DateTimeField(null=True, blank=True)
    summary_by_agent = models.ForeignKey("agents.Agent", on_delete=models.SET_NULL, null=True,
                                         blank=True, related_name="contributor_summaries")

    class Meta:
        db_table = "agents_project_participant"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["project", "person"], name="one_participant_row_per_person"),
            models.CheckConstraint(condition=models.Q(source__in=["fact", "turn", "manual", "summary", "review",
                                                                  "backing", "reply", "checkin"]),
                                   name="project_participant_source_known_v5"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}<-{self.person_id} ({self.source})"


class ProjectBacking(models.Model):
    """Where one backer stands on a project — a person or an agent, never both.

    Backing is a SIGNAL, not a gate (decision 4(b), approved in
    `2026-10-16-work-group-huddles-design.md`): starting a proposed
    project is its decision card's, and no number of backers starts it. What
    this row buys is that "Echo backs · Eva backs with changes: … · Shayoni
    passes: …" is a row on the project, not a sentence in a huddle transcript.

    A row is on a project OR on a task (a check-in's proposed task, spec
    2026-10-17-project-check-ins-design.md §3), never both: one backing table.

    One row per (project, person) and one per (project, agent) — and the same per
    task: a backer changes
    their stance by rewriting their row. A later revision of the project marks
    every row `stale` — they backed the earlier version — and rewriting clears it.
    Written only through `apps/agents/proposals.back`.
    """

    BACKS, BACKS_WITH_CHANGES, PASSES = "backs", "backs_with_changes", "passes"
    STANCES = (BACKS, BACKS_WITH_CHANGES, PASSES)
    LINE_MAX = 280

    project = models.ForeignKey(AgentProject, null=True, blank=True, on_delete=models.CASCADE,
                                related_name="backings")
    task = models.ForeignKey("agents.AgentTask", null=True, blank=True, on_delete=models.CASCADE,
                             related_name="backings")
    person = models.ForeignKey("contacts.Person", null=True, blank=True, on_delete=models.CASCADE,
                               related_name="project_backings")
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="project_backings")
    stance = models.CharField(max_length=20, choices=[(s, s) for s in STANCES])
    #: One line, required unless the stance is plain `backs`: what to change, or why not.
    line = models.CharField(max_length=LINE_MAX, blank=True, default="")
    #: Where it was given: `web`, `mcp`, `slack`, `email`, or `huddle` (a co-sign
    #: `canopy huddle file` wrote from a member's round-3 answer).
    source = models.CharField(max_length=8, default="web")
    huddle_id = models.CharField(max_length=64, blank=True, default="")
    stale = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "agents_project_backing"
        ordering = ["created_at", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(person__isnull=False, agent__isnull=True)
                           | models.Q(person__isnull=True, agent__isnull=False)),
                name="project_backing_one_backer"),
            models.CheckConstraint(
                condition=(models.Q(project__isnull=False, task__isnull=True)
                           | models.Q(project__isnull=True, task__isnull=False)),
                name="project_backing_one_subject"),
            models.CheckConstraint(condition=models.Q(stance__in=["backs", "backs_with_changes", "passes"]),
                                   name="project_backing_stance_known"),
            models.UniqueConstraint(fields=["project", "person"], condition=models.Q(person__isnull=False),
                                    name="one_backing_per_person"),
            models.UniqueConstraint(fields=["project", "agent"], condition=models.Q(agent__isnull=False),
                                    name="one_backing_per_agent"),
            models.UniqueConstraint(fields=["task", "person"],
                                    condition=models.Q(person__isnull=False, task__isnull=False),
                                    name="one_task_backing_per_person"),
            models.UniqueConstraint(fields=["task", "agent"],
                                    condition=models.Q(agent__isnull=False, task__isnull=False),
                                    name="one_task_backing_per_agent"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id or self.task_id}<-{self.person_id or self.agent_id}: {self.stance}"


class AgentTask(models.Model):
    """A task: one step of work, owned by a workspace and addressed by its KEY
    (`DIM-142`) — the workspace's prefix (`TaskSettings`) and its number there.

    **Assigned to a person or an agent** (decision 2026-10-24), never both: the
    agent doing it (`agent`) or the person (`assignee_user`), or nobody yet. An
    agent assignee is who an approval starts a turn for; a person assignee is
    who "My tasks" lists it for.

    **`ext_id` is an agent's alias** ("T12"): tasks were once numbered per agent
    and the fleet still names them so, through `/api/agents/{slug}/tasks/T12/`
    and the canopy CLI (which numbers them on the client). The alias belongs to
    the agent that filed it (`created_by_agent`) and survives reassignment. A
    task filed anywhere else has none.

    `status` values are fixed; each maps to a category (`CATEGORY`), which is
    what progress, counts and agents read. A workspace may rename how each
    category is SHOWN (`TaskSettings.status_names`)."""

    # Suggested = the agent proposed it; a person decides (Linear "Triage").
    # Backlog = accepted, not now. Todo = accepted, next up. "Waiting on a person"
    # is not a status: it is `waiting_on_user` or an open ask, shown as an overlay.
    SUGGESTED, BACKLOG, TODO, IN_PROGRESS = "suggested", "backlog", "todo", "in_progress"
    DONE, DECLINED = "done", "declined"
    #: An OPEN task — the one definition every count of "open tasks" uses.
    LIVE_STATUSES = (SUGGESTED, BACKLOG, TODO, IN_PROGRESS)
    STATUS_CHOICES = [
        (SUGGESTED, "Triage"),
        (BACKLOG, "Backlog"),
        (TODO, "Todo"),
        (IN_PROGRESS, "In progress"),
        (DONE, "Done"),
        (DECLINED, "Canceled"),
    ]
    #: Status -> category (Linear's six). Code that judges progress reads these.
    CATEGORY = {SUGGESTED: "triage", BACKLOG: "backlog", TODO: "unstarted",
                IN_PROGRESS: "started", DONE: "completed", DECLINED: "canceled"}
    #: Status order a list or board shows them in.
    STATUS_ORDER = (SUGGESTED, IN_PROGRESS, TODO, BACKLOG, DONE, DECLINED)

    #: 0 none, 1 urgent, 2 high, 3 medium, 4 low (Linear's scale).
    PRIORITY_CHOICES = [(0, "No priority"), (1, "Urgent"), (2, "High"), (3, "Medium"), (4, "Low")]
    CONFIDENCE_VALUES = ("", "high", "medium", "low")

    #: The workspace that owns it.
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="tasks")
    #: Its number in its workspace; with the prefix, its `key`.
    number = models.PositiveIntegerField()
    #: `<PREFIX>-<number>`, frozen when it is made (a prefix never changes once
    #: used), so a lookup by key is one indexed read.
    key = models.CharField(max_length=24, unique=True)
    #: The ASSIGNEE AGENT — the agent doing the work. Null when a person is the
    #: assignee, or nobody is.
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="tasks")
    #: The ASSIGNEE PERSON. At most one of `agent` / `assignee_user` is set.
    assignee_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="assigned_tasks")
    #: Who filed it. `created_by_agent` also owns the `ext_id` alias.
    created_by_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                                         related_name="filed_tasks")
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="filed_tasks")
    ext_id = models.CharField(max_length=64, blank=True, default="",
                              help_text="An agent's alias (T1, T2 …), unique per the agent that filed it.")
    #: The project this task is part of. Optional: plenty of work is a one-off,
    #: and forcing a project on it would produce a project per task, which is
    #: what the Drive layout already warns against ("never dump flat in
    #: Projects/"). SET_NULL so closing a project does not delete its history.
    project = models.ForeignKey(
        "agents.AgentProject", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="tasks",
    )
    #: The task this one is a step of. Subtasks, not subprojects (T72,
    #: Jonathan 2026-10-09): a product area is ONE project board, and a big
    #: piece of work inside it breaks down into child tasks rather than a new
    #: project per piece. Same workspace and project as the parent, no cycles,
    #: at most `MAX_TASK_DEPTH` levels — all enforced in `services.set_parent`,
    #: the one writer. SET_NULL: deleting a parent promotes its children to
    #: top-level tasks instead of deleting work.
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="subtasks",
    )
    #: WHAT this task is (hal T82, Jonathan 2026-10-12). `work` — the agent's
    #: own work, and every task that says nothing. `idea` — a PERSON's want
    #: about the product, carrying their own words (at least one human-why
    #: quote, enforced in `services`); the work toward it is its subtasks.
    #: Explicit, never inferred: T69 read every task as an idea, and the agent's
    #: process work (a docs round-up, a screen review) counted as the product's
    #: shipped ideas.
    WORK, IDEA = "work", "idea"
    KIND_CHOICES = [(WORK, "Work"), (IDEA, "Idea")]
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=WORK)
    #: The task's own Drive folder, inside its project's — what
    #: `canopy gdoc publish --task` files under. The id is what `gog drive`
    #: needs; the url is what a human clicks. Blank until something is filed.
    drive_folder_id = models.CharField(max_length=128, blank=True, default="")
    drive_folder_url = models.URLField(max_length=500, blank=True, default="")
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
    #: How sure the agent is about a suggestion: one of `CONFIDENCE_VALUES`.
    confidence = models.CharField(max_length=10, blank=True, default="")
    priority = models.PositiveSmallIntegerField(default=0, choices=PRIORITY_CHOICES)
    labels = models.ManyToManyField("agents.Label", blank=True, related_name="tasks")
    milestone = models.ForeignKey("agents.ProjectMilestone", null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="tasks")
    #: Out of Triage until then — or until something new happens on it (an
    #: action or a field change clears it, `services`).
    snoozed_until = models.DateTimeField(null=True, blank=True)
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
    #: Who the next step waits on, as a real person canopy can notify. The free
    #: text `assigned` stays beside it for counterparts canopy has never seen.
    waiting_on_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="tasks_waiting_on",
    )

    # ---- the ask: what this task needs from a person ----------------------
    #: `review` ("should I do this?") or `question` ("I need an answer"); blank
    #: means the task asks nothing and is simply work in flight.
    ASK_NONE, ASK_REVIEW, ASK_QUESTION = "", "review", "question"
    ASK_CHOICES = [(ASK_NONE, "None"), (ASK_REVIEW, "Review"), (ASK_QUESTION, "Question")]
    ask_kind = models.CharField(max_length=10, choices=ASK_CHOICES, blank=True, default=ASK_NONE)
    #: The ask's own words, frozen when it was raised.
    ask_body = models.TextField(blank=True, default="")
    #: Set by the action that closed the ask; null while it is open. Who closed it
    #: and why is that action's row.
    ask_closed_at = models.DateTimeField(null=True, blank=True)

    #: Turn specs that run when the task is approved (or its question answered).
    on_approve = models.JSONField(default=list, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    #: One sitting (a fleet audit), so a batch can be rendered together.
    batch_key = models.CharField(max_length=64, blank=True, default="")
    #: Unique where set, so a producer re-posting a batch replays rather than
    #: duplicating. Null (not "") for a task nobody keyed, because unique
    #: columns treat NULLs as distinct and empty strings as equal.
    idempotency_key = models.CharField(max_length=128, null=True, blank=True, unique=True)

    #: Where the ask came from, when it was raised by machinery rather than
    #: typed by a person.
    origin = models.CharField(max_length=32, blank=True, default="")
    #: What a caller may post as `origin` (AgentTaskIn refuses anything else):
    #: blank, the postable Turn sources an ask was raised from (legacy spellings
    #: are normalized onto these — harness.schemas.normalize_origin), and the
    #: labels canopy's own CLI stamps on the tasks it files (`canopy agent task
    #: add` → task-tracker, `canopy agent dispatch` → dispatch, a huddle's
    #: outcomes → huddle). `canopy_scheduler` is server-set, never posted.
    POSTABLE_ORIGINS = ("", "api", "ace_web", "email", "slack",
                        "task-tracker", "dispatch", "huddle")
    origin_ref = models.JSONField(default=dict, blank=True)
    raised_by = models.ForeignKey(
        "harness.Turn", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="raised_tasks",
    )

    #: Order within a list or a board column: a fractional rank, so moving one
    #: task between two others writes one row (`services.rank_between`).
    rank = models.FloatField(default=0)
    source = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rank", "id"]
        constraints = [
            # Ignoring case: an alias is looked up `ext_id__iexact`, so "t1" beside
            # "T1" would leave one of them unreachable.
            models.UniqueConstraint(Lower("ext_id"), "created_by_agent", condition=~models.Q(ext_id=""),
                                    name="uniq_task_agent_alias_ci"),
            models.UniqueConstraint(fields=["workspace", "number"], name="uniq_task_number"),
            models.CheckConstraint(condition=models.Q(agent__isnull=True) | models.Q(assignee_user__isnull=True),
                                   name="task_one_assignee"),
            models.CheckConstraint(condition=models.Q(confidence__in=["", "high", "medium", "low"]),
                                   name="task_confidence_known"),
            models.CheckConstraint(condition=models.Q(priority__lte=4), name="task_priority_known"),
        ]
        indexes = [models.Index(fields=["agent", "status"]), models.Index(fields=["workspace", "status"]),
                   models.Index(fields=["assignee_user", "status"])]

    def __str__(self):
        return f"task:{self.key}:{self.status}"

    def save(self, *args, **kwargs):
        """Fill what a row may not lack, whoever builds it: its workspace (its
        project's, else its parent's, else its agent's), its number and key
        there, and — carrying an alias — the agent the alias is of."""
        if not self.workspace_id:
            for src in (self.project if self.project_id else None, self.parent if self.parent_id else None,
                        self.agent if self.agent_id else None,
                        self.created_by_agent if self.created_by_agent_id else None):
                if src is not None:
                    self.workspace_id = src.workspace_id
                    break
        if self.ext_id and not self.created_by_agent_id and self.pk is None:
            self.created_by_agent_id = self.agent_id
        if not self.key:
            from ..work import allocate_number  # noqa: PLC0415 — work imports models

            self.number, self.key = allocate_number(self.workspace_id)
        super().save(*args, **kwargs)

    @property
    def job_schedule_id(self) -> int | None:
        """The schedule of the FAILED PLAIN JOB this review is about, else None.

        A job's review (`schedule_turns._raise_job_nag`) carries no `on_approve`, and
        approving a task without one starts a model turn on the agent: for a job, the
        empty-prompt mistake. So it has no approve action; "Run now" on its schedule is
        how a job runs again."""
        ref = self.origin_ref or {}
        if ref.get("kind") != "schedule_nag" or not ref.get("job"):
            return None
        sid = ref.get("schedule_id")
        return sid if isinstance(sid, int) else None

    @property
    def ref(self) -> str:
        """What an agent's route names it by: its alias where it has one, else its key."""
        return self.ext_id or self.key

    @property
    def url(self) -> str:
        """Its one page."""
        return f"/w/{self.workspace_id}/tasks/{self.key}"

    @property
    def category(self) -> str:
        return self.CATEGORY.get(self.status, "triage")

    @property
    def position(self) -> int:
        """`rank` as the integer older callers read."""
        return int(self.rank)

    @property
    def assignee_email(self) -> str | None:
        return self.assignee_user.email if self.assignee_user_id else None

    @property
    def created_by_agent_slug(self) -> str | None:
        return self.created_by_agent.slug if self.created_by_agent_id else None

    @property
    def label_list(self) -> list[dict]:
        """Its labels, from the prefetch cache when the list prefetched them."""
        if self.pk is None:
            return []
        return [{"name": lb.name, "color": lb.color} for lb in self.labels.all()]

    @property
    def milestone_name(self) -> str | None:
        return self.milestone.name if self.milestone_id else None

    @property
    def waiting_on_email(self) -> str | None:
        return self.waiting_on_user.email if self.waiting_on_user_id else None

    @property
    def ask_is_open(self) -> bool:
        return bool(self.ask_kind) and self.ask_closed_at is None

    @property
    def project_ext_id(self) -> str | None:
        """The project's agent alias where it has one, else its handle."""
        return (self.project.ext_id or self.project.handle) if self.project_id else None

    @property
    def project_handle(self) -> str | None:
        return self.project.handle if self.project_id else None

    @property
    def project_name(self) -> str | None:
        return self.project.name if self.project_id else None

    @property
    def project_address(self) -> str | None:
        """`<workspace>/<handle>`."""
        return self.project.address if self.project_id else None

    @property
    def parent_ext_id(self) -> str | None:
        return self.parent.ref if self.parent_id else None

    @property
    def parent_key(self) -> str | None:
        return self.parent.key if self.parent_id else None

    @property
    def agent_slug(self) -> str | None:
        """The assignee agent's slug, or None when an agent is not the assignee."""
        return self.agent.slug if self.agent_id else None

    @property
    def human_why(self) -> list[dict]:
        """The people's own words behind this task, oldest first (`AgentTaskHumanWhy`).

        Reads the prefetch cache when the caller prefetched `human_whys`, so a list
        route costs one extra query rather than one per task."""
        return [w.as_dict() for w in self.human_whys.all()]


#: How deep subtasks nest: a task, its subtasks, and theirs. Deeper than that
#: is a project's worth of structure hiding inside one card — split it out.
MAX_TASK_DEPTH = 3


class AgentTaskHumanWhy(models.Model):
    """What a PERSON actually said that this task answers — quoted, attributed,
    linked — kept apart from the agent's `rationale` (T68).

    `rationale` is the agent's inference, and it never traced back to whoever
    wanted the work: "Canopy tasks do a decent job of explaining why the AI
    thinks a task matters, but they don't trace back to the person who
    originally wanted it" (the Supply brief, Part 3). A person's words weigh
    more than the agent's reading of them, so the board shows these first.

    **A row per quote, not a JSON list on the task**, for two reasons that are
    both about `who`:
      * `person` is a real foreign key, so the link survives (SET_NULL keeps the
        quote and its name/email if the Person goes) and cannot point at a row
        that never existed — a JSON id would.
      * "everything this person asked for, across every task" is the question
        contributor pages ask (T69). With rows it is one indexed query on
        `person`; with JSON it is a scan of every task's blob.
    The cost is one prefetch on the list routes. The list is still written
    wholesale (create, or a PATCH that sends `human_why`), like `links`.

    **Verbatim or nothing.** `quote` is the person's own words, never a
    paraphrase — an agent with no quote leaves the list empty.
    """

    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="human_whys")
    position = models.PositiveSmallIntegerField(default=0)
    quote = models.TextField(help_text="The person's own words, verbatim.")
    #: The canopy Person who said it, when canopy knows them in the agent's
    #: workspace. Resolved from `who_email` (or given directly) at write time.
    person = models.ForeignKey("contacts.Person", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="task_quotes")
    #: Kept beside `person` so a quote from someone canopy has never seen still
    #: says who, and a deleted Person does not orphan the attribution.
    who_email = models.EmailField(blank=True, default="")
    who_name = models.CharField(max_length=200, blank=True, default="")
    #: Where they said it: the thread, the doc comment, the session.
    source_url = models.URLField(max_length=500, blank=True, default="")
    said_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.task_id}: {self.who_name or self.who_email}: {self.quote[:40]}"

    def as_dict(self) -> dict:
        return {
            "quote": self.quote,
            "who": {"person_id": self.person_id, "email": self.who_email, "name": self.who_name},
            "source_url": self.source_url,
            "said_at": self.said_at,
        }


class AgentTaskAction(models.Model):
    """Something a person did to a task. While `pending` it is also the assignee
    agent's to-do: the agent drains pending rows on its next turn and marks each
    applied. The task's whole history is its `TaskEvent` stream, which links
    these rows."""

    APPROVE, DECLINE, REPLY, NUDGE, DONE = "approve", "decline", "reply", "nudge", "done"
    ACCEPT, BACKLOG, SNOOZE, DUPLICATE = "accept", "backlog", "snooze", "duplicate"
    ACTIONS = (APPROVE, DECLINE, REPLY, NUDGE, DONE, ACCEPT, BACKLOG, SNOOZE, DUPLICATE)
    ACTION_CHOICES = [(a, a) for a in ACTIONS]
    PENDING, APPLIED = "pending", "applied"
    STATUS_CHOICES = [(PENDING, "Pending"), (APPLIED, "Applied")]

    #: The task's assignee agent when it was done — whose queue a pending row is
    #: on. Null on a task no agent is assigned.
    agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="task_actions")
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="actions")
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    comment = models.TextField(blank=True, default="")
    by = models.CharField(max_length=200, blank=True, default="")
    by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="task_actions")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=APPLIED)
    applied_at = models.DateTimeField(null=True, blank=True)
    result_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["agent", "status"])]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.agent_id}:{self.task_id}:{self.action}"

    @property
    def agent_slug(self) -> str | None:
        return self.agent.slug if self.agent_id else None

    @property
    def task_ext_id(self) -> str:
        return self.task.ref

    @property
    def task_key(self) -> str:
        return self.task.key


class TaskSettings(models.Model):
    """A workspace's task settings: the prefix its keys carry, the counter that
    numbers them, how each status category is shown, and how often an active
    project should post an update. Made on first use (`work.settings_for`)."""

    workspace = models.OneToOneField("workspaces.Workspace", primary_key=True, on_delete=models.CASCADE,
                                     related_name="task_settings")
    #: `DIM` in `DIM-142`. Upper-case letters and digits, unique across canopy so
    #: a key names one task anywhere; frozen once a task carries it.
    prefix = models.CharField(max_length=10, unique=True)
    #: The last number handed out. Only goes up: a reused number would make an
    #: old link point at new work.
    last_number = models.PositiveIntegerField(default=0)
    #: category -> the name this workspace shows it by ({"unstarted": "Ready"}).
    #: Missing categories use the default names.
    status_names = models.JSONField(default=dict, blank=True)
    #: An active project with no update in this many days shows "update missing".
    update_cadence_days = models.PositiveSmallIntegerField(default=7)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.prefix}"


class Label(models.Model):
    """A label tasks in one workspace share."""

    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=60)
    #: A semantic colour token name (`primary`, `success`, `warning`,
    #: `destructive`, `info`, `muted`), never a raw palette value.
    color = models.CharField(max_length=20, blank=True, default="muted")
    description = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), "workspace", name="uniq_label_name_ci")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.name}"


class TaskRelation(models.Model):
    """`from_task` blocks / relates to / duplicates `to_task`. "Blocked by" is
    `blocks` read from the other end — never stored twice."""

    BLOCKS, RELATED, DUPLICATE = "blocks", "related", "duplicate"
    KINDS = (BLOCKS, RELATED, DUPLICATE)
    from_task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="relations_out")
    to_task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="relations_in")
    kind = models.CharField(max_length=10, choices=[(k, k) for k in KINDS])
    created_by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["from_task", "to_task", "kind"], name="uniq_task_relation"),
            models.CheckConstraint(condition=~models.Q(from_task=models.F("to_task")),
                                   name="task_relation_not_self"),
            models.CheckConstraint(condition=models.Q(kind__in=["blocks", "related", "duplicate"]),
                                   name="task_relation_kind_known"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.from_task_id} {self.kind} {self.to_task_id}"


class TaskEvent(models.Model):
    """One entry in a task's history, append-only: it was created, a field
    changed, someone acted on it (the `AgentTaskAction` row, whose comment is
    the comment), or a relation was added or removed. Written by `work.record`
    from every writer, so the stream is the same whichever path the change came
    through."""

    CREATED, FIELD, ACTION, RELATION = "created", "field", "action", "relation"
    KINDS = (CREATED, FIELD, ACTION, RELATION)
    task = models.ForeignKey(AgentTask, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=10, choices=[(k, k) for k in KINDS])
    #: For `field`: which one, and its value before and after, as shown (a key,
    #: an email, a status) — the history must read the same after the related
    #: row is gone. For `relation`: the kind, and the other task's key in `new`
    #: (added) or `old` (removed).
    field = models.CharField(max_length=40, blank=True, default="")
    old = models.TextField(blank=True, default="")
    new = models.TextField(blank=True, default="")
    action = models.ForeignKey(AgentTaskAction, null=True, blank=True, on_delete=models.CASCADE,
                               related_name="events")
    by_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                on_delete=models.SET_NULL, related_name="+")
    by_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["task", "created_at"])]
        constraints = [models.CheckConstraint(condition=models.Q(kind__in=["created", "field", "action", "relation"]),
                                              name="task_event_kind_known")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.task_id}:{self.kind}:{self.field}"


class ProjectMilestone(models.Model):
    """A checkpoint inside a project. Its progress is the share of its tasks done."""

    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="milestones")
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    target_date = models.DateField(null=True, blank=True)
    rank = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rank", "id"]
        constraints = [models.UniqueConstraint(fields=["project", "name"], name="uniq_milestone_name")]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}:{self.name}"


class ProjectUpdate(models.Model):
    """A project's status update: its health, a short note, who wrote it, and
    what changed since the last one (`diff`, filled in when it is published).

    An agent may DRAFT one (a check-in run ends with one); a person PUBLISHES it,
    choosing the health — the agent proposes, the person decides. A workspace
    admin may let the project's lead agent publish its own drafts
    (`AgentProject.lead_agent_may_publish`); such an update names the agent in
    `published_by_agent`. A project's health is its latest published update's."""

    ON_TRACK, AT_RISK, OFF_TRACK = "on_track", "at_risk", "off_track"
    HEALTHS = (ON_TRACK, AT_RISK, OFF_TRACK)
    DRAFT, PUBLISHED = "draft", "published"
    project = models.ForeignKey(AgentProject, on_delete=models.CASCADE, related_name="updates")
    health = models.CharField(max_length=10, choices=[(h, h) for h in HEALTHS])
    body = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=[(DRAFT, DRAFT), (PUBLISHED, PUBLISHED)], default=PUBLISHED)
    author_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL, related_name="+")
    author_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name="+")
    #: The lead agent that published it itself, where its project allows that
    #: (`AgentProject.lead_agent_may_publish`); null when a person published it.
    published_by_agent = models.ForeignKey(Agent, null=True, blank=True, on_delete=models.SET_NULL,
                                           related_name="+")
    checkin_run = models.ForeignKey("huddles.CheckinRun", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="updates")
    #: What changed since the previous published update: `{tasks_done, tasks_added,
    #: target_date: [old, new], milestones: [{name, done, total}]}`.
    diff = models.JSONField(default=dict, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["project", "status", "published_at"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(health__in=["on_track", "at_risk", "off_track"]),
                                   name="project_update_health_known"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.project_id}:{self.health}:{self.status}"

    @property
    def author_email(self) -> str | None:
        return self.author_user.email if self.author_user_id else None

    @property
    def author_agent_slug(self) -> str | None:
        return self.author_agent.slug if self.author_agent_id else None

    @property
    def published_by_email(self) -> str | None:
        return self.published_by.email if self.published_by_id else None

    @property
    def published_by_agent_slug(self) -> str | None:
        return self.published_by_agent.slug if self.published_by_agent_id else None


class SavedView(models.Model):
    """A saved task list: filters, grouping, sorting and layout under a name.
    Personal unless `shared`, when everyone in the workspace sees it."""

    TABLE, BOARD, CALENDAR = "table", "board", "calendar"
    LAYOUTS = (TABLE, BOARD, CALENDAR)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE, related_name="saved_views")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_views")
    name = models.CharField(max_length=100)
    shared = models.BooleanField(default=False)
    #: The list's query, as the Tasks page puts it in its URL ({"assignee": "me",
    #: "category": "started,unstarted", "label": "bug"}).
    filters = models.JSONField(default=dict, blank=True)
    group_by = models.CharField(max_length=20, blank=True, default="")
    sort = models.CharField(max_length=20, blank=True, default="")
    layout = models.CharField(max_length=10, choices=[(x, x) for x in LAYOUTS], default=TABLE)
    rank = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rank", "id"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.workspace_id}:{self.name}"
