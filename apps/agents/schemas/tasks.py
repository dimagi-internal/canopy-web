from __future__ import annotations

import datetime as dt
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator

from apps.agents.models import AgentTask
from apps.common.schemas import StrictModel
from apps.harness.schemas import TurnSpecIn, normalize_origin

from .agents import AgentDetailOut


# ---- projects and tasks ----
class AgentTaskLink(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=500)
    #: `decision`: an open question this task is carrying for a person — the
    #: project's team view lists each one as a line under "waiting on". Blank
    #: for an ordinary link.
    #: `live`: on a PROJECT, what the product already does today — its label
    #: says what, its url opens it. The project page leads "Where it stands"
    #: with these, so a live product never reads as "nothing built" because
    #: none of its ideas is marked shipped.
    kind: Literal["", "decision", "live"] = ""
    #: On a decision: who it waits on, by name. Blank falls back to the task's
    #: waiting-on person, then the project's owner.
    waiting_on: str = Field(default="", max_length=120)


class HumanWho(StrictModel):
    """Who said it. Give `email` (and `name`): canopy links the Person it names
    when it knows them in this agent's workspace. `person_id` names one
    directly. At least one of the three."""

    person_id: int | None = None
    email: str = Field(default="", max_length=254)
    name: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _someone(self):
        if self.person_id is None and not self.email.strip() and not self.name.strip():
            raise ValueError("who needs an email, a name or a person_id")
        return self


class HumanWhyIn(StrictModel):
    """What a person actually said that this task answers (T68): their words,
    verbatim — never a paraphrase — who said them, and where."""

    quote: str = Field(min_length=1, max_length=4000)
    who: HumanWho
    source_url: str = Field(default="", max_length=500)
    said_at: dt.datetime | None = None


class HumanWhoOut(StrictModel):
    #: Set when canopy linked a Person (known in the agent's workspace).
    person_id: int | None = None
    email: str = ""
    name: str = ""


class HumanWhyOut(StrictModel):
    quote: str
    who: HumanWhoOut
    source_url: str = ""
    said_at: dt.datetime | None = None


class AgentProjectIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    ext_id: str = Field(default="", max_length=64)
    #: Its address in its workspace; free from its name when omitted.
    handle: str = Field(default="", max_length=60)
    outcome: str = ""
    status: str = "active"
    owner_note: str = Field(default="", max_length=200)
    # The lead as a real person — a member of the project's workspace — so an
    # idea added on the team view can email them. Unknown → 422.
    owner_email: str = Field(default="", max_length=254)
    #: 0 none, 1 urgent, 2 high, 3 medium, 4 low.
    priority: int = Field(default=0, ge=0, le=4)
    start_date: dt.date | None = None
    target_date: dt.date | None = None
    drive_folder_id: str = Field(default="", max_length=128)
    drive_folder_url: str = Field(default="", max_length=500)
    repo_slug: str = Field(default="", max_length=100)
    notes: str = ""
    links: list[AgentTaskLink] = Field(default_factory=list)


class AgentProjectPatch(StrictModel):
    name: str | None = Field(default=None, max_length=200)
    outcome: str | None = None
    status: str | None = None
    owner_note: str | None = Field(default=None, max_length=200)
    # Present-and-empty clears the linked owner; absent leaves it.
    owner_email: str | None = Field(default=None, max_length=254)
    drive_folder_id: str | None = Field(default=None, max_length=128)
    drive_folder_url: str | None = Field(default=None, max_length=500)
    repo_slug: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    links: list[AgentTaskLink] | None = None
    priority: int | None = Field(default=None, ge=0, le=4)
    start_date: dt.date | None = None
    target_date: dt.date | None = None
    #: The agents working on it, by slug — REPLACES the list (its lead agent
    #: stays one).
    member_agents: list[str] | None = Field(default=None, max_length=50)
    #: The task this project grew out of, as `<agent>/<task ext_id>` ("ada/T41");
    #: `""` clears it. Same agent's or another visible agent's task.
    spawned_from: str | None = Field(default=None, max_length=130)
    #: A new address in its workspace — callers holding the old one lose it.
    handle: str | None = Field(default=None, max_length=60)
    #: With `handle`: move it to this workspace — its lead agent's own or one
    #: below it ("connect" for a dimagi agent).
    workspace: str | None = Field(default=None, max_length=100)
    #: Its lead agent, by slug (`""`: no agent leads it).
    steward: str | None = Field(default=None, max_length=100)


class WorkspaceProjectIn(AgentProjectIn):
    """Create a project in a workspace: `workspace` (default: the lead agent's,
    else the one you are in), led by `steward` (an agent's slug) if given."""

    handle: str = Field(default="", max_length=60)
    steward: str = Field(default="", max_length=100)
    workspace: str = Field(default="", max_length=100)
    #: The agents working on it, by slug, beside its lead agent.
    member_agents: list[str] = Field(default_factory=list, max_length=50)


class ProjectShareIn(StrictModel):
    #: The workspace to share the project with (and the ones below it): one of
    #: the project's own tree, not its workspace or one above it.
    workspace: str = Field(min_length=1, max_length=100)


class ProjectShareOut(StrictModel):
    workspace: str
    added_by: str | None = None
    created_at: dt.datetime
    #: The project's agents that are members through this share: of that workspace
    #: or below it, and not of the project's own side.
    member_agents: list[str] = Field(default_factory=list)


class ProjectShareRemovedOut(StrictModel):
    workspace: str
    #: Member agents taken off the project because the share was all that let them
    #: work on it (never its lead agent).
    removed_agents: list[str] = Field(default_factory=list)


