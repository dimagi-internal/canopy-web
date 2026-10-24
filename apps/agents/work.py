"""The workspace's work: task keys, history, ranks, labels, relations,
milestones, project updates and saved views.

`services` keeps the task and project writers; this module holds what they
share and the planning objects around them (design
`docs/superpowers/specs/2026-10-24-workspace-tasks-and-projects-design.md`).
"""
from __future__ import annotations

import datetime as dt
import re

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Q, prefetch_related_objects
from django.utils import timezone

from .models import (
    AgentProject,
    AgentTask,
    Label,
    ProjectMilestone,
    ProjectService,
    ProjectUpdate,
    SavedView,
    TaskEvent,
    TaskRelation,
    TaskSettings,
)

# ---- keys -------------------------------------------------------------------

PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]{1,9})-(\d+)$")

#: How each category is shown when a workspace names it nothing of its own.
DEFAULT_STATUS_NAMES = {"triage": "Triage", "backlog": "Backlog", "unstarted": "Todo",
                        "started": "In progress", "completed": "Done", "canceled": "Canceled"}


def derive_prefix(slug: str, taken: set[str]) -> str:
    """`dimagi` -> `DIM`: the first three letters of the slug, longer (then
    numbered) when another workspace already has it. Migration 0084 freezes a
    copy of this rule."""
    letters = re.sub(r"[^A-Za-z0-9]", "", slug).upper() or "WS"
    base = letters[:3] if len(letters) >= 3 else (letters + "XXX")[:3]
    if base not in taken:
        return base
    for n in range(4, len(letters) + 1):
        if letters[:n] not in taken:
            return letters[:n]
    i = 2
    while f"{base}{i}" in taken:
        i += 1
    return f"{base}{i}"


def settings_for(workspace_slug: str) -> TaskSettings:
    """The workspace's task settings, made on first use."""
    found = TaskSettings.objects.filter(workspace_id=workspace_slug).first()
    if found is not None:
        return found
    taken = set(TaskSettings.objects.values_list("prefix", flat=True))
    try:
        with transaction.atomic():
            return TaskSettings.objects.create(workspace_id=workspace_slug,
                                               prefix=derive_prefix(workspace_slug, taken))
    except IntegrityError:  # a concurrent first use made it, or took the prefix
        found = TaskSettings.objects.filter(workspace_id=workspace_slug).first()
        if found is not None:
            return found
        taken = set(TaskSettings.objects.values_list("prefix", flat=True))
        return TaskSettings.objects.create(workspace_id=workspace_slug,
                                           prefix=derive_prefix(workspace_slug, taken))


def allocate_number(workspace_slug: str) -> tuple[int, str]:
    """The next (number, key) in the workspace: one UPDATE, so two creates at
    once cannot collide, and the counter never goes back."""
    ts = settings_for(workspace_slug)
    TaskSettings.objects.filter(pk=ts.pk).update(last_number=F("last_number") + 1)
    ts.refresh_from_db(fields=["last_number"])
    return ts.last_number, f"{ts.prefix}-{ts.last_number}"


def rehome(task: AgentTask, workspace_slug: str, *, by_user=None, by_agent=None) -> AgentTask:
    """Move a task to another workspace (its project moved there). It takes a
    number and key there; its old key still finds it (`task_by_key`), through
    the `key` event this writes."""
    old_key = task.key
    task.workspace_id = workspace_slug
    task.number, task.key = allocate_number(workspace_slug)
    task.save(update_fields=["workspace", "number", "key", "updated_at"])
    TaskEvent.objects.create(task=task, kind=TaskEvent.FIELD, field="key", old=old_key, new=task.key,
                             **_by(by_user, by_agent))
    return task


def looks_like_key(ref: str) -> bool:
    return bool(KEY_RE.match((ref or "").strip()))


