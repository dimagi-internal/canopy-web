"""Business logic for the agent workspace — kept out of the Ninja router so it's
unit-testable without HTTP."""
from __future__ import annotations

import datetime as dt
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.harness.models import Turn, agent_turns_q

from .models import (
    Agent,
    AgentProject,
    AgentSkill,
    AgentSync,
    AgentTask,
    AgentTaskAction,
)

_VALID_TASK_STATUS = {AgentTask.SUGGESTED, AgentTask.IN_PROGRESS, AgentTask.DONE, AgentTask.DECLINED}


def _aware(value):
    if isinstance(value, dt.datetime) and timezone.is_naive(value):
        return value.replace(tzinfo=dt.UTC)
    return value


# ---- agents ----
#: What an agent IS on its boxes: the code they clone and what they install with
#: it. Changing one on an existing agent is its admins' (agents/api.py upsert).
DEFINITION_FIELDS = ("repo_url", "repo_ref", "runtime_engine", "runtime_secrets",
                     "runtime_sources")


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


def _canopy_user_summary(agent: Agent) -> dict | None:
    linked = agent.user
    if linked is None:
        return None
    return {"user_id": linked.pk, "name": linked.get_full_name() or linked.email, "email": linked.email}


class AgentUserLinkError(Exception):
    """The canopy user cannot be linked to this agent; the message says why, as shown."""


def link_canopy_user(agent: Agent, user_id: int | None) -> Agent:
    """Link this agent to the canopy user it IS (`Agent.user`), or unlink with None.

    An agent calls canopy with its own token, and that token signs in as a
    canopy user — `ace@dimagi-ai.com` for ACE. This link is what says that user
    is the agent: an agent acting as itself is never confined against itself
    (`agents/access.py`). It is an identity, not a permission — it grants the
    user nothing it did not already have.

    It was only ever set by migration `agents/0022`, matching on `Agent.email`,
    so agents created with a blank email (echo, ace, eva) had no link and no way
    to get one.

    One user is ONE instance: the column is one-to-one, so of several ACE
    instances exactly one can be `ace@dimagi-ai.com`, and a second attempt says
    which instance already is. The user must be a member of the agent's
    workspace. The agent's `email` is filled from the user when it was blank,
    since for these agents it is the same mailbox.
    """
    from django.contrib.auth import get_user_model

    from apps.workspaces import services as wsvc

    if user_id is None:
        agent.user = None
        agent.save(update_fields=["user", "updated_at"])
        return agent
    linked = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if linked is None:
        raise AgentUserLinkError("no such canopy user")
    other = Agent.objects.filter(user=linked).exclude(pk=agent.pk).first()
    if other is not None:
        raise AgentUserLinkError(f"{linked.email} is already the canopy user of '{other.slug}' — "
                                 "one user is one agent instance; unlink it there first")
    if not wsvc.is_member(linked, agent.workspace_id):
        raise AgentUserLinkError(f"{linked.email} is not a member of this agent's workspace")
    agent.user = linked
    fields = ["user", "updated_at"]
    if not agent.email:
        agent.email = linked.email
        fields.append("email")
    agent.save(update_fields=fields)
    return agent


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
        "canopy_user": _canopy_user_summary(agent),
        "runner_preference": list(agent.runner_preference or []),
        "turn_mode": agent.turn_mode,
        "slack_enabled": agent.slack_enabled,
        "people_digest_enabled": agent.people_digest_enabled,
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
        "sync_count": agent.syncs.count(),
        "skill_count": agent.skills.count(),
        "task_count": agent.tasks.count(),
        # Same set as `GET /api/harness/turns/?agent=` (agent_turns_q): counting
        # only `agent.turns` left out every email/Slack turn, which targets the
        # agent's session, so the card and the turn list disagreed (#359).
        "turn_count": Turn.objects.filter(agent_turns_q(agent)).count(),
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
    turns = Turn.objects.filter(agent_turns_q(agent))
    started = turns.filter(started_at__isnull=False).order_by("-started_at").first()
    if started is not None:
        return started.started_at
    newest = turns.order_by("-created_at").first()
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
      2. session_key — the runner stamped the emdash session it created; the
         closing agent recovers the same name from its cwd. Newest UNREPORTED turn
         for that task wins, because a reused session serves many turns and the one
         being closed is the latest.

      3. cli_session_id AS the session key — a CLOUD runner has no emdash task:
         it stamps the turn with the Claude session id it ran (`finish`), and the
         closing agent, whose cwd is the shared agent clone rather than an emdash
         worktree, has no task name to recover. The Claude session id it reports
         is that same id, so it is the join. Without this every cloud close-out
         became its own report-only row beside the turn it was closing.

    No time window on (2): an agent turn legitimately runs for hours, and a wrong
    window would silently split one turn into two rows — the exact failure this
    merge exists to end. The `reported_at__isnull=True` filter is what keeps an
    older turn from being claimed twice.
    """
    if data.cli_session_id:
        existing = agent.turns.filter(cli_session_id=data.cli_session_id).first()
        if existing is not None:
            return existing
    task = getattr(data, "session_key", "") or data.cli_session_id or ""
    if task:
        return (
            agent.turns.filter(session_key=task, reported_at__isnull=True)
            .order_by("-created_at")
            .first()
        )
    return None


def upsert_turn(agent: Agent, data, initiator=None) -> Turn:
    """Attach an agent's close-out report to the turn it was dispatched as.

    Idempotent per (agent, cli_session_id). When no dispatch row can be matched —
    a turn a human started by hand in a terminal, or a fleet still posting without
    `session_key` — a report-only Turn is created instead, so the record is never
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
    extra_ref = dict(getattr(data, "origin_ref", None) or {})
    turn = _claim_dispatch_row(agent, data)
    if turn is not None:
        for key, value in fields.items():
            setattr(turn, key, value)
        if extra_ref:
            turn.origin_ref = {**(turn.origin_ref or {}), **extra_ref}
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
        # Who reported it, when the caller says (the API passes the request's
        # asker). Provenance + the TURN_CREATED line come from the harness's
        # pre_save/post_save receivers (apps/harness/provenance.py).
        **(initiator.fields() if initiator is not None else {}),
        agent=agent,
        origin=Turn.ORIGIN_API,
        status=Turn.DONE,
        idempotency_key=f"closeout:{agent.slug}:{data.cli_session_id}",
        session_key=getattr(data, "session_key", "") or "",
        started_at=_aware(data.started_at),
        finished_at=_aware(data.ended_at),
        origin_ref=extra_ref,
        **fields,
    )