class KbUploadOut(StrictModel):
    """Where to PUT a kb.sqlite.gz (straight to storage, never through canopy),
    then name `key` to `POST …/kb/publish`."""

    key: str
    url: str
    headers: dict[str, str] = {}
    expires_in: int


class KbPublishIn(StrictModel):
    key: str = Field(min_length=1, max_length=300)
    #: sha256 of the uploaded file — what serving instances cache by and verify.
    sha256: str = Field(min_length=64, max_length=64)
    #: The version this file was made from (null for a knowledge base's first): the
    #: publish is refused (409) when another version landed since.
    base: int | None
    #: The curation state it was built from, uploaded first as a result (`…/kb/results`):
    #: the next refresh starts from it. Blank for a file built without one.
    curation: str = Field(default="", max_length=64)
    #: How many documents' CONTENT it added, changed and removed ({added, changed,
    #: removed}), as the builder counted them: the version log shows these.
    changes: dict | None = None


class KbBlobIn(StrictModel):
    #: sha256 of the record's canonical JSON — its address.
    sha256: str = Field(min_length=64, max_length=64)
    #: sha256 and size of the gzip uploaded (S3 checks it).
    gz_sha256: str = Field(min_length=64, max_length=64)
    bytes: int = Field(gt=0)


class KbBlobsIn(StrictModel):
    blobs: list[KbBlobIn] = Field(max_length=1000)


class KbObjectsIn(StrictModel):
    store: Literal["results", "originals"]
    #: Results by step key, originals by sha256.
    keys: list[str] = Field(max_length=1000)


class KbObjectsHaveOut(StrictModel):
    have: list[str]


class KbObjectLinkOut(StrictModel):
    key: str
    url: str


class KbObjectLinksOut(StrictModel):
    """GET each `url`: results come gzipped, originals as themselves."""

    links: list[KbObjectLinkOut]
    expires_in: int


class KbVersionOut(StrictModel):
    number: int
    kind: Literal["full", "delta", "rollback"]
    status: Literal["pending", "building", "ready", "failed"]
    base: int | None = None
    rollback_of: int | None = None
    added: int
    changed: int
    removed: int
    uploaded_bytes: int
    kb_bytes: int | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    published_by: str = ""
    created_at: datetime
    finished_at: datetime | None = None
    #: The Drive replica's outcome, when the project keeps one.
    replica: str = ""
    #: The curation state it was built from: a refresh of the head starts from it.
    curation_sha: str = ""
    #: The version the project serves now.
    current: bool = False
    #: Its file is kept, so the project can be pointed back at it.
    can_roll_back: bool = False


class KbFileOut(StrictModel):
    """The kb.sqlite.gz a project serves now: a 15-minute download link."""

    url: str
    sha256: str
    bytes: int
    version: int | None = None
    published_at: datetime | None = None


class KbSettingsIn(StrictModel):
    #: Write each new version's kb.sqlite.gz into the project's Drive folder.
    drive_replica: bool


class KbSourceIn(StrictModel):
    kind: Literal["web"]
    name: str = Field(min_length=1, max_length=100)
    #: What the crawler reads (notes).
    config: dict[str, Any] = Field(default_factory=dict)


class KbSourceOut(StrictModel):
    id: int
    kind: str
    name: str
    config: dict[str, Any]


class KbOriginalIn(StrictModel):
    sha256: str = Field(min_length=64, max_length=64)
    bytes: int = Field(ge=0)
    content_type: str = Field(default="application/octet-stream", max_length=200)


class KbOriginalsIn(StrictModel):
    """The original files a build points at (`canopy kb push --originals`)."""

    files: list[KbOriginalIn] = Field(max_length=1000)


class KbOriginalUploadOut(StrictModel):
    sha256: str
    url: str
    headers: dict[str, str] = {}


class KbOriginalsOut(StrictModel):
    """PUT each file to its `url` with its `headers`; `have` were already stored."""

    have: int
    uploads: list[KbOriginalUploadOut]
    expires_in: int


class McpServerOut(StrictModel):
    """One MCP server in the directory: a workspace project serving its own tools.
    Listed so a person or agent can decide to add it; nothing adds it for them."""

    #: The name to add it under (its handle: two side by side never collide).
    name: str
    #: `<workspace>/<handle>`, the address that survives a change of steward.
    address: str
    workspace: str
    workspace_name: str
    title: str
    #: What it is, in its steward's words (the project's outcome).
    description: str
    #: "kb" — a knowledge base.
    kind: str
    steward_slug: str
    url: str
    #: `claude mcp add --transport http <name> <url>`.
    add_command: str
    #: The project's page on canopy-web.
    project_url: str
    published_at: datetime | None = None
    bytes: int | None = None


class SpawnedFromOut(StrictModel):
    """The task a project grew out of: "from <task> in <project>"."""

    agent: str
    ref: str
    key: str = ""
    title: str
    status: str
    project_ref: str = ""
    project_name: str = ""
    #: The task's own page.
    url: str


class LabelBriefOut(StrictModel):
    name: str
    color: str = "muted"