def task_by_key(key: str) -> AgentTask | None:
    key = (key or "").strip().upper()
    if not looks_like_key(key):
        return None
    qs = AgentTask.objects.select_related("agent", "project", "workspace", "assignee_user")
    found = qs.filter(key=key).first()
    if found is None:  # a key it had before it moved workspace
        moved = (TaskEvent.objects.filter(kind=TaskEvent.FIELD, field="key", old=key)
                 .order_by("-id").values_list("task_id", flat=True).first())
        found = qs.filter(pk=moved).first() if moved else None
    return found


def status_names(workspace_slug: str) -> dict[str, str]:
    """category -> name as this workspace shows it."""
    ts = TaskSettings.objects.filter(workspace_id=workspace_slug).first()
    return {**DEFAULT_STATUS_NAMES, **((ts.status_names if ts else None) or {})}


def set_settings(workspace_slug: str, data: dict) -> TaskSettings:
    """Change a workspace's prefix (only while no task carries it), status names
    or update cadence. Raises ValueError on a bad value."""
    ts = settings_for(workspace_slug)
    if "prefix" in data and data["prefix"] is not None:
        prefix = data["prefix"].strip().upper()
        if prefix != ts.prefix:
            if not PREFIX_RE.match(prefix):
                raise ValueError("prefix must be 2-10 capital letters or digits, starting with a letter")
            if AgentTask.objects.filter(workspace_id=workspace_slug).exists():
                raise ValueError(f"tasks already carry {ts.prefix}-…; a prefix is frozen once used")
            if TaskSettings.objects.filter(prefix=prefix).exclude(pk=ts.pk).exists():
                raise ValueError(f"{prefix} is another workspace's prefix")
            ts.prefix = prefix
    if data.get("status_names") is not None:
        names = {}
        for cat, name in data["status_names"].items():
            if cat not in DEFAULT_STATUS_NAMES:
                raise ValueError(f"unknown status category {cat!r}")
            name = (name or "").strip()
            if len(name) > 30:
                raise ValueError("a status name is at most 30 characters")
            if name and name != DEFAULT_STATUS_NAMES[cat]:
                names[cat] = name
        ts.status_names = names
    if data.get("update_cadence_days") is not None:
        days = int(data["update_cadence_days"])
        if not 1 <= days <= 90:
            raise ValueError("update_cadence_days must be 1-90")
        ts.update_cadence_days = days
    ts.save()
    return ts


# ---- list reads -------------------------------------------------------------


def task_rows(qs):
    """A task queryset joined up for the list shape (`AgentTaskOut`): one query
    for the rows and one per prefetched relation, never one per task."""
    return (qs.select_related("agent", "project", "waiting_on_user", "parent", "assignee_user",
                              "created_by_agent", "milestone")
            .prefetch_related("human_whys", "labels"))


def annotate_projects(projects: list[AgentProject]) -> list[AgentProject]:
    """Cache every list figure on each project (`AgentProjectOut`) in a fixed
    number of queries: task counts, done count, health, update_missing and its
    member agents."""
    from .services import LIVE_STATUSES, waiting_q  # noqa: PLC0415 — services imports work

    if not projects:
        return projects
    prefetch_related_objects(projects, "served")
    ids = [p.pk for p in projects]
    counts = {r["project_id"]: r for r in AgentTask.objects.filter(project_id__in=ids).values("project_id")
              .annotate(n=Count("id"), live=Count("id", filter=Q(status__in=LIVE_STATUSES)),
                        waiting=Count("id", filter=waiting_q()),
                        done=Count("id", filter=Q(status=AgentTask.DONE)))}
    latest: dict[int, ProjectUpdate] = {}
    for u in (ProjectUpdate.objects.filter(project_id__in=ids, status=ProjectUpdate.PUBLISHED)
              .order_by("project_id", "-published_at", "-id")):
        latest.setdefault(u.project_id, u)
    cadence = dict(TaskSettings.objects.filter(workspace_id__in={p.workspace_id for p in projects})
                   .values_list("workspace_id", "update_cadence_days"))
    members: dict[int, list] = {}
    through = AgentProject.member_agents.through
    for pid, slug in through.objects.filter(agentproject_id__in=ids).values_list("agentproject_id", "agent__slug"):
        members.setdefault(pid, []).append(slug)
    for p in projects:
        c = counts.get(p.pk, {})
        p._task_count, p._open_task_count = c.get("n", 0), c.get("live", 0)
        p._waiting_task_count, p._done_task_count = c.get("waiting", 0), c.get("done", 0)
        lu = latest.get(p.pk)
        p._health = lu.health if lu else None
        p._update_missing = update_missing(p, lu, cadence_days=cadence.get(p.workspace_id, 7))
        p._member_slugs = sorted(members.get(p.pk, []))
    return projects