def stamp_share_urls(turns) -> list:
    """Stamp `share_url` on each turn: its transcript's page, under the
    workspace the transcript was shared from. One query for the whole page.
    Run AFTER redaction, so a hidden turn's blanked token yields no link."""
    from apps.session_sharing.models import ShareToken
    from apps.workspaces import services as wsvc

    tokens = {t.share_token for t in turns if t.share_token}
    homes = dict(
        ShareToken.objects.filter(token__in=tokens, revoked_at__isnull=True)
        .values_list("token", "session__workspace_id")
    ) if tokens else {}
    for t in turns:
        ws = homes.get(t.share_token) if t.share_token else None
        t.share_url = wsvc.scoped_url_or_none(ws, f"/share/{t.share_token}") if ws else None
    return turns


def list_turns(agent: Agent, limit: int = 100) -> list[Turn]:
    """Newest first. Turn.Meta orders ASC (the queue is drained oldest-first), which
    is the wrong end for a workspace timeline, so this reverses it explicitly.

    Each turn also carries where to go to see what it DID — `linked_session_id`
    (see `_link_turn_sessions`) and `has_transcript` — because a turn row on its
    own is only the dispatch envelope."""
    from django.db.models import Exists, OuterRef

    from apps.harness.models import TurnTranscript

    turns = list(
        agent.turns.select_related("agent")
        .annotate(has_transcript=Exists(TurnTranscript.objects.filter(turn=OuterRef("pk"))))
        .order_by("-created_at")[:limit]
    )
    _link_turn_sessions(agent, turns)
    return turns


# A runner names its session moments after the turn starts and reports the name
# at finish, so the session it created always exists by then. The slack covers a
# runner whose finish lands just before its session report does.
_SESSION_LINK_SLACK = dt.timedelta(minutes=5)