class AgentProjectOut(StrictModel):
    id: int
    #: Its lead agent, which holds its Drive folder (None: no agent leads it).
    agent_slug: str | None = None
    #: The workspace that owns it. A list pinned to a parent workspace carries
    #: its children's rows too.
    workspace: str = Field(default="", validation_alias=AliasChoices("workspace_id", "workspace"))
    #: Its address within its workspace.
    handle: str = ""
    #: Its lead agent (the same as `agent_slug`).
    steward_slug: str | None = None
    #: The agents working on it, its lead agent among them.
    member_agents: list[str] = Field(default_factory=list,
                                     validation_alias=AliasChoices("member_slugs", "member_agents"))
    #: The workspaces of its tree it is shared with: open to them and the
    #: workspaces below them (`/shares/`).
    shared_with: list[str] = Field(default_factory=list)
    #: True on a row of a list that the reader sees only because it is shared
    #: with their workspace.
    shared_in: bool = False
    #: How to name it anywhere: `<workspace>/<handle>`.
    address: str = ""
    #: Its one page.
    url: str = ""
    #: Its page on its lead agent's board (`url` when no agent leads it).
    board_url: str = ""
    #: What it serves as its own MCP server ("" nothing, "kb" a knowledge base),
    #: and where (relative to canopy's origin; empty until something is published).
    mcp_kind: str = ""
    mcp_path: str = ""
    kb_published_at: datetime | None = None
    #: Its knowledge base's current version (`…/kb/versions`), and whether each new
    #: version is also written into its Drive folder (off by default).
    kb_version_number: int | None = None
    kb_drive_replica: bool = False
    #: Its maker agent's alias ("P5"); blank for a project made elsewhere.
    ext_id: str
    name: str
    outcome: str
    status: Literal["proposed", "planned", "active", "paused", "done", "canceled", "archived"]
    #: triage | unstarted | started | paused | completed | canceled.
    category: str = ""
    priority: int = 0
    start_date: dt.date | None = None
    target_date: dt.date | None = None
    owner_note: str
    #: Its lead (the person accountable).
    owner_email: str | None = None
    #: The latest published update's health (on_track | at_risk | off_track).
    health: str | None = None
    #: Active, and no update within the workspace's cadence.
    update_missing: bool = False
    #: Tasks done / in scope (canceled ones out), by category.
    done_task_count: int = 0
    drive_folder_id: str
    drive_folder_url: str
    repo_slug: str
    notes: str
    links: list[AgentTaskLink] = Field(default_factory=list)
    task_count: int = 0
    open_task_count: int = 0
    #: Live tasks with an open ask or parked on a person (`services.waiting_q`).
    waiting_task_count: int = 0
    #: The task this project grew out of, if it did.
    spawned_from: SpawnedFromOut | None = Field(
        default=None, validation_alias=AliasChoices("spawned_from_ref", "spawned_from"))
    created_at: dt.datetime
    updated_at: dt.datetime


class AgentTaskIn(StrictModel):
    """One task to create. `POST /tasks/` takes a LIST of these.

    `idempotency_key` makes a create safe to retry: a key already seen returns
    the task it made instead of making another. `ext_id` is assigned (T1, T2 …)
    when omitted — pass it only to mirror an id the agent already uses.
    """

    ext_id: str = Field(default="", max_length=64)
    #: The project this task belongs to, by its `ext_id` ("P3") or numeric id.
    #: Empty means a one-off, which plenty of work legitimately is.
    project: str = Field(default="", max_length=64)
    #: The task this is a subtask of, by `ext_id` ("T66") — same agent, same
    #: project (inherited when `project` is omitted), no cycles, at most three
    #: levels deep. A parent created earlier in the same batch may be named.
    parent: str = Field(default="", max_length=64)
    #: `work` (the default: the agent's own work) or `idea` — a person's want
    #: about the product, which must carry their words in `human_why`. Never
    #: inferred; the work toward an idea is its subtasks (`parent`).
    kind: Literal["work", "idea"] = "work"
    #: The task's own Drive folder (inside its project's), when it has one.
    drive_folder_id: str = Field(default="", max_length=128)
    drive_folder_url: str = Field(default="", max_length=500)
    title: str = Field(min_length=1, max_length=300)
    #: The people's own words behind the task, verbatim, with who and where.
    #: Separate from `rationale` (the agent's inference) and shown above it.
    #: Leave it empty rather than paraphrase.
    human_why: list[HumanWhyIn] = Field(default_factory=list, max_length=20)
    next_action: str = Field(default="", max_length=300)
    #: suggested · backlog · todo · in_progress · done · declined.
    status: str = "suggested"  # normalized server-side
    #: Who does it: a member's email, or an agent's slug (`agent:<slug>`).
    #: Omitted: the agent whose route filed it (nobody, on the workspace route).
    assignee: str | None = Field(default=None, max_length=254)
    owner: str = Field(default="", max_length=120)
    assigned: str = Field(default="", max_length=120)
    #: Who the next step waits on, by email — a member of the task's workspace.
    waiting_on_email: str = Field(default="", max_length=254)
    #: high · medium · low (anything else is stored as blank).
    confidence: str = Field(default="", max_length=10)
    #: 0 none, 1 urgent, 2 high, 3 medium, 4 low.
    priority: int = Field(default=0, ge=0, le=4)
    #: Label names; unknown ones are made.
    labels: list[str] = Field(default_factory=list, max_length=20)
    #: A milestone of its project, by name or id.
    milestone: str = Field(default="", max_length=200)
    score: str = Field(default="", max_length=8)
    review: str = ""
    rationale: str = ""
    source_url: str = Field(default="", max_length=500)
    plan: str = ""
    due: dt.date | None = None
    links: list[AgentTaskLink] = Field(default_factory=list)
    notes: str = ""
    #: Order in a list (sets its rank). Omitted: above every other task.
    position: int | None = None
    #: What the task asks a person, if anything: `review` ("should I do
    #: this?") or `question` ("I need an answer"). Blank asks nothing.
    ask_kind: Literal["", "review", "question"] = ""
    ask_body: str = ""
    #: The turns that run when the task is approved (or its question answered).
    on_approve: list[TurnSpecIn] = Field(default_factory=list)
    batch_key: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=128)
    #: One of `AgentTask.POSTABLE_ORIGINS`; a retired spelling is normalized.
    origin: str = Field(default="", max_length=32)
    origin_ref: dict = Field(default_factory=dict)
    #: The turn that raised this task, if any.
    raised_by: uuid.UUID | None = None
    #: Free-text producer tag (the sheet / tool the task was mirrored from).
    source: str = Field(default="", max_length=100)

    @field_validator("origin")
    @classmethod
    def _known_origin(cls, v: str) -> str:
        # A retired spelling (cron, manual …) is accepted and normalized, as on
        # every other input carrying an origin.
        if v not in AgentTask.POSTABLE_ORIGINS and normalize_origin(v) == v:
            raise ValueError(f"origin must be one of {', '.join(o for o in AgentTask.POSTABLE_ORIGINS if o)}"
                             f" (or blank), got {v!r}")
        return normalize_origin(v)