# ---- history ----------------------------------------------------------------

#: The fields a PATCH records, as shown.
TRACKED_FIELDS = ("title", "status", "assignee", "project", "parent", "priority", "due", "milestone",
                  "labels", "next_action", "kind", "waiting_on")


def snapshot(task: AgentTask) -> dict[str, str]:
    """The tracked fields as shown — what a `field` event's old/new hold."""
    return {
        "title": task.title,
        "status": task.status,
        "assignee": (f"agent:{task.agent.slug}" if task.agent_id else
                     (task.assignee_user.email if task.assignee_user_id else "")),
        "project": task.project.address if task.project_id else "",
        "parent": task.parent.key if task.parent_id else "",
        "priority": str(task.priority or 0),
        "due": task.due.isoformat() if task.due else "",
        "milestone": task.milestone.name if task.milestone_id else "",
        "labels": ",".join(sorted(task.labels.values_list("name", flat=True))) if task.pk else "",
        "next_action": task.next_action,
        "kind": task.kind,
        "waiting_on": task.waiting_on_user.email if task.waiting_on_user_id else "",
    }


def _by(by_user=None, by_agent=None) -> dict:
    user = by_user if getattr(by_user, "is_authenticated", False) else None
    return {"by_user": user, "by_agent": by_agent}


def record_created(task: AgentTask, *, by_user=None, by_agent=None) -> None:
    TaskEvent.objects.create(task=task, kind=TaskEvent.CREATED, **_by(by_user, by_agent))


def record_changes(task: AgentTask, before: dict[str, str], *, by_user=None, by_agent=None) -> int:
    """One `field` event per tracked field that changed since `before`."""
    after = snapshot(task)
    rows = [TaskEvent(task=task, kind=TaskEvent.FIELD, field=f, old=before.get(f, ""), new=after[f],
                      **_by(by_user, by_agent))
            for f in TRACKED_FIELDS if before.get(f, "") != after[f]]
    TaskEvent.objects.bulk_create(rows)
    return len(rows)


def record_action(task: AgentTask, action_row, *, by_user=None, by_agent=None) -> None:
    TaskEvent.objects.create(task=task, kind=TaskEvent.ACTION, action=action_row,
                             field=action_row.action, **_by(by_user, by_agent))


def events_of(task: AgentTask) -> list[TaskEvent]:
    return list(task.events.select_related("by_user", "by_agent", "action", "action__by_user"))


# ---- rank -------------------------------------------------------------------


def rank_between(before: float | None, after: float | None) -> float:
    """A rank strictly between two neighbours (either may be missing: the top
    or the bottom of the list)."""
    if before is None and after is None:
        return 0.0
    if before is None:
        return after - 1.0
    if after is None:
        return before + 1.0
    return (before + after) / 2.0


def move_task(task: AgentTask, *, before_key: str = "", after_key: str = "") -> AgentTask:
    """Put `task` between the tasks named (by key) — the one row a drag writes."""
    def rank_of(key):
        if not key:
            return None
        other = task_by_key(key)
        return other.rank if other is not None and other.workspace_id == task.workspace_id else None

    task.rank = rank_between(rank_of(before_key), rank_of(after_key))
    task.save(update_fields=["rank", "updated_at"])
    return task