def _link_turn_sessions(agent: Agent, turns: list[Turn]) -> None:
    """Stamp `linked_session_id` on each turn: the chat that holds its actual work.

    Three cases, by how the turn ran:
    - a CHAT turn targets its session directly (`chat_session`);
    - an agent turn on a runner stamps `session_key` with the session it drove
      — an emdash task name on a LAPTOP, the Claude session id on a CLOUD runner
      — the same string the runner's record-session stores as
      `RunnerBinding.session_key`, so that is the join;
    - an older cloud turn (before the cloud runner recorded agent sessions) has
      no session; at most its transcript is on the turn (`has_transcript`).

    Session keys are emdash task names and names get reused, so a match must be a
    session that existed by the time the turn finished: a later session that
    happens to share the name is a different conversation. Of several that fit,
    the newest wins. One query for the whole page, not one per turn."""
    from apps.canopy_sessions.models import RunnerBinding

    keys = {t.session_key for t in turns if t.session_key and not t.chat_session_id}
    by_key: dict[str, list[tuple[dt.datetime, object]]] = {}
    if keys:
        rows = RunnerBinding.objects.filter(
            session_key__in=keys, session__agent=agent
        ).values_list("session_key", "session__created_at", "session_id")
        for key, created_at, session_id in rows:
            by_key.setdefault(key, []).append((created_at, session_id))
    now = timezone.now()
    for t in turns:
        t.linked_session_id = t.chat_session_id
        if t.linked_session_id or not t.session_key:
            continue
        cutoff = (t.finished_at or now) + _SESSION_LINK_SLACK
        fits = [c for c in by_key.get(t.session_key, []) if c[0] <= cutoff]
        if fits:
            t.linked_session_id = max(fits, key=lambda c: c[0])[1]


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
    """`{project_id: (tasks, still open, waiting on a person)}` in ONE query.

    The board shows a count per project, and resolving it per row would be a
    query per project on a page that lists them all.
    """
    from django.db.models import Count, Q

    rows = (
        AgentTask.objects.filter(agent=agent, project__isnull=False)
        .values("project_id")
        .annotate(
            total=Count("id"),
            open=Count("id", filter=Q(status__in=LIVE_STATUSES)),
            waiting=Count("id", filter=waiting_q()),
        )
    )
    return {r["project_id"]: (r["total"], r["open"], r["waiting"]) for r in rows}


def set_task_project(task, project) -> None:
    """Put a task in a project (or take it out with None)."""
    task.project = project
    task.save(update_fields=["project", "updated_at"])
    _link_participants(task)


def _link_participants(task) -> None:
    """A task in a project raised by a human's turn makes them a participant
    (fleet brain v1.1). Best-effort: never fails the task write."""
    from . import participants

    participants.on_task(task)


# ---- tasks ----
def _norm_status(s: str) -> str:
    return s if s in _VALID_TASK_STATUS else AgentTask.SUGGESTED


_TASK_FIELDS = ("title", "next_action", "status", "owner", "assigned", "confidence",
                "score", "review", "rationale", "source_url", "plan", "due", "notes", "position")

#: What a create may carry beyond `_TASK_FIELDS`, copied straight onto the row.
_TASK_CREATE_EXTRAS = ("ask_body", "on_approve", "batch_key", "origin", "origin_ref", "source")

_ASK_KINDS = {AgentTask.ASK_NONE, AgentTask.ASK_REVIEW, AgentTask.ASK_QUESTION}


def _plain_links(links) -> list:
    return [link.model_dump() if hasattr(link, "model_dump") else dict(link) for link in links or []]