class AgentTaskOut(StrictModel):
    """A task, addressed by its `key` (`DIM-142`) anywhere, and on an agent's
    route also by `ext_id` (that agent's alias, else the key)."""

    #: `<PREFIX>-<number>`: its one name.
    key: str = ""
    #: Its one page.
    url: str = ""
    #: The assignee agent (None when a person, or nobody, is the assignee).
    agent_slug: str | None = None
    #: The assignee person.
    assignee_email: str | None = None
    created_by_agent_slug: str | None = None
    #: The workspace that owns it — see `AgentProjectOut.workspace`.
    workspace: str = Field(default="", validation_alias=AliasChoices("workspace_id", "workspace"))
    #: Its agent alias ("T12") where it has one, else its key.
    ext_id: str = Field(validation_alias=AliasChoices("ref", "ext_id"))
    #: Its project's alias where it has one, else the project's handle.
    project_ext_id: str | None = None
    project_handle: str | None = None
    project_name: str | None = None
    #: `<workspace>/<handle>`.
    project_address: str | None = None
    #: The task this is a subtask of (its alias, else its key); null for a top-level task.
    parent_ext_id: str | None = None
    parent_key: str | None = None
    #: `idea` — a person's want, in their words (`human_why`); `work` otherwise.
    kind: Literal["work", "idea"] = "work"
    drive_folder_id: str = ""
    drive_folder_url: str = ""
    title: str
    #: What people actually said, verbatim, oldest first — above `rationale`.
    human_why: list[HumanWhyOut] = Field(default_factory=list)
    next_action: str
    status: Literal["suggested", "backlog", "todo", "in_progress", "done", "declined"]
    #: triage · backlog · unstarted · started · completed · canceled.
    category: str = ""
    priority: int = 0
    labels: list[LabelBriefOut] = Field(default_factory=list,
                                        validation_alias=AliasChoices("label_list", "labels"))
    milestone: str | None = Field(default=None, validation_alias=AliasChoices("milestone_name", "milestone"))
    snoozed_until: dt.datetime | None = None
    owner: str
    assigned: str
    waiting_on_email: str | None = None
    # The ask, where the task carries one. Blank `ask_kind` means the task asks
    # nothing and is simply work in flight. Open until an action closes it.
    ask_kind: str = ""
    ask_body: str = ""
    ask_open: bool = Field(default=False,
                           validation_alias=AliasChoices("ask_is_open", "ask_open"))
    ask_closed_at: dt.datetime | None = None
    on_approve: list[dict] = Field(default_factory=list)
    dispatched_at: dt.datetime | None = None
    batch_key: str = ""
    #: The producer's key, echoed so an agent can match the tasks it published.
    #: "" when none was sent (the column is NULL then).
    idempotency_key: str = ""
    origin: str = ""
    confidence: str
    score: str
    review: str
    rationale: str
    source_url: str
    plan: str
    due: dt.date | None = None
    links: list[AgentTaskLink] = Field(default_factory=list)
    notes: str
    position: int
    rank: float = 0
    created_at: dt.datetime
    updated_at: dt.datetime

    @field_validator("idempotency_key", mode="before")
    @classmethod
    def _null_key_is_blank(cls, v):
        return v or ""


class TaskCountOut(StrictModel):
    count: int