def top_rank(workspace_slug: str) -> float:
    """A rank above every task of the workspace — where a new task goes."""
    first = AgentTask.objects.filter(workspace_id=workspace_slug).order_by("rank").values_list(
        "rank", flat=True).first()
    return (first - 1.0) if first is not None else 0.0


# ---- labels -----------------------------------------------------------------

LABEL_COLORS = ("muted", "primary", "success", "warning", "destructive", "info")


def labels_in(workspace_slugs) -> list[Label]:
    return list(Label.objects.filter(workspace_id__in=list(workspace_slugs))
                .annotate(task_count=Count("tasks")).order_by("name"))


def _clean_label(data: dict, label: Label) -> Label:
    if "name" in data and data["name"] is not None:
        name = data["name"].strip()
        if not name or len(name) > 60:
            raise ValueError("a label name is 1-60 characters")
        label.name = name
    if data.get("color") is not None:
        if data["color"] not in LABEL_COLORS:
            raise ValueError(f"color must be one of {', '.join(LABEL_COLORS)}")
        label.color = data["color"]
    if data.get("description") is not None:
        label.description = data["description"].strip()[:200]
    return label


def create_label(workspace_slug: str, data: dict) -> Label:
    label = _clean_label(data, Label(workspace_id=workspace_slug))
    try:
        with transaction.atomic():
            label.save()
    except IntegrityError:
        raise ValueError(f"there is already a label {label.name!r}") from None
    return label


def update_label(label: Label, data: dict) -> Label:
    label = _clean_label(data, label)
    try:
        with transaction.atomic():
            label.save()
    except IntegrityError:
        raise ValueError(f"there is already a label {label.name!r}") from None
    return label


def resolve_labels(workspace_slug: str, names: list[str], *, create: bool = False) -> list[Label]:
    """The workspace's labels by name (any case). Unknown names are refused,
    or made when `create`."""
    out = []
    for raw in names or []:
        name = (raw or "").strip()
        if not name:
            continue
        label = Label.objects.filter(workspace_id=workspace_slug, name__iexact=name).first()
        if label is None:
            if not create:
                raise ValueError(f"no label {name!r} in {workspace_slug}")
            label = create_label(workspace_slug, {"name": name})
        out.append(label)
    return out


# ---- relations --------------------------------------------------------------


def add_relation(task: AgentTask, kind: str, other: AgentTask, *, by_user=None, by_agent=None) -> TaskRelation:
    """`task` <kind> `other`. `blocked_by` is `blocks` the other way round."""
    if kind == "blocked_by":
        task, other, kind = other, task, TaskRelation.BLOCKS
    if kind not in TaskRelation.KINDS:
        raise ValueError(f"kind must be blocks|blocked_by|related|duplicate, got {kind!r}")
    if task.pk == other.pk:
        raise ValueError("a task cannot relate to itself")
    if task.workspace_id != other.workspace_id:
        raise ValueError("both tasks must be in the same workspace")
    if kind == TaskRelation.BLOCKS and TaskRelation.objects.filter(
            from_task=other, to_task=task, kind=TaskRelation.BLOCKS).exists():
        raise ValueError(f"{other.key} already blocks {task.key}; two tasks cannot block each other")
    rel, created = TaskRelation.objects.get_or_create(
        from_task=task, to_task=other, kind=kind,
        defaults={"created_by_user": by_user if getattr(by_user, "is_authenticated", False) else None})
    if created:
        for a, b in ((task, other), (other, task)):
            TaskEvent.objects.create(task=a, kind=TaskEvent.RELATION, field=kind, new=b.key,
                                     **_by(by_user, by_agent))
    return rel