@transaction.atomic
def create_tasks(agent: Agent, payloads: list[dict]) -> list[AgentTask]:
    """Create a batch of tasks, idempotent per `idempotency_key`.

    ONE outer transaction, so a batch that cannot route somebody's wait
    (`UnknownPersonError`) leaves nothing behind; each row keeps its own
    SAVEPOINT so a key two producers raced on replays instead of rolling the
    batch back. A key already used by ANOTHER agent is refused rather than
    replayed — handing back somebody else's task would cross a tenant line.
    """
    out = []
    for p in payloads:
        key = (p.get("idempotency_key") or "").strip()
        existing = AgentTask.objects.filter(idempotency_key=key).first() if key else None
        if existing is not None:
            if existing.agent_id != agent.pk:
                raise ValueError(f"idempotency_key {key!r} is already used by another agent")
            out.append(existing)
            continue

        fields = {f: p[f] for f in _TASK_FIELDS if p.get(f) is not None}
        fields.update({f: p[f] for f in _TASK_CREATE_EXTRAS if p.get(f) is not None})
        fields["status"] = _norm_status(fields.get("status", AgentTask.SUGGESTED))
        ask_kind = p.get("ask_kind") or AgentTask.ASK_NONE
        if ask_kind not in _ASK_KINDS:
            raise ValueError(f"ask_kind must be review|question, got {ask_kind!r}")
        if p.get("links"):
            fields["links"] = _plain_links(p["links"])
        # An unknown project reference files the task nowhere rather than failing
        # the create: the task is the thing worth keeping, and a typo in "P7" must
        # not cost the agent the work it just recorded. The response carries
        # `project_ext_id: null`, so the miss is visible.
        ref = str(p.get("project") or "").strip()
        waiting_on = resolve_waiting_on(agent, p["waiting_on_email"]) if p.get("waiting_on_email") else None
        raised_by = p.get("raised_by") or None
        if raised_by and not Turn.objects.filter(pk=raised_by, agent=agent).exists():
            raise ValueError(f"raised_by {raised_by} is not a turn of {agent.slug}")
        ext_id = _claim_ext_id(agent, (p.get("ext_id") or "").strip())
        try:
            with transaction.atomic():  # savepoint
                task = AgentTask.objects.create(
                    agent=agent,
                    ext_id=ext_id,
                    project=get_project(agent, ref) if ref else None,
                    ask_kind=ask_kind,
                    idempotency_key=key or None,
                    raised_by_id=raised_by,
                    waiting_on_user=waiting_on,
                    **fields,
                )
            out.append(task)
            _link_participants(task)
        except IntegrityError:
            replay = AgentTask.objects.filter(agent=agent, idempotency_key=key).first() if key else None
            if replay is not None:
                out.append(replay)
                continue
            if _ext_id_taken(agent, ext_id):  # a concurrent create took it first
                raise DuplicateTaskError(f"{agent.slug} already has a task {ext_id}") from None
            raise
    return out


class DuplicateTaskError(Exception):
    """An explicit `ext_id` the agent already uses (in any letter case)."""


def _ext_id_taken(agent: Agent, ext_id: str) -> bool:
    return agent.tasks.filter(ext_id__iexact=ext_id).exists()


def _claim_ext_id(agent: Agent, explicit: str) -> str:
    """The ext_id a new task gets.

    Explicit: refused if the agent already has it in ANY case — lookups are
    case-insensitive (`get_task`), so "t1" beside "T1" would make one of them
    unreachable. An explicit `T<n>` also moves the counter past n.

    Auto: the next counter value that is free. An explicit id may already sit
    ahead of the counter, and handing it out again would 409 every auto create
    from then on (the counter bump rolls back with the failed batch).
    """
    if explicit:
        if "/" in explicit:
            # A task is addressed as `/tasks/{ext_id}/` — a slash cannot be routed.
            raise ValueError(f"ext_id {explicit!r} may not contain '/'")
        if _ext_id_taken(agent, explicit):
            raise DuplicateTaskError(f"{agent.slug} already has a task {explicit}")
        m = re.fullmatch(r"[Tt](\d+)", explicit)
        if m:
            Agent.objects.filter(pk=agent.pk, task_seq__lt=int(m.group(1))).update(
                task_seq=int(m.group(1)))
        return explicit
    while True:
        ext_id = next_task_ext_id(agent)
        if not _ext_id_taken(agent, ext_id):
            return ext_id


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
    if "project" in data:
        _link_participants(task)
    return task


def get_task(agent: Agent, ref: str) -> AgentTask | None:
    """By `ext_id` ("T3"), the way every route and link names a task."""
    return agent.tasks.filter(ext_id__iexact=str(ref)).select_related("agent", "project").first()


def list_tasks(agent: Agent) -> list[AgentTask]:
    return list(agent.tasks.select_related("agent"))


# ---- actions: everything a person does TO a task ------------------------
#
# Five of them — approve, decline, reply, nudge, done — through one function.
# Each is recorded as an `AgentTaskAction` row, which is both the task's history
# ("who approved this and why" is the closing row) and, while `pending`, the
# agent's to-do: it drains pending rows on its next turn and marks each applied.
#
# An action that hands the agent work STARTS a turn rather than leaving a row
# for whenever the agent next runs (Jonathan, 2026-10-08): approve always does,
# nudge always does, a reply does when an editor writes it. The task's own
# `on_approve` specs run when it has them; otherwise canopy-web writes the one
# turn from the card (`apps.harness.dispatch.enqueue_task_turn`). A row whose
# turn was enqueued is `applied` — the turn IS the follow-up.