class AgentTaskPatch(StrictModel):
    """Partial update — only the fields sent are written."""

    #: `""` takes the task OUT of its project; omitting it leaves the task where
    #: it is. The two must differ, or patching a title would silently unfile it.
    project: str | None = Field(default=None, max_length=64)
    #: The parent task's `ext_id`; `""` makes it top-level again. Moving a
    #: parent's project moves its subtasks with it.
    parent: str | None = Field(default=None, max_length=64)
    #: `idea` needs at least one `human_why` quote (sent here or already on it).
    kind: Literal["work", "idea"] | None = None
    drive_folder_id: str | None = Field(default=None, max_length=128)
    drive_folder_url: str | None = Field(default=None, max_length=500)
    #: Who the next step waits on, by email. `""` clears it. A person canopy
    #: does not know is refused rather than silently dropped — the free-text
    #: `assigned` is where an unknown counterpart belongs, and a wait that
    #: looks routed but reaches nobody is the failure this field exists to end.
    waiting_on_email: str | None = Field(default=None, max_length=254)
    #: Who does it: a member's email, an agent's slug (`agent:<slug>`), or `""`
    #: for nobody. Giving it to an agent starts nothing; approving does.
    assignee: str | None = Field(default=None, max_length=254)
    title: str | None = Field(default=None, max_length=300)
    #: REPLACES the list (send every quote you want kept); `[]` clears it.
    human_why: list[HumanWhyIn] | None = Field(default=None, max_length=20)
    next_action: str | None = Field(default=None, max_length=300)
    status: str | None = None
    owner: str | None = Field(default=None, max_length=120)
    assigned: str | None = Field(default=None, max_length=120)
    confidence: str | None = Field(default=None, max_length=10)
    score: str | None = Field(default=None, max_length=8)
    review: str | None = None
    rationale: str | None = None
    source_url: str | None = Field(default=None, max_length=500)
    plan: str | None = None
    due: dt.date | None = None
    notes: str | None = None
    position: int | None = None
    links: list[AgentTaskLink] | None = None
    priority: int | None = Field(default=None, ge=0, le=4)
    #: REPLACES its labels (names; unknown ones are made). `[]` clears them.
    labels: list[str] | None = Field(default=None, max_length=20)
    #: A milestone of its project, by name or id; `""` clears it.
    milestone: str | None = Field(default=None, max_length=200)
    #: Out of Triage until then; null wakes it.
    snoozed_until: dt.datetime | None = None
    rank: float | None = None
    #: Place it between two tasks — what a drag sends: `before` is the key of the
    #: task just ABOVE where it lands, `after` the one just below (either may be
    #: omitted at the top or bottom of the list).
    before: str | None = Field(default=None, max_length=24)
    after: str | None = Field(default=None, max_length=24)


# ---- actions: everything a person does TO a task ----
class AgentTaskActionIn(StrictModel):
    #: approve (start now) · decline · reply (a reply needs a comment) · nudge ·
    #: done · accept (→ todo) · backlog · snooze (comment: a date, default a
    #: week) · duplicate (comment: the key it duplicates). Field changes are a
    #: PATCH, not an action.
    action: Literal["approve", "decline", "reply", "nudge", "done", "accept", "backlog", "snooze",
                    "duplicate"]
    comment: str = ""


class AgentTaskActionOut(StrictModel):
    id: int
    agent_slug: str | None = None
    task_key: str = ""
    task_ext_id: str
    action: str
    comment: str
    by: str
    #: `pending` until the agent has carried it out (its queue), then `applied`.
    status: str
    applied_at: dt.datetime | None = None
    result_note: str
    created_at: dt.datetime


class AgentTaskDetailOut(AgentTaskOut):
    #: Everything done to the task, newest first — "who approved this and why"
    #: is the closing row.
    actions: list[AgentTaskActionOut] = Field(default_factory=list)


class ActOut(StrictModel):
    task: AgentTaskOut
    action: AgentTaskActionOut
    #: Turns the action started (an approve or answer running `on_approve`).
    turn_ids: list[uuid.UUID] = Field(default_factory=list)


class ActionAppliedIn(StrictModel):
    result_note: str = ""


class TurnBriefOut(StrictModel):
    id: uuid.UUID
    status: str
    #: The turn's reported title if it has one, else the first 200 characters of
    #: its prompt. When the caller may not read the turn's content
    #: (`turn_access`), the prompt is blanked, so this is the title or empty.
    prompt_preview: str = ""
    created_at: dt.datetime
    task_ext_ids: list[str] = Field(default_factory=list)


class ProjectParticipantOut(StrictModel):
    """A person taking part in the project (fleet brain v1.1)."""

    id: int = Field(description="The person's id — `GET /api/people/{id}/?workspace=`.")
    display_name: str
    email: str = ""
    role: str = Field(default="", description="Free text: the part they play. Blank when canopy linked them itself.")
    source: Literal["fact", "turn", "manual", "summary", "review", "backing", "reply", "checkin"] = Field(
        description="How canopy first learned it: a fact filed against the project, a conversation "
                    "that raised one of its tasks, a person adding them, an agent writing their "
                    "contributor page, a storyboard comment, backing it, or replying on one of its tasks.")
    since: dt.datetime


# ---- proposed projects: the decision, the backings (apps/agents/proposals.py) ----


BackingStance = Literal["backs", "backs_with_changes", "passes"]


class ProjectBackerOut(StrictModel):
    kind: Literal["person", "agent"]
    #: The agent's slug when an agent backs; blank for a person.
    agent: str = ""
    person_id: int | None = None
    name: str
    email: str = ""


class ProjectBackingOut(StrictModel):
    backer: ProjectBackerOut
    stance: BackingStance
    line: str = ""
    #: Where it was given: web, mcp, slack, email — or huddle, a co-sign a huddle filed.
    source: str = "web"
    huddle_id: str = ""
    #: They backed an earlier version: the project has been revised since.
    stale: bool = False
    #: The reader's own backing.
    mine: bool = False
    updated_at: dt.datetime


class ProjectBackingIn(StrictModel):
    stance: BackingStance
    #: One line, required unless the stance is `backs`: what to change, or why you pass.
    line: str = Field(default="", max_length=280)


class AgentBackingIn(ProjectBackingIn):
    #: The huddle whose round-3 answer this co-sign records, if any.
    huddle_id: str = Field(default="", max_length=64)


class ProposalCommentOut(StrictModel):
    """A reply on the decision card — the project's comment thread."""

    by: str
    by_agent: bool = False
    comment: str
    at: dt.datetime


class ProposalPartnerOut(StrictModel):
    """A partner's linked task: in the project, assigned to the partner."""

    agent: str
    ext_id: str
    key: str = ""
    task_id: int
    status: str
    url: str


