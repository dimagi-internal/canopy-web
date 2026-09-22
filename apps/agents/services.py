"""Business logic for the agent workspace — kept out of the Ninja router so it's
unit-testable without HTTP."""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.harness.models import Turn

from .models import (
    Agent,
    AgentProject,
    AgentSkill,
    AgentSync,
    AgentTask,
    AgentTaskCommand,
    AgentWorkProduct,
)

_VALID_TASK_STATUS = {AgentTask.SUGGESTED, AgentTask.IN_PROGRESS, AgentTask.DONE, AgentTask.DECLINED}


def _aware(value):
    if isinstance(value, dt.datetime) and timezone.is_naive(value):
        return value.replace(tzinfo=dt.UTC)
    return value


# ---- agents ----
def upsert_agent(data, *, workspace) -> Agent:
    """Create or update an agent by slug.

    `workspace` is REQUIRED and keyword-only: `Agent.workspace` is NOT NULL
    (agents/0013), so there is no such thing as creating an agent and homing it
    afterwards. Making the caller supply the tenant up front is the point — the
    old shape created the row unhomed and left the view to home it a few lines
    later, which is how a workspace-less agent was ever representable.

    It is applied on CREATE only (`create_defaults`), never on update: an
    upsert must not silently move an existing agent between tenants. Moving one
    is an explicit, membership-gated action in the view.
    """
    defaults = {
        "name": data.name,
        "description": data.description,
        "persona": data.persona,
        "email": data.email,
        "avatar_url": data.avatar_url,
    }
    # Runtime-registry fields (repo pointer / engine / secret refs) are written
    # ONLY when explicitly provided. The plugin re-upserts agents on every sync
    # with these fields absent (None) — a plain default would clobber runtime
    # config back to empty on each heartbeat. Configure once, keep it.
    for field in ("repo_url", "repo_ref", "runtime_engine", "runtime_secrets",
                  "runtime_sources", "runner_preference"):
        value = getattr(data, field, None)
        if value is not None:
            defaults[field] = value
    agent, _ = Agent.objects.update_or_create(
        slug=data.slug, defaults=defaults, create_defaults={**defaults, "workspace": workspace}
    )
    return agent


def list_agents() -> list[Agent]:
    return list(Agent.objects.all())


def get_agent(slug: str) -> Agent | None:
    return Agent.objects.filter(slug=slug).first()


def _definition_summary(agent: Agent) -> dict:
    """The repo this instance runs, and which other tenants run it.

    Cheap for the fleet's size (a handful of agents, compared in Python because
    the key needs normalising before it can be matched). If the fleet ever grows
    enough for that to matter, the fix is a stored normalised column — not a
    raw-URL match, which would silently under-report.
    """
    from .definition import definition_key, siblings

    return {
        "key": definition_key(agent.repo_url, agent.repo_ref),
        "repo_url": agent.repo_url,
        "repo_ref": agent.repo_ref,
        "shared_with": sorted({a.workspace_id for a in siblings(agent)}),
    }


def _owner_summary(agent: Agent) -> dict | None:
    owner = agent.owner
    if owner is None:
        return None
    return {"user_id": owner.pk, "name": owner.get_full_name() or owner.email, "email": owner.email}


def agent_detail(agent: Agent) -> dict:
    latest = agent.syncs.order_by("-period_end").first()
    return {
        "id": agent.id,
        "slug": agent.slug,
        "name": agent.name,
        "description": agent.description,
        "persona": agent.persona,
        "email": agent.email,
        "avatar_url": agent.avatar_url,
        "workspace_id": agent.workspace_id,
        "definition": _definition_summary(agent),
        "owner": _owner_summary(agent),
        "runner_preference": list(agent.runner_preference or []),
        "turn_mode": agent.turn_mode,
        "slack_enabled": agent.slack_enabled,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
        "sync_count": agent.syncs.count(),
        "work_product_count": agent.work_products.count(),
        "skill_count": agent.skills.count(),
        "task_count": agent.tasks.count(),
        "turn_count": agent.turns.count(),
        "latest_sync_at": latest.period_end if latest else None,
        # "When did this agent last RUN" — read off the dispatch queue, which has a
        # row for every turn the harness sent. It used to be read off the close-out
        # report, which only exists when an agent survived to package itself: ada
        # rendered as never-run (0 / null) against 34 dispatched turns, and every
        # other agent was weeks stale against its own queue.
        #
        # `started_at` (not created_at) is the honest answer — a queued turn nobody
        # picked up is not a run — with created_at as the fallback for rows that
        # predate the runner reporting a start.
        "latest_turn_at": _latest_turn_at(agent),
    }