class ClosedAskError(Exception):
    """The task is not in a state this action applies to — the ask is already
    closed, the task is finished, or (nudge) it is not in progress. Acting
    anyway would dispatch twice or start unapproved work."""


#: action -> (closes the ask?, new status or None, needs agent follow-up?)
_EFFECT = {
    AgentTaskAction.APPROVE: (True, AgentTask.IN_PROGRESS, True),
    AgentTaskAction.DECLINE: (True, AgentTask.DECLINED, False),
    AgentTaskAction.REPLY: (None, None, True),  # closes only a question
    AgentTaskAction.NUDGE: (False, None, True),
    AgentTaskAction.DONE: (True, AgentTask.DONE, False),
}


@transaction.atomic
def act(task: AgentTask, *, action: str, comment: str = "", by: str, by_user=None,
        actor_workspace_ids: set, may_start_turns: bool = False,
        ) -> tuple[AgentTask, AgentTaskAction, list[Turn]]:
    """Do one of the five actions to `task`; returns (task, action row, turns).

    `may_start_turns` is whether the actor holds the tier that may start an
    agent turn (editor — the QuickTurn gate). It decides only whether a REPLY
    wakes the agent: an approve is the decision a viewer exists to make, and it
    starts the work whoever makes it (as `on_approve` always has); a nudge is
    editor-gated at the route.

    Atomic, and that is the whole ballgame: `dispatch()` raises on a bad
    `on_approve` spec, and committing the action first would leave the ask
    closed and its work never run — permanently, since approving a closed ask is
    refused. Rolling back leaves it open and retryable, with no action row.

    The task row is re-read under a lock before the ask is checked. Two tabs
    holding the same open card would otherwise both pass the check on their own
    stale copies and dispatch the work twice; locked, the second one waits,
    then sees the ask closed and gets `ClosedAskError`.
    """
    from apps.harness.dispatch import dispatch, enqueue_task_turn

    if action not in _EFFECT:
        raise ValueError(f"action must be one of {'|'.join(_EFFECT)}, got {action!r}")
    comment = (comment or "").strip()
    if action == AgentTaskAction.REPLY and not comment:
        raise ValueError("a reply needs words — comment must not be empty")

    task = (AgentTask.objects.select_for_update(of=("self",))
            .select_related("agent", "project").get(pk=task.pk))
    if action in (AgentTaskAction.APPROVE, AgentTaskAction.DECLINE):
        if task.ask_kind and not task.ask_is_open:
            raise ClosedAskError(f"{task.agent.slug}/{task.ext_id} has no open ask")
        # A plain task with nothing to ask is approvable/declinable only while it
        # is live — approving a finished one would quietly re-open it.
        if not task.ask_kind and task.status not in LIVE_STATUSES:
            raise ClosedAskError(f"{task.agent.slug}/{task.ext_id} is already {task.status}")
        # ...and approvable only once: approving starts a turn, so approving a
        # plain task that is already in progress would be a nudge any viewer
        # could send. Nudge is the action for that, and it is an editor's.
        if (action == AgentTaskAction.APPROVE and not task.ask_kind
                and task.status == AgentTask.IN_PROGRESS):
            raise ClosedAskError(
                f"{task.agent.slug}/{task.ext_id} is already in progress — nudge it instead")
    if action == AgentTaskAction.NUDGE and task.status != AgentTask.IN_PROGRESS:
        # A suggested task is started by approving it; nudging it would start
        # work nobody approved. A finished one has nothing to nudge.
        raise ClosedAskError(
            f"{task.agent.slug}/{task.ext_id} is {task.status}, not in progress — nothing to nudge")

    closes, status, follow_up = _EFFECT[action]
    if action == AgentTaskAction.REPLY:
        # A reply on a question IS the answer; on anything else it is a note.
        closes = task.ask_kind == AgentTask.ASK_QUESTION and task.ask_is_open

    row = AgentTaskAction(
        agent=task.agent, task=task, action=action, comment=comment, by=by,
        by_user=by_user if getattr(by_user, "is_authenticated", False) else None,
    )
    turns: list[Turn] = []
    # Answering a question keeps its old behaviour: it runs `on_approve` when the
    # card has one, and is otherwise the agent's pending answer to drain.
    runs_on_approve = action == AgentTaskAction.APPROVE or (action == AgentTaskAction.REPLY and closes)
    if runs_on_approve and task.on_approve:
        turns = dispatch(task, action=row, actor_workspace_ids=actor_workspace_ids)
        task.dispatched_at = timezone.now()
        follow_up = False  # the dispatched turn IS the follow-up
    elif (action in (AgentTaskAction.APPROVE, AgentTaskAction.NUDGE)
          or (action == AgentTaskAction.REPLY and not closes and may_start_turns)):
        # No spec of its own: the card's own turn. The row is saved first because
        # its pk keys the turn (one enqueue per click; a replay is the same turn).
        row.status = AgentTaskAction.APPLIED
        row.applied_at = timezone.now()
        row.save()
        turn, _created = enqueue_task_turn(task, action=row)
        turns = [turn]
        task.dispatched_at = timezone.now()
        follow_up = False
    if closes and task.ask_is_open:
        task.ask_closed_at = timezone.now()
        # Answered: nobody is waiting on a person any more.
        task.waiting_on_user = None
    if status:
        task.status = status
    elif runs_on_approve and turns:
        task.status = AgentTask.IN_PROGRESS  # the agent has the ball now
    task.save()

    row.status = AgentTaskAction.PENDING if follow_up else AgentTaskAction.APPLIED
    row.applied_at = None if follow_up else (row.applied_at or timezone.now())
    row.save()
    return task, row, turns