class ProposedProjectOut(StrictModel):
    """A proposed (or once-proposed) project as both the project page and the
    huddle page render it: what it is, its decision and whether the reader may
    make it, where people stand, what they said, and its partners."""

    id: int
    agent: str
    workspace: str
    ref: str
    name: str
    status: Literal["proposed", "planned", "active", "paused", "done", "canceled", "archived"]
    outcome: str
    why: str = ""
    plan: list[str] = Field(default_factory=list)
    #: The project page (app-relative).
    url: str
    #: The page that proposed it — a huddle, a thread.
    source_url: str = ""
    proposed_at: dt.datetime | None = None
    #: When it is archived as "not taken up" if nobody starts or declines it.
    expires_at: dt.datetime | None = None
    owner_email: str | None = None
    #: The project's notes — where a decline's reason is kept.
    notes: str = ""
    #: The task it grew out of ("from <task> in <project>"), if it did.
    spawned_from: SpawnedFromOut | None = None
    #: The decision card ("Start this project?"): act on it with `POST …/tasks/{ref}/actions`.
    decision_task: str | None = None
    decision_task_id: int | None = None
    decision_open: bool = False
    #: The reader may approve or decline it (a workspace owner or admin, or the project's owner).
    may_decide: bool = False
    backings: list[ProjectBackingOut] = Field(default_factory=list)
    comments: list[ProposalCommentOut] = Field(default_factory=list)
    partners: list[ProposalPartnerOut] = Field(default_factory=list)


class ProjectProposeIn(StrictModel):
    """Create or revise a proposed project (`POST …/projects/propose/`)."""

    name: str = Field(min_length=1, max_length=200)
    #: What done looks like.
    outcome: str = Field(min_length=1)
    why: str = Field(min_length=1)
    #: The plan's steps, in order — each becomes a `suggested` task in the project.
    plan: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=20)
    #: Agent slugs that would work with the lead; each gets a linked task on its own board.
    partners: list[str] = Field(default_factory=list, max_length=10)
    #: The page that proposed it — a huddle, a thread.
    source_url: str = Field(default="", max_length=500)
    owner_email: str = Field(default="", max_length=254)
    #: Required to bring back a project of this name that was declined.
    new_evidence: str = ""
    #: The task this project grows out of, as `<agent>/<task ext_id>` — a check-in's
    #: proposed task that turned out to be a project's worth of work.
    from_task: str = Field(default="", max_length=130)
    #: The check-in this came from (its id), recorded on the `proposed` event.
    checkin: str = Field(default="", max_length=64)
    #: Deprecated (work groups are gone): accepted and ignored for one release.
    group: str = Field(default="", max_length=64)


class ProjectProposeOut(StrictModel):
    #: False when an open proposed project of that name was revised instead.
    created: bool
    project: ProposedProjectOut


class SpawnedProjectOut(StrictModel):
    """A project a task grew into: "spawned → <project>"."""

    agent: str
    ref: str
    name: str
    status: str
    url: str


class ProposedTaskOut(StrictModel):
    """A check-in's proposed task as both the project page and the huddle page
    render it: what it is, its decision (Start = approve, Decline = decline, the
    task's own actions) and whether the reader may make it, where people stand,
    what they said, its partners and any project it spawned."""

    id: int
    #: Its assignee agent ("" when none).
    agent: str
    workspace: str
    #: Its agent alias, else its key.
    ref: str
    #: Act on it with `POST /api/tasks/{key}/actions`.
    key: str = ""
    title: str
    status: Literal["suggested", "backlog", "todo", "in_progress", "done", "declined"]
    why: str = ""
    done_when: str = ""
    plan: list[str] = Field(default_factory=list)
    project_ref: str = ""
    project_name: str = ""
    project_url: str = ""
    #: The page to open it on (its project's, app-relative).
    url: str
    #: The page that proposed it — a huddle.
    source_url: str = ""
    checkin: int | None = None
    run: int | None = None
    huddle_id: str = ""
    proposed_at: dt.datetime | None = None
    #: When it is declined as "not taken up" if nobody starts or declines it.
    expires_at: dt.datetime | None = None
    #: The task's notes — where a decline's reason is kept.
    notes: str = ""
    decision_open: bool = False
    #: The reader may start or decline it (the project's owner, the check-in's
    #: lead, or a workspace owner or admin).
    may_decide: bool = False
    backings: list[ProjectBackingOut] = Field(default_factory=list)
    comments: list[ProposalCommentOut] = Field(default_factory=list)
    partners: list[ProposalPartnerOut] = Field(default_factory=list)
    spawned: list[SpawnedProjectOut] = Field(default_factory=list)


class TaskProposeIn(StrictModel):
    """Create or revise a proposed task in a project (`POST …/projects/{ref}/tasks/propose/`)."""

    title: str = Field(min_length=1, max_length=300)
    why: str = Field(min_length=1)
    #: What done looks like.
    done_when: str = Field(min_length=1)
    #: The plan's steps, in order — each becomes a `suggested` child task.
    plan: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=20)
    #: Agent slugs that would work with the project's agent; each gets a linked task on its own board.
    partners: list[str] = Field(default_factory=list, max_length=10)
    #: The page that proposed it — a huddle.
    source_url: str = Field(default="", max_length=500)
    #: The check-in run it came from (its id), if any — it must be this project's.
    run: int | None = None
    #: Required to bring back a title that was declined in this project.
    new_evidence: str = ""


class TaskProposeOut(StrictModel):
    #: False when an open proposed task of that title was revised instead.
    created: bool
    task: ProposedTaskOut


