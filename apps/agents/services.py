"""Business logic for the agent workspace — kept out of the Ninja router so it's
unit-testable without HTTP."""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from apps.harness.models import Turn

from .models import (
    Agent,
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
    for field in ("repo_url", "repo_ref", "runtime_engine", "runtime_secrets", "runner_preference"):
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
        "runner_preference": list(agent.runner_preference or []),
        "turn_mode": agent.turn_mode,
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
        _, was_created = AgentTask.objects.update_or_create(
            agent=agent,
            ext_id=t.ext_id,
            defaults=dict(
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
    return AgentTask.objects.create(agent=agent, ext_id=data.ext_id, **payload)


def patch_task(task: AgentTask, data) -> AgentTask:
    """Partial update — only fields present in `data` (a dict) are written."""
    for f in _TASK_FIELDS:
        if f in data:
            setattr(task, f, _norm_status(data[f]) if f == "status" else data[f])
    if "links" in data:
        task.links = data["links"]
    task.save()
    return task


def get_task(agent: Agent, task_id: int) -> AgentTask | None:
    return agent.tasks.filter(id=task_id).select_related("agent").first()


def list_tasks(agent: Agent) -> list[AgentTask]:
    return list(agent.tasks.select_related("agent"))


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