def _latest_turn_at(agent: Agent):
    """Newest start among turns that actually started, else newest enqueue.

    Deliberately NOT `order_by("-started_at", "-created_at")`: Postgres sorts DESC
    NULLS FIRST, so a single queued turn (started_at IS NULL) would win and the
    fallback would become the answer for every agent with anything in the queue.
    """
    started = agent.turns.filter(started_at__isnull=False).order_by("-started_at").first()
    if started is not None:
        return started.started_at
    newest = agent.turns.order_by("-created_at").first()
    return newest.created_at if newest else None


# ---- syncs ----
def upsert_sync(agent: Agent, data) -> AgentSync:
    """Idempotent per (agent, period_start, period_end, source)."""
    period_start = _aware(data.period_start)
    period_end = _aware(data.period_end)
    AgentSync.objects.filter(
        agent=agent,
        period_start=period_start,
        period_end=period_end,
        source=data.source,
    ).delete()
    return AgentSync.objects.create(
        agent=agent,
        period_start=period_start,
        period_end=period_end,
        title=data.title,
        summary=data.summary,
        doc_url=data.doc_url,
        self_grades=data.self_grades,
        source=data.source,
    )


def list_syncs(agent: Agent, limit: int = 100) -> list[AgentSync]:
    return list(agent.syncs.select_related("agent")[:limit])


def delete_sync(agent: Agent, sync_id: int) -> bool:
    """Delete ONE sync by id, scoped to the agent. True if a row was removed.

    upsert_sync is idempotent per (period_start, period_end, source), so re-posting
    only ever corrects a sync for the SAME window. A sync posted with the wrong
    period (or a stray test row) is otherwise unreachable — this is the escape hatch.
    """
    deleted, _ = AgentSync.objects.filter(agent=agent, pk=sync_id).delete()
    return bool(deleted)