class AgentProjectDetailOut(AgentProjectOut):
    #: The project's tasks, live ones first.
    tasks: list[AgentTaskOut] = Field(default_factory=list)
    #: The people the work involves, newest first (fleet brain v1.1).
    participants: list[ProjectParticipantOut] = Field(default_factory=list)
    #: The agent's latest turns that worked on any of those tasks.
    recent_turns: list[TurnBriefOut] = Field(default_factory=list)
    #: Set when the project was proposed (it has a decision card): the card the
    #: page renders, the same one the huddle page shows.
    proposal: ProposedProjectOut | None = None
    #: The tasks its check-ins proposed that wait for Start or Decline, each as
    #: the huddle page shows it.
    proposed_tasks: list[ProposedTaskOut] = Field(default_factory=list)
    #: Can the caller read its workspace's board? Always true for a member.
    board_readable: bool = True
    #: Tasks by category, and the scope-versus-done graph's points.
    progress: ProjectProgressOut | None = None
    milestones: list[MilestoneOut] = Field(default_factory=list)
    #: Its latest published update, and any drafts waiting for a person.
    latest_update: ProjectUpdateOut | None = None
    draft_updates: list[ProjectUpdateOut] = Field(default_factory=list)
    #: The reader may change it (an editor, its lead, or one of its agents).
    may_edit: bool = False
    #: What it serves to the workspace.
    services: list[ProjectServiceOut] = Field(default_factory=list)
    #: The workspace vaults attached to it, only those in a workspace the reader belongs to.
    vaults: list[ProjectVaultOut] = Field(default_factory=list)


class TaskBriefOut(StrictModel):
    """Another task, as a line in this one (a relation, its parent)."""

    key: str
    title: str
    status: str
    category: str = ""
    url: str = ""


class TaskRelationOut(StrictModel):
    #: blocks · blocked_by · related · duplicate_of · duplicated_by.
    kind: str
    task: TaskBriefOut


class TaskEventOut(StrictModel):
    """One entry in the task's history."""

    id: int
    #: created · field · action · relation.
    kind: str
    #: For `field`: which field. For `action`: the action. For `relation`: its kind.
    field: str = ""
    old: str = ""
    new: str = ""
    #: An action's comment — a reply is the comment thread.
    comment: str = ""
    by_email: str | None = None
    by_name: str = ""
    by_agent: str | None = None
    created_at: dt.datetime


class TaskTurnOut(StrictModel):
    """The assignee agent's latest turn on this task, and its state as every
    channel renders it (`harness.turn_status`)."""

    id: uuid.UUID
    status: str
    state: str
    created_at: dt.datetime
    #: Its page (the turn log).
    url: str = ""


class TaskDetailOut(AgentTaskOut):
    actions: list[AgentTaskActionOut] = Field(default_factory=list)
    events: list[TaskEventOut] = Field(default_factory=list)
    relations: list[TaskRelationOut] = Field(default_factory=list)
    subtasks: list[AgentTaskOut] = Field(default_factory=list)
    parent: TaskBriefOut | None = None
    turn: TaskTurnOut | None = None
    #: Blocked by a task that is still open.
    blocked: bool = False
    #: The reader may change its fields.
    may_edit: bool = False
    #: category -> how this workspace names it.
    status_names: dict[str, str] = Field(default_factory=dict)


class TaskCreateIn(AgentTaskIn):
    """One task made in a workspace (`POST /api/tasks/`): `workspace` (default:
    the project's, else the one you are pinned to, else your only one)."""

    workspace: str = Field(default="", max_length=64)


class TaskRelationIn(StrictModel):
    #: blocks · blocked_by · related · duplicate.
    kind: Literal["blocks", "blocked_by", "related", "duplicate"]
    #: The other task's key.
    other: str = Field(min_length=3, max_length=24)


class BulkTaskIn(StrictModel):
    """One change to many tasks: a PATCH, or an action, or both (the PATCH first)."""

    keys: list[str] = Field(min_length=1, max_length=200)
    patch: AgentTaskPatch | None = None
    action: AgentTaskActionIn | None = None


class BulkTaskResultOut(StrictModel):
    key: str
    ok: bool
    #: Why it was not changed (a 403/404/409/422 detail).
    error: str = ""
    task: AgentTaskOut | None = None


class TriageOut(StrictModel):
    """One thing waiting for a decision: a suggested task (`kind` task or idea)
    or a proposed project."""

    #: task · idea · project.
    kind: str
    #: A task's key, or a project's address.
    ref: str
    title: str
    why: str = ""
    #: Who proposed it: an agent's slug, or a person's email.
    proposed_by: str = ""
    workspace: str
    project_address: str | None = None
    project_name: str | None = None
    url: str
    created_at: dt.datetime
    #: How many back it / pass, from its backings.
    backers: int = 0
    passes: int = 0
    ask_kind: str = ""
    confidence: str = ""
    priority: int = 0
    human_why: list[HumanWhyOut] = Field(default_factory=list)


class LabelOut(StrictModel):
    id: int
    workspace: str = Field(validation_alias=AliasChoices("workspace_id", "workspace"))
    name: str
    color: str
    description: str = ""
    task_count: int = 0


class LabelIn(StrictModel):
    workspace: str = Field(default="", max_length=64)
    name: str = Field(min_length=1, max_length=60)
    #: muted · primary · success · warning · destructive · info.
    color: str = "muted"
    description: str = Field(default="", max_length=200)


class LabelPatch(StrictModel):
    name: str | None = Field(default=None, max_length=60)
    color: str | None = None
    description: str | None = Field(default=None, max_length=200)