def remove_relation(task: AgentTask, kind: str, other: AgentTask, *, by_user=None, by_agent=None) -> bool:
    if kind == "blocked_by":
        task, other, kind = other, task, TaskRelation.BLOCKS
    q = Q(from_task=task, to_task=other, kind=kind)
    if kind != TaskRelation.BLOCKS:  # symmetric kinds may be stored either way round
        q |= Q(from_task=other, to_task=task, kind=kind)
    deleted, _ = TaskRelation.objects.filter(q).delete()
    if deleted:
        for a, b in ((task, other), (other, task)):
            TaskEvent.objects.create(task=a, kind=TaskEvent.RELATION, field=kind, old=b.key,
                                     **_by(by_user, by_agent))
    return bool(deleted)


def relations_of(task: AgentTask) -> list[dict]:
    """Every relation, read from `task`'s side: blocks, blocked_by, related,
    duplicate_of, duplicated_by."""
    out = []
    for rel in task.relations_out.select_related("to_task"):
        kind = {"duplicate": "duplicate_of"}.get(rel.kind, rel.kind)
        out.append({"kind": kind, "task": rel.to_task})
    for rel in task.relations_in.select_related("from_task"):
        kind = {"blocks": "blocked_by", "duplicate": "duplicated_by"}.get(rel.kind, rel.kind)
        out.append({"kind": kind, "task": rel.from_task})
    return out


def is_blocked(task: AgentTask) -> bool:
    """Blocked by a task that is still open."""
    return task.relations_in.filter(kind=TaskRelation.BLOCKS,
                                    from_task__status__in=AgentTask.LIVE_STATUSES).exists()


# ---- milestones -------------------------------------------------------------


def milestones_of(project: AgentProject) -> list[ProjectMilestone]:
    """Its milestones, each with `done` and `total` task counts."""
    rows = list(project.milestones.annotate(
        total=Count("tasks", filter=~Q(tasks__status=AgentTask.DECLINED)),
        done=Count("tasks", filter=Q(tasks__status=AgentTask.DONE))))
    return rows


def save_milestone(project: AgentProject, data: dict, milestone: ProjectMilestone | None = None) -> ProjectMilestone:
    m = milestone or ProjectMilestone(project=project)
    if data.get("name") is not None:
        name = data["name"].strip()
        if not name:
            raise ValueError("a milestone needs a name")
        m.name = name[:200]
    elif milestone is None:
        raise ValueError("a milestone needs a name")
    if "description" in data and data["description"] is not None:
        m.description = data["description"]
    if "target_date" in data:
        m.target_date = data["target_date"]
    if data.get("rank") is not None:
        m.rank = float(data["rank"])
    elif milestone is None:
        last = project.milestones.order_by("-rank").values_list("rank", flat=True).first()
        m.rank = (last + 1.0) if last is not None else 0.0
    try:
        with transaction.atomic():
            m.save()
    except IntegrityError:
        raise ValueError(f"{project.name} already has a milestone {m.name!r}") from None
    return m


def resolve_milestone(project: AgentProject | None, name_or_id) -> ProjectMilestone | None:
    if name_or_id in (None, ""):
        return None
    if project is None:
        raise ValueError("a task outside a project has no milestones")
    ref = str(name_or_id).strip()
    m = (project.milestones.filter(pk=int(ref)).first() if ref.isdigit() else None) \
        or project.milestones.filter(name__iexact=ref).first()
    if m is None:
        raise ValueError(f"{project.name} has no milestone {ref!r}")
    return m


# ---- progress and health ----------------------------------------------------