# ---- turns (a packaged unit of work + optional transcript link) ----
def _claim_dispatch_row(agent: Agent, data) -> Turn | None:
    """The dispatch row this close-out belongs to, or None to create a fresh one.

    Match order, most specific first:
      1. cli_session_id — a turn already reported from this Claude session (re-run
         of the close-out). Also what the unique constraint keys on.
      2. emdash_task_id — the runner stamped the emdash session it created; the
         closing agent recovers the same name from its cwd. Newest UNREPORTED turn
         for that task wins, because a reused session serves many turns and the one
         being closed is the latest.

    No time window on (2): an agent turn legitimately runs for hours, and a wrong
    window would silently split one turn into two rows — the exact failure this
    merge exists to end. The `reported_at__isnull=True` filter is what keeps an
    older turn from being claimed twice.
    """
    if data.cli_session_id:
        existing = agent.turns.filter(cli_session_id=data.cli_session_id).first()
        if existing is not None:
            return existing
    task = getattr(data, "emdash_task_id", "") or ""
    if task:
        return (
            agent.turns.filter(emdash_task_id=task, reported_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    return None


def upsert_turn(agent: Agent, data) -> Turn:
    """Attach an agent's close-out report to the turn it was dispatched as.

    Idempotent per (agent, cli_session_id). When no dispatch row can be matched —
    a turn a human started by hand in a terminal, or a fleet still posting without
    `emdash_task_id` — a report-only Turn is created instead, so the record is never
    dropped on the floor. That row carries origin=api and status=done because it is,
    from the harness's point of view, a turn that has already finished; it has no
    idempotency of its own to enforce, so the key is synthesized from the session.
    """
    fields = {
        "report_title": data.title,
        "report_summary": data.summary,
        "task_ext_ids": list(data.task_ext_ids),
        "work_product_urls": list(data.work_product_urls),
        "session_slug": data.session_slug,
        "share_token": data.share_token,
        "report_source": data.source,
        "cli_session_id": data.cli_session_id,
        "reported_at": timezone.now(),
    }
    turn = _claim_dispatch_row(agent, data)
    if turn is not None:
        for key, value in fields.items():
            setattr(turn, key, value)
        # The agent's own timings are better than the runner's: `started_at` from
        # the harness is when the SESSION was created, `ended_at` only the agent
        # knows. Never overwrite a known dispatch time with a null.
        if _aware(data.started_at):
            turn.started_at = _aware(data.started_at)
        if _aware(data.ended_at):
            turn.finished_at = _aware(data.ended_at)
        turn.save()
        return turn

    return Turn.objects.create(
        agent=agent,
        origin=Turn.ORIGIN_API,
        status=Turn.DONE,
        idempotency_key=f"closeout:{agent.slug}:{data.cli_session_id}",
        emdash_task_id=getattr(data, "emdash_task_id", "") or "",
        started_at=_aware(data.started_at),
        finished_at=_aware(data.ended_at),
        **fields,
    )


def list_turns(agent: Agent, limit: int = 100) -> list[Turn]:
    """Newest first. Turn.Meta orders ASC (the queue is drained oldest-first), which
    is the wrong end for a workspace timeline, so this reverses it explicitly."""
    return list(agent.turns.select_related("agent").order_by("-created_at")[:limit])


# ---- work products ----
def upsert_work_products(agent: Agent, items: list) -> dict:
    """Create work products; re-posting the same url for the agent updates it."""
    created = replaced = 0
    for item in items:
        _, was_created = AgentWorkProduct.objects.update_or_create(
            agent=agent,
            url=item.url,
            defaults={
                "title": item.title,
                "kind": item.kind,
                "description": item.description,
                "tags": item.tags,
                "source": item.source,
            },
        )
        if was_created:
            created += 1
        else:
            replaced += 1
    return {"created": created, "replaced": replaced}


def list_work_products(agent: Agent, limit: int = 200) -> list[AgentWorkProduct]:
    return list(agent.work_products.select_related("agent")[:limit])


# ---- skills ----
@transaction.atomic
def replace_skills(agent: Agent, items: list) -> int:
    """Replace the agent's whole skill catalog so it mirrors the repo."""
    agent.skills.all().delete()
    AgentSkill.objects.bulk_create(
        [
            AgentSkill(
                agent=agent,
                name=s.name,
                description=s.description,
                url=s.url,
                improvement_note=s.improvement_note,
                launchable=s.launchable,
                args_hint=s.args_hint,
            )
            for s in items
        ]
    )
    return agent.skills.count()


def list_skills(agent: Agent) -> list[AgentSkill]:
    return list(agent.skills.select_related("agent"))


# ---- projects ----
#
# A project is the work an agent's `Projects/<name>` Drive folder holds. canopy
# keeps the state (status, owner, what is waiting); Drive keeps the files. See
# `AgentProject`.


def list_projects(agent: Agent, status: str = "") -> list:
    qs = agent.projects.select_related("owner_user")
    if status:
        qs = qs.filter(status=status)
    return list(qs)


def get_project(agent: Agent, ref: str):
    """By `ext_id` ("P3") or by numeric id — the CLI and the board name projects
    differently, and a caller should not have to know which it holds."""
    ref = str(ref)
    qs = agent.projects.select_related("owner_user")
    project = qs.filter(ext_id__iexact=ref).first()
    if project is None and ref.isdigit():
        project = qs.filter(pk=int(ref)).first()
    return project


def next_project_ext_id(agent: Agent) -> str:
    """P1, P2, … — from a counter that only ever goes up.

    Not from the highest existing project: deleting P2 would hand "P2" out
    again, and an old link — a task's Links, a Drive folder, an email — would
    then point at different work. The increment is a single UPDATE so two turns
    numbering at once cannot collide.
    """
    from django.db.models import F

    Agent.objects.filter(pk=agent.pk).update(project_seq=F("project_seq") + 1)
    agent.refresh_from_db(fields=["project_seq"])
    return f"P{agent.project_seq}"


_PROJECT_FIELDS = ("name", "outcome", "status", "owner_note", "drive_folder_id",
                   "drive_folder_url", "repo_slug", "notes")
_PROJECT_STATUS = {AgentProject.ACTIVE, AgentProject.DONE, AgentProject.ARCHIVED}


def _norm_project_status(value: str) -> str:
    return value if value in _PROJECT_STATUS else AgentProject.ACTIVE


def create_project(agent: Agent, data) -> AgentProject:
    payload = {f: getattr(data, f) for f in _PROJECT_FIELDS if getattr(data, f, None) is not None}
    payload["status"] = _norm_project_status(payload.get("status", AgentProject.ACTIVE))
    if getattr(data, "links", None):
        payload["links"] = [link.model_dump() for link in data.links]
    ext_id = (getattr(data, "ext_id", "") or "").strip() or next_project_ext_id(agent)
    return AgentProject.objects.create(agent=agent, ext_id=ext_id, **payload)


def patch_project(project: AgentProject, data: dict) -> AgentProject:
    for f in _PROJECT_FIELDS:
        if f in data:
            value = data[f]
            setattr(project, f, _norm_project_status(value) if f == "status" else value)
    if "links" in data:
        project.links = data["links"]
    project.save()
    return project


def project_task_counts(agent: Agent) -> dict:
    """`{project_id: (tasks, still open)}` in ONE query.

    The board shows a count per project, and resolving it per row would be a
    query per project on a page that lists them all.
    """
    from django.db.models import Case, Count, IntegerField, When

    rows = (
        AgentTask.objects.filter(agent=agent, project__isnull=False)
        .values("project_id")
        .annotate(
            total=Count("id"),
            open=Count(Case(When(status__in=[AgentTask.SUGGESTED, AgentTask.IN_PROGRESS], then=1),
                            output_field=IntegerField())),
        )
    )
    return {r["project_id"]: (r["total"], r["open"]) for r in rows}


def set_task_project(task, project) -> None:
    """Put a task in a project (or take it out with None)."""
    task.project = project
    task.save(update_fields=["project", "updated_at"])


# ---- tasks ----
def _norm_status(s: str) -> str:
    return s if s in _VALID_TASK_STATUS else AgentTask.SUGGESTED


@transaction.atomic
def sync_tasks(agent: Agent, items: list) -> dict:
    """Upsert tasks from the (legacy) source sheet by ext_id. NON-destructive:
    the DB is now the source of truth, so DB-only fields (rationale/plan/…) and
    DB-only tasks are preserved; the sheet just sets the columns it carries."""
    created = updated = 0
    for t in items:
        # A project is set only when the payload NAMES one. A wholesale sync
        # that defaulted it to "" would unfile every task it touches, and the
        # CLI's `agent add` goes through this path — so filing a task once and
        # editing its title later would quietly take it out of its project.
        ref = (getattr(t, "project", "") or "").strip()
        filing = {"project": get_project(agent, ref)} if ref else {}
        _, was_created = AgentTask.objects.update_or_create(
            agent=agent,
            ext_id=t.ext_id,
            defaults=dict(
                **filing,
                title=t.title,
                next_action=t.next_action,
                status=_norm_status(t.status),
                owner=t.owner,
                assigned=t.assigned,
                confidence=t.confidence,
                due=t.due,
                links=[l.model_dump() for l in t.links],
                notes=t.notes,
                position=t.position,
                source=t.source,
            ),
        )
        created += int(was_created)
        updated += int(not was_created)
    return {"created": created, "count": agent.tasks.count()}


_TASK_FIELDS = ("title", "next_action", "status", "owner", "assigned", "confidence",
                "score", "review", "rationale", "source_url", "plan", "due", "notes", "position")


def create_task(agent: Agent, data) -> AgentTask:
    payload = {f: getattr(data, f) for f in _TASK_FIELDS if getattr(data, f, None) is not None}
    payload["status"] = _norm_status(payload.get("status", AgentTask.SUGGESTED))
    if getattr(data, "links", None):
        payload["links"] = [l.model_dump() for l in data.links]
    # An unknown project reference files the task nowhere rather than 404ing the
    # create: the task is the thing worth keeping, and a typo in "P7" must not
    # cost the agent the work it just recorded. The response carries
    # `project_ext_id: null`, so the miss is visible.
    ref = (getattr(data, "project", "") or "").strip()
    project = get_project(agent, ref) if ref else None
    return AgentTask.objects.create(agent=agent, ext_id=data.ext_id, project=project, **payload)


class UnknownPersonError(Exception):
    """canopy cannot route a wait to somebody it has never seen."""


def resolve_waiting_on(agent: Agent, email: str):
    """The member of this agent's workspace with that email, or None to clear.

    Scoped to the workspace on purpose: "waiting on you" is a claim on somebody
    canopy can notify AND who can see the agent, so routing it to a stranger
    would create a wait nobody will ever answer.
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    from django.contrib.auth import get_user_model

    from apps.workspaces import services as wsvc

    user = get_user_model().objects.filter(email__iexact=email).first()
    if user is None or not wsvc.is_member(user, agent.workspace_id):
        raise UnknownPersonError(
            f"{email or 'that person'} is not a member of this agent's workspace — "
            f"put the name in `assigned` instead"
        )
    return user


def patch_task(task: AgentTask, data) -> AgentTask:
    """Partial update — only fields present in `data` (a dict) are written."""
    for f in _TASK_FIELDS:
        if f in data:
            setattr(task, f, _norm_status(data[f]) if f == "status" else data[f])
    if "links" in data:
        task.links = data["links"]
    if "waiting_on_email" in data:
        task.waiting_on_user = resolve_waiting_on(task.agent, data["waiting_on_email"])
    if "project" in data:
        # Present-and-empty means "take it out of its project"; absent means
        # "leave it where it is" (the schema keeps the two apart).
        ref = (data["project"] or "").strip()
        task.project = get_project(task.agent, ref) if ref else None
    task.save()
    return task


def get_task(agent: Agent, task_id: int) -> AgentTask | None:
    return agent.tasks.filter(id=task_id).select_related("agent").first()


def list_tasks(agent: Agent) -> list[AgentTask]:
    return list(agent.tasks.select_related("agent"))


# ---- asks: what a task needs from a human -------------------------------
#
# An `Item` was its own model — "work YOU do", the dual of a Turn. The two
# stopped being different things in practice: the fleet's work lives in tasks,
# one agent had ever raised an item, and the tasks actually waiting on somebody
# reached no inbox at all. So an ask is a property of a task, and these are
# Item's three verbs with the same guarantees.


class AlreadyDecidedError(Exception):
    """The ask is closed. Deciding twice would dispatch its work twice."""


@transaction.atomic
def raise_asks(*, agent: Agent, payloads: list[dict]) -> list:
    """Raise asks on an agent, idempotent per `idempotency_key`.

    The whole batch commits in ONE outer transaction so a fleet audit raising N
    of them notifies once rather than N times; each row keeps its own SAVEPOINT
    so a single duplicate key replays without rolling the batch back.
    """
    out = []
    for p in payloads:
        key = p.get("idempotency_key") or ""
        existing = AgentTask.objects.filter(idempotency_key=key).first() if key else None
        if existing is not None:
            out.append(existing)
            continue
        project = get_project(agent, str(p.get("project") or "")) if p.get("project") else None
        try:
            with transaction.atomic():  # savepoint
                out.append(AgentTask.objects.create(
                    agent=agent,
                    project=project,
                    ext_id=p.get("ext_id") or next_task_ext_id(agent),
                    title=p["title"],
                    # Suggested is the board's word for "the agent proposed it,
                    # a human validates" — which is what an ask is.
                    status=AgentTask.SUGGESTED,
                    ask_kind=p.get("ask_kind") or AgentTask.ASK_REVIEW,
                    ask_body=p.get("ask_body") or "",
                    origin=p.get("origin") or "",
                    origin_ref=p.get("origin_ref") or {},
                    dispatch=p.get("dispatch") or [],
                    batch_key=p.get("batch_key") or "",
                    idempotency_key=key or None,
                    raised_by_id=p.get("raised_by") or None,
                    waiting_on_user=p.get("waiting_on_user"),
                    assigned=p.get("assigned") or "",
                ))
        except IntegrityError:
            replay = AgentTask.objects.filter(idempotency_key=key).first() if key else None
            if replay is None:
                raise
            out.append(replay)
    return out


def decide_ask(task: AgentTask, *, decision: str, comment: str, by: str,
               actor_workspace_slugs: set[str], decided_by_user=None):
    """Answer a task's ask, dispatching its work the moment the human commits.

    A **review** takes a verb from the closed set and dispatches on `implement`.
    A **question** is resolved by its ANSWER — `decision` stays blank, and any
    answer dispatches, because there is no verb to click. Answering used to be
    inert, and three answered cards produced zero turns (2026-07-30).

    Atomic, and that is the whole ballgame: `dispatch()` raises on a bad spec,
    and committing the decision first would leave the ask closed and
    undispatched — permanently, since deciding twice is refused. Rolling back
    instead leaves it open and retryable.
    """
    from apps.harness.dispatch import dispatch as dispatch_ask

    if not task.ask_is_open:
        raise AlreadyDecidedError(f"task {task.uuid} has no open ask")

    if task.ask_kind == AgentTask.ASK_QUESTION:
        if not (comment or "").strip():
            raise ValueError("a question is resolved by its answer — comment must not be empty")
        decision = ""
    elif decision not in (AgentTask.IMPLEMENT, AgentTask.SKIP, AgentTask.DEFER):
        raise ValueError(f"decision must be one of implement|skip|defer, got {decision!r}")

    with transaction.atomic():
        task.decision = decision
        task.comment = comment or ""
        task.decided_by = by
        task.decided_by_user = (
            decided_by_user if getattr(decided_by_user, "is_authenticated", False) else None
        )
        task.decided_at = timezone.now()
        # Answered: nobody is waiting on a person any more.
        task.waiting_on_user = None

        turns = []
        answered = task.ask_kind == AgentTask.ASK_QUESTION and bool(task.dispatch)
        if decision == AgentTask.IMPLEMENT or answered:
            turns = dispatch_ask(task, actor_workspace_slugs=actor_workspace_slugs)
            task.dispatched_at = timezone.now()
            # The agent has the ball now.
            task.status = AgentTask.IN_PROGRESS
        elif decision == AgentTask.SKIP:
            task.status = AgentTask.DECLINED
        # `defer` leaves the task suggested: not now is not never, and the card
        # stays on the board while the ask stops asking.

        task.save(update_fields=[
            "decision", "comment", "decided_by", "decided_by_user", "decided_at",
            "dispatched_at", "status", "waiting_on_user", "updated_at",
        ])
    return task, turns


def dismiss_ask(task: AgentTask, *, by: str, decided_by_user=None, comment: str = "") -> AgentTask:
    """Retire an open ask without acting — raised in error, or overtaken.

    Guards on the same state decide does: dismissing a decided ask would
    overwrite who approved it while the turns that decision dispatched keep
    running.
    """
    if not task.ask_is_open:
        raise AlreadyDecidedError(f"task {task.uuid} has no open ask")
    task.decided_by = by
    task.decided_by_user = (
        decided_by_user if getattr(decided_by_user, "is_authenticated", False) else None
    )
    task.decided_at = timezone.now()
    task.waiting_on_user = None
    task.status = AgentTask.DECLINED
    task.ask_dismissed = True
    fields = ["decided_by", "decided_by_user", "decided_at", "status",
              "ask_dismissed", "waiting_on_user", "updated_at"]
    if comment:
        task.comment = comment
        fields.append("comment")
    task.save(update_fields=fields)
    return task


def next_task_ext_id(agent: Agent) -> str:
    """T1, T2, … from a counter that only goes up — the same rule projects use,
    and for the same reason: a reused id makes an old link point at new work."""
    from django.db.models import F

    Agent.objects.filter(pk=agent.pk).update(task_seq=F("task_seq") + 1)
    agent.refresh_from_db(fields=["task_seq"])
    return f"T{agent.task_seq}"


#: Statuses a task is still LIVE in. A done or declined card waits on nobody.
LIVE_STATUSES = [AgentTask.SUGGESTED, AgentTask.IN_PROGRESS]


def waiting_q():
    """"Somebody has to do something" — as ONE predicate.

    Two shapes count, and both are real:
      * an open ASK (a review or question nobody has answered), and
      * a live task PARKED ON A PERSON, which is most of what the fleet's
        boards actually hold — "waiting on Andrea for the numbers" is a wait
        even though nothing is being asked.

    One definition because three consumers read it (the inbox, the waiting
    badge, push), and this codebase has already paid for the same predicate
    written three times.
    """
    from django.db.models import Q

    return Q(status__in=LIVE_STATUSES) & (
        (~Q(ask_kind="") & Q(decided_at__isnull=True)) | Q(waiting_on_user__isnull=False)
    )


def tasks_waiting_on(user, *, agent: Agent | None = None):
    """The inbox: live tasks parked on THIS person.

    `waiting_on_user`, not the free-text `assigned`: canopy cannot notify a
    string, and the fleet's boards spell one human three ways
    ("Jonathan", "Jonathan Jackson", "jjackson@dimagi.com").
    """
    from django.db.models import Q

    qs = (
        AgentTask.objects.filter(
            Q(waiting_on_user=user),
            Q(status__in=LIVE_STATUSES),
            Q(decided_at__isnull=True),
        )
        .select_related("agent", "project")
        .order_by("-updated_at")
    )
    return qs.filter(agent=agent) if agent is not None else qs


# ---- task commands (the board's action queue) ----
@transaction.atomic
def create_command(agent: Agent, task, kind: str, payload: dict, created_by: str) -> AgentTaskCommand:
    """Record a board action. Some kinds apply immediately to the task; accept
    and dispatch also leave a PENDING command for the agent to drain."""
    C = AgentTaskCommand
    payload = payload or {}
    cmd = C(agent=agent, task=task, kind=kind, payload=payload, created_by=created_by)
    applied_now = True  # most kinds need no agent follow-up
    if task is not None:
        if kind == C.ACCEPT:
            task.status, task.assigned = AgentTask.IN_PROGRESS, "Echo"
            task.save(update_fields=["status", "assigned", "updated_at"])
            applied_now = False  # the agent still has to do the work
        elif kind == C.DECLINE:
            task.status = AgentTask.DECLINED
            reason = payload.get("reason", "").strip()
            if reason:
                task.notes = f"{task.notes}\nDeclined: {reason}".strip()
            task.save(update_fields=["status", "notes", "updated_at"])
        elif kind == C.REASSIGN:
            task.assigned = payload.get("assignee", task.assigned)
            task.save(update_fields=["assigned", "updated_at"])
        elif kind == C.EDIT:
            for f in ("title", "next_action", "plan", "owner", "assigned"):
                if f in payload:
                    setattr(task, f, payload[f])
            task.save()
        elif kind == C.DONE:
            task.status = AgentTask.DONE
            task.save(update_fields=["status", "updated_at"])
        elif kind == C.COMMENT:
            note = payload.get("note", "").strip()
            if note:
                task.notes = f"{task.notes}\n{note}".strip()
                task.save(update_fields=["notes", "updated_at"])
        elif kind == C.DISPATCH:
            applied_now = False  # pure agent work
    if applied_now:
        cmd.status, cmd.applied_at = C.APPLIED, timezone.now()
    cmd.save()
    return cmd


def list_commands(agent: Agent, status: str | None = None) -> list[AgentTaskCommand]:
    qs = agent.commands.select_related("task", "agent")
    if status:
        qs = qs.filter(status=status)
    return list(qs)


def apply_command(cmd: AgentTaskCommand, result_note: str = "") -> AgentTaskCommand:
    cmd.status = AgentTaskCommand.APPLIED
    cmd.applied_at = timezone.now()
    if result_note:
        cmd.result_note = result_note
    cmd.save(update_fields=["status", "applied_at", "result_note"])
    return cmd


# ---- Agent credentials (per-agent secret store, encrypted at rest) ----------
# Spec: docs/superpowers/specs/2026-09-05-agent-credentials-design.md

#: Where a resolved value came from. During migration a secret can exist in BOTH
#: stores and the box silently prefers canopy-web, so the screen must say which
#: is live rather than only that something is set.
SOURCE_CANOPY_WEB = "canopy-web"
SOURCE_UNSET = "unset"


def set_agent_credentials(agent, values: dict[str, str], *, user=None) -> int:
    """Upsert named secrets. NON-CLOBBERING: a ref absent from `values` is left
    alone, so a single-field edit can never wipe the rest (the same rule
    RunnerCredentialIn follows, for the same reason).

    Rejects a blank value rather than storing one: "" is how a UI says "I did not
    type anything", and storing it makes a declared ref read as SET while
    resolving to nothing — the worst of both.
    """
    from apps.agents.models import AgentCredential
    from apps.common.encryption import encrypt_secret

    blank = sorted(k for k, v in values.items() if not (v or "").strip())
    if blank:
        raise ValueError(f"blank value for: {', '.join(blank)}")

    written = 0
    for name, value in values.items():
        AgentCredential.objects.update_or_create(
            agent=agent,
            name=name.strip(),
            defaults={
                "value_enc": encrypt_secret(value.strip()),
                "updated_by": user if getattr(user, "is_authenticated", False) else None,
            },
        )
        written += 1
    return written


def delete_agent_credential(agent, name: str) -> int:
    from apps.agents.models import AgentCredential

    deleted, _ = AgentCredential.objects.filter(agent=agent, name=name).delete()
    return deleted


def agent_credential_status(agent) -> list[dict]:
    """Every ref the agent DECLARES, against what is actually set — booleans and
    timestamps, never values.

    This is the screen that would have caught the 2026-09-05 failure: ACE's
    mailbox had been dead since May and seeing that required SSH-ing to a box and
    running `gog auth list`. A credential that lives only in a vault and a
    keyring is a credential nobody is watching.

    Declared-but-unset rows appear (that is the question the page answers), and
    so do stored-but-undeclared ones — an orphan left behind when a ref was
    removed from runtime.yaml is a live secret nothing accounts for, and hiding
    it is how it stays that way.
    """
    from apps.agents.models import AgentCredential

    stored = {c.name: c for c in AgentCredential.objects.filter(agent=agent).select_related("updated_by")}
    declared = [str(n) for n in (agent.runtime_secrets or []) if str(n).strip()]

    rows: list[dict] = []
    for name in declared + [n for n in sorted(stored) if n not in declared]:
        cred = stored.get(name)
        rows.append({
            "name": name,
            "declared": name in declared,
            "set": cred is not None,
            "source": SOURCE_CANOPY_WEB if cred else SOURCE_UNSET,
            "updated_at": cred.updated_at if cred else None,
            "updated_by_email": (cred.updated_by.email if cred and cred.updated_by_id else None),
        })
    return rows


def resolve_agent_credentials(agent) -> dict[str, str]:
    """PLAINTEXT. The only path that returns values, and only to a runner."""
    from apps.agents.models import AgentCredential
    from apps.common.encryption import decrypt_secret

    return {
        c.name: decrypt_secret(c.value_enc)
        for c in AgentCredential.objects.filter(agent=agent)
    }


def caller_runs_agent(user, agent) -> bool:
    """True when `user` pairs a live runner that this agent's routing could
    actually send work to.

    Tighter than workspace membership, deliberately: plaintext should reach a box
    that runs the agent, not everyone who can see it. Mirrors the runner
    credential fetch, whose trust boundary is "the caller who can claim turns as
    this runner".
    """
    from apps.harness.models import Runner, RunnerAssignment

    # ASSIGNED, not necessarily ENABLED. `enabled=False` removes a runner from
    # the automatic rotation — it stops being a fallback — but it does NOT stop
    # work being directed there: `claim_next_turn` checks `pinned_runner` first
    # and returns before any assignment lookup ("a pin trumps everything below
    # it"). That combination IS the explicit-only runner Jonathan asked for on
    # 2026-09-06: "I don't want it to fire ace commands as a fall back, only if
    # we explicit trigger it."
    #
    # So requiring `enabled=True` here contradicted the claim path: a pinned turn
    # would land on the box, which would then be refused the very secrets it
    # needs to run the agent — a failure at the far end of a long round trip,
    # reading as a broken agent rather than a routing rule.
    #
    # The trust boundary is unchanged in substance: an assignment row at all
    # means this agent's work may be directed at this runner, and the caller must
    # still PAIR it. Disabling governs automatic routing, not trust.
    return RunnerAssignment.objects.filter(
        agent=agent, runner__paired_by=user,
    ).exclude(runner__status=Runner.RETIRED).exists()


# ---- 1Password vault + import (spec 2026-09-06) -------------------------------

def set_agent_vault(agent, *, vault=None, service_key=None):
    """Set the agent's vault name and/or its service-account token.

    Non-clobbering on the KEY specifically: renaming a vault must not silently
    wipe the credential that reads it, which is the shape of every other
    credential write here."""
    from apps.common.encryption import encrypt_secret

    fields = []
    if vault is not None:
        agent.op_vault = vault.strip()
        fields.append("op_vault")
    if service_key and service_key.strip():
        agent.op_sa_token_enc = encrypt_secret(service_key.strip())
        fields.append("op_sa_token_enc")
    if fields:
        agent.save(update_fields=[*fields, "updated_at"])
    return agent_vault_status(agent)


def agent_vault_status(agent):
    """Vault config plus how much of the agent is actually locatable.

    `locatable` is the number of declared refs that carry a source. It is the
    difference between "the import found nothing" and "this deployment was never
    told where anything lives" — the same failure with two completely different
    fixes."""
    from apps.agents import vault_import
    from apps.agents.schemas import AgentVaultOut

    declared = [str(n) for n in (agent.runtime_secrets or []) if str(n).strip()]
    plan = vault_import.plan_import(declared, agent.runtime_sources or {})
    return AgentVaultOut(
        vault=agent.op_vault,
        key_set=bool(agent.op_sa_token_enc),
        declared=len(declared),
        locatable=sum(1 for i in plan if i.kind in ("op", "value")),
    )


def resolve_agent_vault(agent) -> tuple[str, str]:
    """PLAINTEXT vault config, for a runner. Empty token when none is set."""
    from apps.common.encryption import decrypt_secret

    token = decrypt_secret(agent.op_sa_token_enc) if agent.op_sa_token_enc else ""
    return agent.op_vault, token


def resolve_shared_vault(agent) -> tuple[str, str]:
    """PLAINTEXT shared-vault config for this agent's TENANT.

    The sibling of resolve_agent_vault one level up. Returns ("", "") when the
    workspace has not been configured, which is what keeps this additive: the
    box falls back to its compiled-in default and behaves exactly as before.

    Deliberately reads the workspace rather than deriving a name, because the
    whole defect this closes was a derived name — "Canopy-Shared" compiled into
    bootstrap_agents.sh, correct for one tenant and silently wrong for the next.
    """
    from apps.common.encryption import decrypt_secret

    ws = agent.workspace
    if ws is None:
        return "", ""
    token = decrypt_secret(ws.shared_op_sa_token_enc) if ws.shared_op_sa_token_enc else ""
    return ws.shared_op_vault, token


# ---- what the BOX observed (spec 2026-09-07) ---------------------------------

def record_bootstrap_report(agent, *, runner_name, client_creds_ok, mailbox_ok,
                            gog_client="", detail="", turn_client="",
                            turn_ready=None, env_ok=None):
    """Upsert one box's view of this agent. Latest wins — this is current state,
    not a log: the question it answers is "can this agent run RIGHT NOW", and a
    history of that would bury the answer under every prior boot."""
    from apps.agents.models import AgentBootstrapReport

    row, _ = AgentBootstrapReport.objects.update_or_create(
        agent=agent, runner_name=runner_name.strip(),
        defaults={
            "client_creds_ok": bool(client_creds_ok),
            "mailbox_ok": bool(mailbox_ok),
            "gog_client": (gog_client or "").strip(),
            # None is preserved, not coerced: a box that did not check must not
            # report the agent as broken. Only a real observation flips it.
            "turn_client": (turn_client or "").strip(),
            "turn_ready": (None if turn_ready is None else bool(turn_ready)),
            "env_ok": (None if env_ok is None else bool(env_ok)),
            "detail": (detail or "").strip()[:2000],
        },
    )
    return row


def bootstrap_reports(agent) -> list:
    """Every box's view of this agent, newest first."""
    from apps.agents.models import AgentBootstrapReport

    return list(
        AgentBootstrapReport.objects.filter(agent=agent).order_by("-reported_at")
    )