class SavedViewOut(StrictModel):
    id: int
    workspace: str = Field(validation_alias=AliasChoices("workspace_id", "workspace"))
    name: str
    shared: bool
    #: Yours (you may change it), or shared by someone else.
    mine: bool = False
    owner_email: str | None = None
    filters: dict[str, str] = Field(default_factory=dict)
    group_by: str = ""
    sort: str = ""
    layout: str = "table"
    rank: float = 0


class SavedViewIn(StrictModel):
    workspace: str = Field(default="", max_length=64)
    name: str = Field(min_length=1, max_length=100)
    shared: bool = False
    filters: dict[str, str] = Field(default_factory=dict)
    group_by: str = Field(default="", max_length=20)
    sort: str = Field(default="", max_length=20)
    layout: Literal["table", "board", "calendar"] = "table"


class SavedViewPatch(StrictModel):
    name: str | None = Field(default=None, max_length=100)
    shared: bool | None = None
    filters: dict[str, str] | None = None
    group_by: str | None = Field(default=None, max_length=20)
    sort: str | None = Field(default=None, max_length=20)
    layout: Literal["table", "board", "calendar"] | None = None
    rank: float | None = None


class TaskSettingsOut(StrictModel):
    workspace: str = Field(validation_alias=AliasChoices("workspace_id", "workspace"))
    prefix: str
    #: category -> name, every category (defaults filled in).
    status_names: dict[str, str] = Field(default_factory=dict)
    update_cadence_days: int = 7


class TaskSettingsPatch(StrictModel):
    #: Only while no task of the workspace carries the current one.
    prefix: str | None = Field(default=None, max_length=10)
    #: category -> name; a blank name restores the default.
    status_names: dict[str, str] | None = None
    update_cadence_days: int | None = Field(default=None, ge=1, le=90)


class MilestoneOut(StrictModel):
    id: int
    name: str
    description: str = ""
    target_date: dt.date | None = None
    rank: float = 0
    done: int = 0
    total: int = 0


class MilestoneIn(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    target_date: dt.date | None = None


class MilestonePatch(StrictModel):
    name: str | None = Field(default=None, max_length=200)
    description: str | None = None
    target_date: dt.date | None = None
    rank: float | None = None


class ProjectUpdateOut(StrictModel):
    id: int
    #: on_track · at_risk · off_track.
    health: str
    body: str
    #: draft (an agent's, waiting for a person) · published.
    status: str
    author_email: str | None = None
    author_agent: str | None = Field(default=None, validation_alias=AliasChoices("author_agent_slug", "author_agent"))
    published_by_email: str | None = None
    checkin_run: int | None = Field(default=None, validation_alias=AliasChoices("checkin_run_id", "checkin_run"))
    diff: dict = Field(default_factory=dict)
    published_at: dt.datetime | None = None
    created_at: dt.datetime


class ProjectUpdateIn(StrictModel):
    health: Literal["on_track", "at_risk", "off_track"]
    body: str = Field(default="", max_length=10000)
    #: A person's update is published at once unless `draft`; an agent's (its
    #: own login) is always a draft a person publishes.
    draft: bool = False
    #: The check-in run it reports on, if any.
    checkin_run: int | None = None


class ProjectUpdatePublishIn(StrictModel):
    #: The health as the person publishing it judges it (default: the draft's).
    health: Literal["on_track", "at_risk", "off_track"] | None = None
    body: str | None = Field(default=None, max_length=10000)


class ProjectServiceOut(StrictModel):
    """Something a project serves to its workspace: its knowledge base, a shared
    MCP server, a skill, a plugin."""

    id: int
    #: kb · mcp · skill · plugin.
    kind: str
    name: str
    description: str = ""
    #: An MCP server's URL; a skill's or plugin's repository. For a knowledge base,
    #: the project's own MCP server path (empty until it publishes).
    url: str = ""
    #: How to add it to Claude Code, where there is one line for that.
    add_command: str = ""
    #: A skill's instructions: what an agent reads when it uses the skill.
    instructions: str = ""
    updated_at: dt.datetime


class ProjectVaultOut(StrictModel):
    """A workspace vault attached to a project: names only, never a value. Its member agents
    that lie in the vault's tree read it in their turns."""

    vault_id: str
    workspace: str
    name: str
    #: Secret NAMES the vault holds.
    secrets: list[str] = []
    #: Member agents served now, and member agents not served (outside the vault's tree).
    served: list[str] = []
    not_served: list[str] = []
    #: The project itself lies outside the vault's tree, so it serves nobody.
    project_outside_tree: bool = False


class ProjectServiceIn(StrictModel):
    #: mcp · skill · plugin (a knowledge base is published through `…/kb/`).
    kind: Literal["mcp", "skill", "plugin"]
    name: str = Field(min_length=1, max_length=80)
    #: An MCP server's or plugin's address (required for those); for a skill,
    #: where its instructions came from (optional when it has them).
    url: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=2000)
    #: A skill's instructions (its SKILL.md body).
    instructions: str = Field(default="", max_length=50000)


class ProjectServicePatch(StrictModel):
    name: str | None = Field(default=None, max_length=80)
    url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    instructions: str | None = Field(default=None, max_length=50000)


class ProjectProgressOut(StrictModel):
    total: int = 0
    done: int = 0
    started: int = 0
    #: Weekly points for the scope-versus-done graph, oldest first.
    scope_history: list[dict] = Field(default_factory=list)


AgentProjectDetailOut.model_rebuild()
AgentDetailOut.model_rebuild()


# ---- The access list (docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md) ----