def pending_actions(agent: Agent):
    """The agent's queue: actions it still has to carry out, oldest first."""
    return (agent.task_actions.filter(status=AgentTaskAction.PENDING)
            .select_related("agent", "task", "by_user")
            .order_by("created_at", "id"))


def mark_applied(action_row: AgentTaskAction, result_note: str = "") -> AgentTaskAction:
    action_row.status = AgentTaskAction.APPLIED
    action_row.applied_at = timezone.now()
    if result_note:
        action_row.result_note = result_note
    action_row.save(update_fields=["status", "applied_at", "result_note"])
    return action_row


def next_task_ext_id(agent: Agent) -> str:
    """T1, T2, … from a counter that only goes up — the same rule projects use,
    and for the same reason: a reused id makes an old link point at new work."""
    from django.db.models import F

    Agent.objects.filter(pk=agent.pk).update(task_seq=F("task_seq") + 1)
    agent.refresh_from_db(fields=["task_seq"])
    return f"T{agent.task_seq}"


#: Statuses a task is still LIVE in. A done or declined card waits on nobody.
LIVE_STATUSES = [AgentTask.SUGGESTED, AgentTask.IN_PROGRESS]


def waiting_q(prefix: str = ""):
    """"Somebody has to do something" — as ONE predicate.

    Two shapes count, and both are real:
      * an open ASK (a review or question nobody has answered), and
      * a live task PARKED ON A PERSON, which is most of what the fleet's
        boards actually hold — "waiting on Andrea for the numbers" is a wait
        even though nothing is being asked.

    One definition because several consumers read it (the inbox, the waiting
    badge, push, the per-project waiting count), and this codebase has already
    paid for the same predicate written three times. `prefix` (e.g. "tasks__")
    lets a query on a related model — a project annotating its tasks — reuse it.
    """
    from django.db.models import Q

    p = prefix
    return Q(**{f"{p}status__in": LIVE_STATUSES}) & (
        (~Q(**{f"{p}ask_kind": ""}) & Q(**{f"{p}ask_closed_at__isnull": True}))
        | Q(**{f"{p}waiting_on_user__isnull": False})
    )