def progress_of(project: AgentProject) -> dict:
    """`{total, done, started, scope_history}` — counted by category. Canceled
    tasks are out of scope. `scope_history` is weekly (scope, done) points for
    the scope-versus-done graph, oldest first, at most 12."""
    tasks = project.tasks.exclude(status=AgentTask.DECLINED)
    total = tasks.count()
    done = tasks.filter(status=AgentTask.DONE).count()
    started = tasks.filter(status=AgentTask.IN_PROGRESS).count()
    history = []
    created = list(project.tasks.values_list("created_at", "status", "updated_at"))
    if created:
        start = min(c for c, _, _ in created)
        now = timezone.now()
        weeks = max(1, min(12, (now - start).days // 7 + 1))
        for i in range(weeks):
            at = now - dt.timedelta(days=7 * (weeks - 1 - i))
            scope = sum(1 for c, s, _ in created if c <= at and s != AgentTask.DECLINED)
            closed = sum(1 for c, s, u in created if c <= at and s == AgentTask.DONE and u <= at)
            history.append({"at": at.date().isoformat(), "scope": scope, "done": closed})
    return {"total": total, "done": done, "started": started, "scope_history": history}


def latest_update(project: AgentProject) -> ProjectUpdate | None:
    return project.updates.filter(status=ProjectUpdate.PUBLISHED).order_by("-published_at", "-id").first()


def update_missing(project: AgentProject, latest: ProjectUpdate | None = None, *, cadence_days: int | None = None) -> bool:
    """An active project that has had no published update within its
    workspace's cadence (counted from when it started, if it never had one)."""
    if project.status != AgentProject.ACTIVE:
        return False
    if cadence_days is None:
        cadence_days = settings_for(project.workspace_id).update_cadence_days
    if latest is None and not hasattr(project, "_health"):
        latest = latest_update(project)
    since = (latest.published_at or latest.created_at) if latest is not None else project.created_at
    return timezone.now() - since > dt.timedelta(days=cadence_days)


def _diff_since(project: AgentProject, previous: ProjectUpdate | None) -> dict:
    since = previous.published_at if previous is not None else project.created_at
    tasks = project.tasks
    diff = {
        "tasks_done": tasks.filter(status=AgentTask.DONE, updated_at__gt=since).count(),
        "tasks_added": tasks.filter(created_at__gt=since).count(),
        "milestones": [{"name": m.name, "done": m.done, "total": m.total} for m in milestones_of(project)],
    }
    prev_target = (previous.diff or {}).get("target_date_now") if previous is not None else None
    now_target = project.target_date.isoformat() if project.target_date else None
    diff["target_date_now"] = now_target
    if previous is not None and prev_target != now_target:
        diff["target_date"] = [prev_target, now_target]
    return diff


def post_update(project: AgentProject, *, health: str, body: str = "", draft: bool = False,
                author_user=None, author_agent=None, checkin_run=None) -> ProjectUpdate:
    """A person's update is published at once; an agent's is a draft a person
    publishes (`publish_update`), choosing the health."""
    if health not in ProjectUpdate.HEALTHS:
        raise ValueError(f"health must be on_track|at_risk|off_track, got {health!r}")
    update = ProjectUpdate(project=project, health=health, body=(body or "").strip(),
                           author_user=author_user if getattr(author_user, "is_authenticated", False) else None,
                           author_agent=author_agent, checkin_run=checkin_run,
                           status=ProjectUpdate.DRAFT if (draft or author_agent is not None) else ProjectUpdate.PUBLISHED)
    if update.status == ProjectUpdate.PUBLISHED:
        update.diff = _diff_since(project, latest_update(project))
        update.published_at = timezone.now()
        update.published_by = update.author_user
    update.save()
    return update


def publish_update(update: ProjectUpdate, *, by_user, health: str | None = None, body: str | None = None) -> ProjectUpdate:
    """A person publishes a draft, choosing its health (and editing its words)."""
    if update.status == ProjectUpdate.PUBLISHED:
        raise ValueError("already published")
    if health is not None:
        if health not in ProjectUpdate.HEALTHS:
            raise ValueError(f"health must be on_track|at_risk|off_track, got {health!r}")
        update.health = health
    if body is not None:
        update.body = body.strip()
    update.diff = _diff_since(update.project, latest_update(update.project))
    update.status = ProjectUpdate.PUBLISHED
    update.published_at = timezone.now()
    update.published_by = by_user
    update.save()
    return update


# ---- saved views ------------------------------------------------------------

#: Filter keys a saved view may hold — the Tasks page's URL query.
VIEW_FILTERS = ("tab", "assignee", "created_by", "project", "label", "priority", "milestone", "category",
                "status", "kind", "due", "q", "agent")


def views_for(user, workspace_slugs) -> list[SavedView]:
    """The user's own views and the shared ones of those workspaces."""
    return list(SavedView.objects.filter(workspace_id__in=list(workspace_slugs))
                .filter(Q(owner=user) | Q(shared=True)).select_related("owner").order_by("rank", "id"))


def save_view(view: SavedView, data: dict) -> SavedView:
    if data.get("name") is not None:
        name = data["name"].strip()
        if not name:
            raise ValueError("a view needs a name")
        view.name = name[:100]
    elif not view.name:
        raise ValueError("a view needs a name")
    if data.get("filters") is not None:
        bad = set(data["filters"]) - set(VIEW_FILTERS)
        if bad:
            raise ValueError(f"unknown filter(s): {', '.join(sorted(bad))}")
        view.filters = {k: str(v) for k, v in data["filters"].items() if v not in (None, "")}
    for f in ("group_by", "sort"):
        if data.get(f) is not None:
            setattr(view, f, data[f][:20])
    if data.get("layout") is not None:
        if data["layout"] not in SavedView.LAYOUTS:
            raise ValueError(f"layout must be one of {', '.join(SavedView.LAYOUTS)}")
        view.layout = data["layout"]
    if data.get("shared") is not None:
        view.shared = bool(data["shared"])
    if data.get("rank") is not None:
        view.rank = float(data["rank"])
    view.save()
    return view


# ---- what a project serves ----------------------------------------------------

#: A shared thing's name: what `claude mcp add` and a plugin list call it.
SERVICE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,78}[a-z0-9]$|^[a-z0-9]$")


def services_of(project: AgentProject, *, origin: str = "") -> list[dict]:
    """Everything the project serves, as `ProjectServiceOut` rows. `origin` (the
    site's base URL) completes a knowledge base's own MCP URL."""
    out = []
    for svc in project.served.all().order_by("kind", "name"):
        url, add = svc.url, ""
        if svc.kind == ProjectService.KB:
            url = f"{origin}{project.mcp_path}".rstrip("/") if project.mcp_path else ""
            add = f"claude mcp add --transport http {project.handle} {url}" if url else ""
        elif svc.kind == ProjectService.MCP:
            add = f"claude mcp add --transport http {svc.name} {svc.url}"
        elif svc.kind == ProjectService.PLUGIN:
            add = f"/plugin marketplace add {svc.url}"
        out.append({"id": svc.pk, "kind": svc.kind, "name": svc.name if svc.kind != ProjectService.KB else project.handle,
                    "description": svc.description, "url": url, "add_command": add, "updated_at": svc.updated_at})
    return out


def save_service(project: AgentProject, data: dict, service=None, *, by_user=None):
    """Declare (or change) a shared MCP server, skill or plugin on the project."""
    svc = service or ProjectService(project=project, kind=data["kind"],
                                    created_by_user=by_user if getattr(by_user, "is_authenticated", False) else None)
    if svc.kind == ProjectService.KB:
        raise ValueError("a knowledge base is published through its …/kb/ routes")
    if data.get("name") is not None:
        name = data["name"].strip().lower()
        if not SERVICE_NAME_RE.match(name):
            raise ValueError("a name is 1-80 characters of a-z, 0-9, '.', '_' and '-'")
        svc.name = name
    if data.get("url") is not None:
        url = data["url"].strip()
        if not url.startswith(("https://", "http://")):
            raise ValueError("url must be an http(s) address")
        svc.url = url
    if data.get("description") is not None:
        svc.description = data["description"].strip()
    try:
        with transaction.atomic():
            svc.save()
    except IntegrityError:
        raise ValueError(f"{project.name} already serves a {svc.kind} named {svc.name!r}") from None
    return svc