def filter_tasks(qs, *, user=None, project: str = "", status: str = "", waiting: str = "",
                 ask: str = "", batch: str = ""):
    """The task list's filters, shared by the per-agent and fleet routes.

    `waiting=me` is what lands in a person's inbox: tasks parked on them, plus
    open asks routed to nobody — an unrouted ask waits on whoever looks at it.
    """
    from django.db.models import Q

    if project == "none":
        qs = qs.filter(project__isnull=True)
    elif project:
        qs = qs.filter(project__ext_id__iexact=project)
    if status:
        qs = qs.filter(status__in=[s.strip() for s in status.split(",") if s.strip()])
    if waiting == "me":
        unrouted_ask = (~Q(ask_kind="") & Q(ask_closed_at__isnull=True)
                        & Q(waiting_on_user__isnull=True))
        mine = Q(waiting_on_user=user) if getattr(user, "is_authenticated", False) else Q(pk__in=[])
        qs = qs.filter(waiting_q()).filter(mine | unrouted_ask)
    if ask == "open":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=True)
    elif ask == "closed":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=False)
    if batch:
        qs = qs.filter(batch_key=batch)
    return qs


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
    #
    # AND the owner must be one of the agent's admins (`runner_may_hold_agent`).
    # An assignment row alone is written by the EDITOR tier, which an
    # auto-approved access request hands out — so without this, an editor paired a box, added it to the list
    # (disabled, at the bottom) and read every secret, both vault keys and the
    # owner's GitHub token through `/credentials/resolve`.
    if not agent.is_admin(user):
        return False
    if RunnerAssignment.objects.filter(
        agent=agent, runner__owner=user,
    ).exclude(runner__status=Runner.RETIRED).exists():
        return True
    # An agent with no order of its own follows its workspace's: a box of this
    # user's in that order is one the agent's work is directed at.
    from apps.harness.services import inherited_orders

    inh = inherited_orders([agent.id]).get(agent.id)
    return bool(inh) and any(r.owner_id == user.pk for r in inh.runners)


def runner_may_hold_agent(runner, agent) -> bool:
    """May this box run AS this agent — hold its prompt, its caller's token, its
    secrets and its owner's GitHub identity?

    Only when its owner is one of the agent's admins (its owner, a
    workspace owner, or an `AgentAdmin`). A runner speaks with its owner's
    authority, so this is "is the owner trusted with the whole agent?", asked of
    the box. The one predicate behind every place an agent's identity leaves
    canopy: claiming an agent turn, the per-turn GitHub token, credential
    resolve, and the routing writes that point work at a box.

    The agent's OWN canopy login (`Agent.user`) is NOT a holder: pairing a box
    as an agent has been refused since #1049, so a box owned by an agent login
    is a pre-#1049 leftover, and the agent's own login is no admin of it.

    Fails closed on a runner with no owner."""
    if getattr(runner, "owner_id", None) is None:
        return False
    return agent.is_admin(runner.owner)


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

    ws = agent.workspace.shared_vault_source() if agent.workspace else None
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

    fields = {
        "client_creds_ok": bool(client_creds_ok),
        "mailbox_ok": bool(mailbox_ok),
        "gog_client": (gog_client or "").strip(),
        "turn_client": (turn_client or "").strip(),
        "detail": (detail or "").strip()[:2000],
    }
    # The tri-states are written ONLY when the box actually looked. "I did not
    # check" is not an observation and must not overwrite one — the same rule
    # the runner's `projects` follows, for the same reason.
    #
    # They were written as None instead, which erased the last real answer: the
    # updater's credentials-only pass (every 30 minutes) does not run
    # `op inject`, so minutes after a full bootstrap recorded `env_ok` for all
    # five agents, every one of them read "not checked" (labs, 2026-09-24).
    if turn_ready is not None:
        fields["turn_ready"] = bool(turn_ready)
    if env_ok is not None:
        fields["env_ok"] = bool(env_ok)

    row, _ = AgentBootstrapReport.objects.update_or_create(
        agent=agent, runner_name=runner_name.strip(), defaults=fields,
    )
    return row


def bootstrap_reports(agent) -> list:
    """Every LIVE box's view of this agent, newest first.

    A retired runner's last report is left out. Rows key on the box's NAME (see
    `AgentBootstrapReport.runner_name`), so retiring a runner never touched them,
    and a box retired on 2026-10-04 (cloud-ec2-test) went on answering "can this
    agent run" for echo/eva/ada — a frozen all-green row from a machine that no
    longer exists, read as a second healthy box by anyone counting rows. A row is
    hidden only when EVERY runner of that name is retired: a report from a name
    with no runner row at all (a laptop that reports before it pairs) still shows,
    and unretiring the runner brings its row back, because nothing is deleted.
    """
    from apps.agents.models import AgentBootstrapReport
    from apps.harness.models import Runner

    retired = set(
        Runner.objects.filter(status=Runner.RETIRED).values_list("name", flat=True)
    ) - set(
        Runner.objects.exclude(status=Runner.RETIRED).values_list("name", flat=True)
    )
    return list(
        AgentBootstrapReport.objects.filter(agent=agent)
        .exclude(runner_name__in=retired)
        .order_by("-reported_at")
    )
