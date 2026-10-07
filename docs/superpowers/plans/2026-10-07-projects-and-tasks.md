# Projects and Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse canopy's task system to two nouns (Project, Task) and five actions, delete Items / Work products / Commands outright, and give projects a first-class page.

**Architecture:** One canopy-web PR mutates the schema to its final shape (one migration per app), replaces the item/command/work-product routes with task routes addressed by `(slug, ext_id)` plus one `actions` route, and rebuilds the agent UI around Projects → Tasks. Then the callers (canopy plugin, ada, eva) move to the new routes, one PR each. No compatibility layer anywhere.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2 + Postgres (pytest via `uv run pytest`); React 19 + Vite + Tailwind 4 + vitest; MCP tools auto-generated from the OpenAPI schema.

**Spec:** `docs/superpowers/specs/2026-10-07-projects-and-tasks-design.md`

## Global Constraints

- No backwards compatibility: no aliases, no shims, no redirects in the API. (UI path redirects for old bookmarks ARE in scope — they are navigation, not API.)
- Existing data is mutated to the final shape in the migration; `edit`/`reassign` command rows and all `AgentWorkProduct` rows are deleted.
- The words `item`, `work_product`, `command`, `decision` must not appear in any route path, schema name, or MCP tool name when done.
- Actions are exactly `approve | decline | reply | dispatch | done`. Viewer may `approve/decline/reply`; editor (`_agent_for_write`) required for `dispatch/done`.
- A task is addressed by `(agent slug, ext_id)`; `{ref}` in a task route is the `ext_id`.
- Task statuses stay `suggested | in_progress | done | declined`; project statuses stay `active | done | archived`.
- Project `links` shape is `[{label, url}]` (existing `AgentTaskLink`).
- Agent nav order: Projects (index), Tasks, Turns, Schedules, Huddles, Skills, Settings.
- canopy-web PRs: open with `gh pr merge <n> --auto`, never a strategy flag (merge queue).
- Commits end with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

## Review Focus

1. Approving a task whose `on_approve` spec is bad (unknown/cross-tenant `target_agent`) must 422 and leave the ask OPEN — the action row and status change roll back with it. (Task 3 test.)
2. Two people acting on the same open ask (double-click, two tabs): the second `approve`/`decline` gets 409, never a second dispatch. (Task 3 test.)
3. `reply` with an empty comment must 422 — an empty answer would close a question with nothing in it. (Task 3 test.)
4. `?waiting=me` must include live tasks parked on you with NO ask (`waiting_on_user` set), not just open asks — that is most of the fleet's real waiting work. (Task 4 test.)
5. A batch `POST /tasks/` re-posted with the same `idempotency_key`s replays (no duplicates) while new keys in the same batch are created. (Task 3 test.)

---

## Part 1 — canopy-web (one branch, one PR)

Work in `~/emdash/worktrees/canopy-web-projects-tasks` (branch `spec/projects-and-tasks`, already holds the spec). Rename the branch first: `git branch -m feat/projects-and-tasks`.

### Task 1: Schema to final shape (models + migration)

**Files:**
- Modify: `apps/agents/models.py` (AgentTask ask fields ~L720–795; `AgentTaskCommand` ~L848; delete `AgentWorkProduct` ~L504)
- Modify: `apps/agents/admin.py` (drop `AgentWorkProduct`, rename command admin)
- Create: `apps/agents/migrations/0038_projects_and_tasks_final_shape.py`
- Test: `tests/test_projects_tasks_migration.py`

**Interfaces:**
- Produces: `AgentTask.ask_closed_at: DateTimeField(null)`, `AgentTask.on_approve: JSONField(list)`, `AgentTask.ask_is_open` (property), `AgentTaskAction` model with constants `APPROVE, DECLINE, REPLY, DISPATCH, DONE`, `PENDING, APPLIED`, fields `agent, task, action, comment, by, by_user, status, applied_at, result_note, created_at`. Migration functions `forward_tasks(apps, schema_editor)` and `forward_actions(apps, schema_editor)` importable for tests.

- [ ] **Step 1: Write the failing test** (pure-function style, like `tests/test_task_uuid_migration.py`, plus a DB round-trip)

```python
# tests/test_projects_tasks_migration.py
"""agents.0038 moves tasks and board commands to their final shape."""
from __future__ import annotations

import importlib

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent, AgentTask, AgentTaskAction
from apps.workspaces.models import Workspace

mig = importlib.import_module("apps.agents.migrations.0038_projects_and_tasks_final_shape")

pytestmark = pytest.mark.django_db


def test_command_kinds_map_to_actions():
    assert mig.ACTION_FOR_KIND == {
        "accept": "approve", "decline": "decline", "comment": "reply",
        "dispatch": "dispatch", "done": "done",
    }
    # edit / reassign were field edits — they have no action and are deleted.
    assert "edit" not in mig.ACTION_FOR_KIND and "reassign" not in mig.ACTION_FOR_KIND


def test_comment_text_comes_from_note_or_reason():
    assert mig.comment_from_payload({"note": "looks good"}) == "looks good"
    assert mig.comment_from_payload({"reason": "dup"}) == "dup"
    assert mig.comment_from_payload({}) == ""


def test_final_task_shape_has_no_item_fields():
    names = {f.name for f in AgentTask._meta.get_fields()}
    assert {"ask_kind", "ask_body", "ask_closed_at", "on_approve", "dispatched_at"} <= names
    assert not names & {"uuid", "decision", "comment", "decided_by", "decided_by_user",
                        "decided_at", "ask_dismissed", "dispatch"}


def test_open_ask_is_keyed_on_closed_at():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w", display_name="W", created_by=u)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    t = AgentTask.objects.create(agent=agent, ext_id="T1", title="x", ask_kind="review")
    assert t.ask_is_open
    t.ask_closed_at = timezone.now()
    assert not t.ask_is_open
    assert AgentTaskAction.APPROVE == "approve"
```

- [ ] **Step 2: Run it — expect FAIL** (`ModuleNotFoundError` for 0038)

Run: `uv run pytest tests/test_projects_tasks_migration.py -v`

- [ ] **Step 3: Edit the models**

In `AgentTask`, replace the block from `uuid = models.UUIDField(...)` through `dispatched_at = ...` with:

```python
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
```

Keep `batch_key`, `idempotency_key`, `origin`, `origin_ref`, `raised_by`, `position`, `source`, timestamps. Replace `ask_state`/`ask_is_open` properties with:

```python
    @property
    def ask_is_open(self) -> bool:
        return bool(self.ask_kind) and self.ask_closed_at is None
```

Delete `IMPLEMENT/SKIP/DEFER`, `DECISION_CHOICES`, and `waiting_on_email` stays. Delete the `AgentWorkProduct` class. Replace `AgentTaskCommand` with:

```python
class AgentTaskAction(models.Model):
    """Something a person did to a task. One row is both the task's history and,
    while `pending`, the agent's to-do: it drains pending rows on its next turn
    and marks each applied."""

    APPROVE, DECLINE, REPLY, DISPATCH, DONE = "approve", "decline", "reply", "dispatch", "done"
    ACTION_CHOICES = [(a, a) for a in (APPROVE, DECLINE, REPLY, DISPATCH, DONE)]
    PENDING, APPLIED = "pending", "applied"
    STATUS_CHOICES = [(PENDING, "Pending"), (APPLIED, "Applied")]

    agent = models.ForeignKey(Agent, on_delete=models.CASCADE, related_name="task_actions")
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
    def agent_slug(self) -> str:
        return self.agent.slug

    @property
    def task_ext_id(self) -> str:
        return self.task.ext_id
```

In `admin.py` remove `AgentWorkProduct` registration and rename the command admin to `AgentTaskAction` (list_display `agent, task, action, status, created_at`).

- [ ] **Step 4: Generate then hand-finish the migration**

Run: `uv run python manage.py makemigrations agents -n projects_and_tasks_final_shape`
Then edit it so operations run in this order: (1) `AddField ask_closed_at`; (2) `RunPython(forward_tasks)`; (3) `RenameField dispatch→on_approve`; (4) `RemoveField` uuid, decision, comment, decided_by, decided_by_user, decided_at, ask_dismissed; (5) `RenameModel AgentTaskCommand→AgentTaskAction`, `RenameField kind→action`, `RenameField created_by→by`, `AddField comment/by_user`, `RunPython(forward_actions)`, `RemoveField payload`, `AlterField task` (CASCADE, non-null — delete rows with null task inside `forward_actions` first), `AlterField status` choices; (6) `DeleteModel AgentWorkProduct`. Module-level helpers:

```python
ACTION_FOR_KIND = {"accept": "approve", "decline": "decline", "comment": "reply",
                   "dispatch": "dispatch", "done": "done"}


def comment_from_payload(payload: dict) -> str:
    return str((payload or {}).get("note") or (payload or {}).get("reason") or "")


def forward_tasks(apps, schema_editor):
    Task = apps.get_model("agents", "AgentTask")
    Task.objects.filter(decided_at__isnull=False).update(ask_closed_at=models.F("decided_at"))


def forward_actions(apps, schema_editor):
    Action = apps.get_model("agents", "AgentTaskAction")
    Action.objects.filter(task__isnull=True).delete()
    Action.objects.exclude(action__in=list(ACTION_FOR_KIND)).delete()
    for row in Action.objects.all().iterator():
        row.action = ACTION_FOR_KIND[row.action]
        row.comment = comment_from_payload(row.payload)
        row.save(update_fields=["action", "comment"])
```

Also check `apps/harness/models.py` for an FK into `AgentWorkProduct` (grep `work_product`): `Turn.work_product_urls` is a JSON list and stays.

- [ ] **Step 5: Run migration + test — expect PASS**

Run: `uv run python manage.py migrate && uv run pytest tests/test_projects_tasks_migration.py -v`

- [ ] **Step 6: Commit** — `git commit -am "feat(agents): tasks and actions in their final shape; drop work products"` (whole suite is red until Task 4; that is expected on this branch).

---

### Task 2: dispatch() reads a task and the action that approved it

**Files:**
- Modify: `apps/harness/dispatch.py` (`dispatch`, `_with_reply`, `_dispatch_initiator`; delete `_is_task` and the `item` branches)
- Test: `tests/test_task_dispatch.py` (replaces `tests/test_item_dispatch.py` — delete that file)

**Interfaces:**
- Consumes: `AgentTask.on_approve`, `AgentTaskAction(comment, by_user)`.
- Produces: `dispatch(task, *, action, actor_workspace_slugs: set[str]) -> list[Turn]` — idempotency key `f"task-{task.pk}-{i}"`, prompt carries `action.comment`, initiator is `action.by_user` when set else the agent.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_task_dispatch.py
import pytest
from django.contrib.auth.models import User

from apps.agents.models import Agent, AgentTask, AgentTaskAction
from apps.harness.dispatch import dispatch
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    task = AgentTask.objects.create(agent=agent, ext_id="T1", title="Send EOI",
                                    ask_kind="review", on_approve=[{"prompt": "/eva:turn send it"}])
    action = AgentTaskAction(agent=agent, task=task, action="approve",
                             comment="yes, cc Neal", by="jj@dimagi.com", by_user=u)
    return u, ws, agent, task, action


def test_dispatch_carries_the_reply_and_is_idempotent(world):
    _u, ws, _agent, task, action = world
    first = dispatch(task, action=action, actor_workspace_slugs={ws.id})
    again = dispatch(task, action=action, actor_workspace_slugs={ws.id})
    assert len(first) == 1 and first[0].pk == again[0].pk
    assert "yes, cc Neal" in first[0].prompt
    assert first[0].raised_from_task_id == task.pk


def test_unknown_target_raises(world):
    _u, ws, _agent, task, action = world
    task.on_approve = [{"prompt": "x", "target_agent": "nobody"}]
    with pytest.raises(ValueError, match="unknown target_agent"):
        dispatch(task, action=action, actor_workspace_slugs={ws.id})
```

(Check how the existing `dispatch` compares `target.workspace_id` to `actor_workspace_slugs` — it holds workspace **ids** despite the name; keep that and rename the parameter to `actor_workspace_ids` in this task, updating its one caller in Task 3.)

- [ ] **Step 2: Run — expect FAIL** (`unexpected keyword argument 'action'`)

Run: `uv run pytest tests/test_task_dispatch.py -v`

- [ ] **Step 3: Implement** — change the signature to `dispatch(task, *, action, actor_workspace_ids)`; iterate `task.on_approve`; `idempotency_key=f"task-{task.pk}-{i}"`; `prompt=_with_reply(brief, action.comment)`; `initiator=_dispatch_initiator(task, action)` where:

```python
def _dispatch_initiator(task, action):
    from . import initiator as who
    via = f"task:{task.agent.slug}/{task.ext_id}"
    if action.by_user_id or getattr(action, "by_user", None) is not None:
        return who.for_user(action.by_user, via=via, assurance=who.APPROVAL)
    return who.for_agent(task.agent.slug, via=via)
```

`_with_reply(prompt, reply: str)` takes the text directly (keep its existing formatting body, replacing `item.comment` with `reply`). Always set `turn.raised_from_task = task`. `origin_ref.setdefault("task_title", task.title)` (rename from `item_title`; grep the runner for `item_title` — `runner/` and `apps/harness` — and rename every reader).

- [ ] **Step 4: Run — expect PASS.** `uv run pytest tests/test_task_dispatch.py -v`
- [ ] **Step 5: Commit** — `git rm tests/test_item_dispatch.py && git commit -am "refactor(dispatch): a task and the action that approved it"`

---

### Task 3: Task services — create, act, filter, drain

**Files:**
- Modify: `apps/agents/services.py` (replace L556–935 task/ask/command section; delete `upsert_work_products`, `list_work_products`, `sync_tasks`, `raise_asks`, `decide_ask`, `dismiss_ask`, `create_command`, `list_commands`, `apply_command`, `get_task(int)`; `work_product_count` in the agent summary ~L182)
- Modify: `apps/harness/schedule_turns.py` L300–345 (the nag raises via `create_tasks`, retires via `act(..., "decline")`)
- Modify: `apps/harness/services.py` ~L1882 (delete the stale Item comment)
- Test: `tests/test_task_actions.py` (new); delete `tests/test_task_asks.py`, `tests/test_item_services.py`, `tests/test_item_models.py`, `tests/test_item_schemas.py`

**Interfaces:**
- Consumes: `dispatch(task, *, action, actor_workspace_ids)` (Task 2).
- Produces (all in `apps.agents.services`):
  - `create_tasks(agent, payloads: list[dict]) -> list[AgentTask]` — atomic batch, per-row savepoint, replays on `idempotency_key`, `ext_id` defaults to `next_task_ext_id(agent)`, `project` resolved by `get_project` (unknown → None), accepts every `_TASK_FIELDS` key plus `ask_kind, ask_body, on_approve, batch_key, idempotency_key, origin, origin_ref, raised_by, waiting_on_email, links`.
  - `get_task(agent, ref: str) -> AgentTask | None` (by `ext_id__iexact`).
  - `act(task, *, action: str, comment: str = "", by: str, by_user=None, actor_workspace_ids: set) -> tuple[AgentTask, AgentTaskAction, list[Turn]]`; raises `ClosedAskError` (409) and `ValueError` (422).
  - `filter_tasks(qs, *, user=None, project: str = "", status: str = "", waiting: str = "", ask: str = "", batch: str = "") -> QuerySet`.
  - `pending_actions(agent) -> QuerySet[AgentTaskAction]`, `mark_applied(action_row, result_note="") -> AgentTaskAction`.
  - `waiting_q()` unchanged in meaning, rewritten on `ask_closed_at`.

- [ ] **Step 1: Write the failing tests** (fixture `world` as in Task 2 without the task)

```python
# tests/test_task_actions.py
import pytest
from django.contrib.auth.models import User

from apps.agents import services
from apps.agents.models import Agent, AgentTask, AgentTaskAction
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    return u, ws, agent


def _review(agent, **over):
    p = {"title": "Send the EOI?", "ask_kind": "review", "idempotency_key": "k1"}
    p.update(over)
    return services.create_tasks(agent, [p])[0]


def _act(task, u, ws, action, comment=""):
    return services.act(task, action=action, comment=comment, by=u.email, by_user=u,
                        actor_workspace_ids={ws.id})


def test_batch_create_replays_by_key_and_creates_new(world):
    _u, _ws, agent = world
    a = services.create_tasks(agent, [{"title": "A", "idempotency_key": "k1"}])
    b = services.create_tasks(agent, [{"title": "A", "idempotency_key": "k1"},
                                      {"title": "B", "idempotency_key": "k2"}])
    assert b[0].pk == a[0].pk and b[1].ext_id == "T2"
    assert agent.tasks.count() == 2


def test_approve_without_on_approve_leaves_work_pending(world):
    u, ws, agent = world
    task, row, turns = _act(_review(agent), u, ws, "approve")
    assert task.status == AgentTask.IN_PROGRESS and not task.ask_is_open
    assert row.status == AgentTaskAction.PENDING and turns == []


def test_approve_with_on_approve_dispatches_and_is_applied(world):
    u, ws, agent = world
    task = _review(agent, on_approve=[{"prompt": "/eva:turn go"}])
    task, row, turns = _act(task, u, ws, "approve", "go")
    assert len(turns) == 1 and row.status == AgentTaskAction.APPLIED
    assert task.dispatched_at is not None


def test_bad_on_approve_rolls_back_and_ask_stays_open(world):
    u, ws, agent = world
    task = _review(agent, on_approve=[{"prompt": "x", "target_agent": "nobody"}])
    with pytest.raises(ValueError):
        _act(task, u, ws, "approve")
    task.refresh_from_db()
    assert task.ask_is_open and task.status == AgentTask.SUGGESTED
    assert not task.actions.exists() and not Turn.objects.exists()


def test_second_approve_on_closed_ask_is_refused(world):
    u, ws, agent = world
    task = _review(agent)
    _act(task, u, ws, "decline", "dup")
    with pytest.raises(services.ClosedAskError):
        _act(task, u, ws, "approve")


def test_decline_closes_and_keeps_reason(world):
    u, ws, agent = world
    task, row, _ = _act(_review(agent), u, ws, "decline", "duplicate of T4")
    assert task.status == AgentTask.DECLINED and row.comment == "duplicate of T4"
    assert row.status == AgentTaskAction.APPLIED


def test_reply_answers_a_question_but_not_a_review(world):
    u, ws, agent = world
    q = _review(agent, ask_kind="question", idempotency_key="q")
    q, row, _ = _act(q, u, ws, "reply", "Tuesday")
    assert not q.ask_is_open and row.status == AgentTaskAction.PENDING
    r = _review(agent, idempotency_key="r")
    r, _row, _ = _act(r, u, ws, "reply", "what's the budget?")
    assert r.ask_is_open


def test_empty_reply_is_refused(world):
    u, ws, agent = world
    with pytest.raises(ValueError):
        _act(_review(agent), u, ws, "reply", "  ")


def test_dispatch_is_pending_and_done_closes(world):
    u, ws, agent = world
    t = _review(agent)
    _t, row, _ = _act(t, u, ws, "dispatch")
    assert row.status == AgentTaskAction.PENDING and t.ask_is_open
    t, _row, _ = _act(t, u, ws, "done")
    assert t.status == AgentTask.DONE and not t.ask_is_open


def test_drain_queue(world):
    u, ws, agent = world
    _t, row, _ = _act(_review(agent), u, ws, "approve")
    assert list(services.pending_actions(agent)) == [row]
    services.mark_applied(row, "sent")
    assert not services.pending_actions(agent).exists()
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: create_tasks`). `uv run pytest tests/test_task_actions.py -v`

- [ ] **Step 3: Implement.** Core of `act`:

```python
class ClosedAskError(Exception):
    """The ask is already closed — acting again would dispatch twice."""


#: (closes ask?, new status or None, needs agent follow-up?)
_EFFECT = {
    AgentTaskAction.APPROVE: (True, AgentTask.IN_PROGRESS, True),
    AgentTaskAction.DECLINE: (True, AgentTask.DECLINED, False),
    AgentTaskAction.REPLY: (None, None, True),   # closes only a question
    AgentTaskAction.DISPATCH: (False, None, True),
    AgentTaskAction.DONE: (True, AgentTask.DONE, False),
}


@transaction.atomic
def act(task, *, action, comment="", by, by_user=None, actor_workspace_ids):
    from apps.harness.dispatch import dispatch

    if action not in _EFFECT:
        raise ValueError(f"action must be one of {', '.join(_EFFECT)}, got {action!r}")
    comment = (comment or "").strip()
    if action == AgentTaskAction.REPLY and not comment:
        raise ValueError("a reply needs words")
    closes, status, follow_up = _EFFECT[action]
    if action == AgentTaskAction.REPLY:
        closes = task.ask_kind == AgentTask.ASK_QUESTION and task.ask_is_open
    if action in (AgentTaskAction.APPROVE, AgentTaskAction.DECLINE) and task.ask_kind \
            and not task.ask_is_open:
        raise ClosedAskError(f"{task.agent.slug}/{task.ext_id} has no open ask")

    row = AgentTaskAction(agent=task.agent, task=task, action=action, comment=comment, by=by,
                          by_user=by_user if getattr(by_user, "is_authenticated", False) else None)
    turns = []
    runs = action == AgentTaskAction.APPROVE or (action == AgentTaskAction.REPLY and closes)
    if runs and task.on_approve:
        turns = dispatch(task, action=row, actor_workspace_ids=actor_workspace_ids)
        task.dispatched_at = timezone.now()
        follow_up = False  # the dispatched turn IS the follow-up
    if closes and task.ask_kind:
        task.ask_closed_at = timezone.now()
        task.waiting_on_user = None
    if status:
        task.status = status
    elif runs and turns:
        task.status = AgentTask.IN_PROGRESS
    task.save()
    row.status = AgentTaskAction.PENDING if follow_up else AgentTaskAction.APPLIED
    row.applied_at = None if follow_up else timezone.now()
    row.save()
    return task, row, turns
```

`filter_tasks`:

```python
def filter_tasks(qs, *, user=None, project="", status="", waiting="", ask="", batch=""):
    from django.db.models import Q
    if project == "none":
        qs = qs.filter(project__isnull=True)
    elif project:
        qs = qs.filter(project__ext_id__iexact=project)
    if status:
        qs = qs.filter(status__in=status.split(","))
    if waiting == "me":
        qs = qs.filter(waiting_q()).filter(
            Q(waiting_on_user=user) | (~Q(ask_kind="") & Q(ask_closed_at__isnull=True)
                                       & Q(waiting_on_user__isnull=True)))
    if ask == "open":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=True)
    elif ask == "closed":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=False)
    if batch:
        qs = qs.filter(batch_key=batch)
    return qs
```

(An unrouted open ask — `waiting_on_user` null — counts as waiting on whoever views it, matching today's inbox. Add `test_waiting_me_includes_parked_and_unrouted_asks` to this file: a task with no ask but `waiting_on_user=u` and a task with an open ask and no user both appear; a task parked on another user does not.)

`waiting_q()` keeps its body with `Q(decided_at__isnull=True)` → `Q(ask_closed_at__isnull=True)`. `schedule_turns.py`: raise with `services.create_tasks(schedule.agent, [{...same keys, "on_approve": ...}])`; retire with `services.act(task, action="decline", by="system:schedule", actor_workspace_ids=set())` over `AgentTask.objects.filter(ask_closed_at__isnull=True, origin_ref__schedule_id=schedule_id)`.

- [ ] **Step 4: Run — expect PASS.** `uv run pytest tests/test_task_actions.py tests/test_schedule_nag.py -v` (fix `test_schedule_nag.py` asserts from `decided_at`/`ask_dismissed` to `ask_closed_at` + a `decline` action row).
- [ ] **Step 5: Commit** — `git rm tests/test_task_asks.py tests/test_item_services.py tests/test_item_models.py tests/test_item_schemas.py && git commit -am "feat(agents): one action set on tasks; batch create; filters; drain queue"`

---

### Task 4: Routes — tasks by ref, actions, fleet lists, project detail; delete the rest

**Files:**
- Modify: `apps/agents/schemas.py` (task/project/action schemas; delete `AgentWorkProduct*`, `AgentTaskSyncIn`, `AgentTaskCommand*`, `CommandResultOut`, `AgentCommandApplyIn`, `work_product_count`)
- Modify: `apps/agents/api.py` (L1236–1260 work products; L1363–1530 projects/tasks/commands)
- Create: `apps/agents/fleet_api.py` (`/api/tasks/`, `/api/projects/`)
- Delete: `apps/harness/items_api.py`; its schemas in `apps/harness/schemas.py` (`ItemIn`, `ItemOut`, `ItemDecideIn`, `ItemDismissIn`)
- Modify: `apps/agent_runs/documents.py` (delete `list_agent_projects` + `AgentProjectRefOut` if unused elsewhere)
- Modify: `apps/api/api.py` (L205–257 routers), `apps/api/route_gates.py` (gate entries for removed/added operations)
- Modify: `apps/agents/timeline.py` (work-product source → remove)
- Test: `tests/test_task_routes.py` (new), `tests/test_vocabulary_guard.py` (new); delete `tests/test_items_api.py`, `tests/test_fleet_inbox_items.py`, `tests/test_item_invalidation.py`, `tests/test_push_items.py`, `apps/mcp/tests/test_item_tools.py`; update `tests/test_agents.py`, `tests/test_agent_acl_gates.py`, `tests/test_acl_escalations.py`, `tests/test_agent_projects.py`, `tests/test_pagination_limits.py`, `tests/test_page_invalidation.py`, `tests/test_push_trigger.py`, `tests/test_self_host.py`, `tests/test_mobile_loop_e2e.py`, `tests/test_turn_initiator.py`, `tests/test_slow_request_log.py` to the new routes.

**Interfaces:**
- Consumes: Task 3 services.
- Produces (OpenAPI operation ids become MCP tool names): `list_tasks`, `create_tasks`, `get_task`, `patch_task`, `act_on_task`, `list_task_actions` (agent queue), `mark_task_action_applied`, `list_projects`, `create_project`, `get_project`, `patch_project`, `list_fleet_tasks`, `list_fleet_projects`.
- Schemas: `AgentTaskOut` (drop `id` and `uuid` — `ext_id` is the address; add `ask_open: bool`, `ask_closed_at`, `on_approve`, `dispatched_at`, `batch_key`, `origin`, `waiting_on_email`, `project_ext_id`, `project_name`), `AgentTaskIn` (all create keys from Task 3, `ext_id` optional), `AgentTaskPatch` (unchanged minus nothing), `AgentTaskActionIn {action: Literal[...], comment: str = ""}`, `AgentTaskActionOut {id, agent_slug, task_ext_id, action, comment, by, status, applied_at, result_note, created_at}`, `AgentTaskDetailOut(AgentTaskOut) + actions: list[AgentTaskActionOut]`, `ActOut {task: AgentTaskOut, action: AgentTaskActionOut, turn_ids: list[int]}`, `ActionAppliedIn {result_note: str = ""}`, `AgentProjectDetailOut(AgentProjectOut) + tasks: list[AgentTaskOut], recent_turns: list[TurnBriefOut]` where `TurnBriefOut {id, status, prompt_preview, created_at, task_ext_ids}` (reuse an existing turn brief schema from `apps/harness/schemas.py` if one fits — grep `class .*Turn.*Out`).

- [ ] **Step 1: Write the failing route tests**

```python
# tests/test_task_routes.py
import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def c():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    client = Client()
    client.force_login(u)
    return client, agent, u


def test_create_list_filter_act(c):
    client, agent, _u = c
    AgentProject.objects.create(agent=agent, ext_id="P1", name="IDM talk")
    r = client.post("/api/agents/eva/tasks/", [
        {"title": "Draft deck", "project": "P1"},
        {"title": "Send EOI?", "ask_kind": "review", "idempotency_key": "k"},
    ], content_type="application/json")
    assert r.status_code == 201, r.content
    assert [t["ext_id"] for t in r.json()] == ["T1", "T2"]
    assert len(client.get("/api/agents/eva/tasks/?project=P1").json()) == 1
    assert len(client.get("/api/agents/eva/tasks/?project=none").json()) == 1
    assert [t["ext_id"] for t in client.get("/api/agents/eva/tasks/?ask=open").json()] == ["T2"]
    r = client.post("/api/agents/eva/tasks/T2/actions", {"action": "approve"},
                    content_type="application/json")
    assert r.status_code == 200 and r.json()["task"]["status"] == "in_progress"
    assert client.post("/api/agents/eva/tasks/T2/actions", {"action": "decline"},
                       content_type="application/json").status_code == 409
    queue = client.get("/api/agents/eva/actions/?status=pending").json()
    assert [a["task_ext_id"] for a in queue] == ["T2"]
    r = client.post(f"/api/agents/eva/actions/{queue[0]['id']}/applied", {},
                    content_type="application/json")
    assert r.json()["status"] == "applied"


def test_empty_reply_is_422(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": ""},
                    content_type="application/json")
    assert r.status_code == 422


def test_get_project_has_four_sections(c):
    client, agent, _u = c
    AgentProject.objects.create(agent=agent, ext_id="P1", name="IDM talk",
                                links=[{"label": "deck", "url": "https://x"}])
    body = client.get("/api/agents/eva/projects/P1/").json()
    assert {"name", "outcome", "status", "tasks", "recent_turns", "links"} <= body.keys()


def test_fleet_routes(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    assert [t["agent_slug"] for t in client.get("/api/tasks/?waiting=me").json()] == ["eva"]
    assert client.get("/api/projects/").status_code == 200


@pytest.mark.parametrize("path", [
    "/api/items/", "/api/agents/eva/items/", "/api/agents/eva/work-products/",
    "/api/agents/eva/tasks/waiting/", "/api/agents/eva/commands", "/api/agent-runs/projects/",
])
def test_removed_routes_are_gone(c, path):
    client, _agent, _u = c
    assert client.get(path).status_code in (404, 405)
```

```python
# tests/test_vocabulary_guard.py
"""The retired nouns cannot creep back into the API (and so the MCP tools)."""
import re

from apps.api.api import api

BANNED = re.compile(r"(^|[_/-])(items?|work[_-]?products?|commands?|decisions?)($|[_/-])")


def test_no_retired_noun_in_paths_or_operations():
    schema = api.get_openapi_schema()
    hits = [p for p in schema["paths"] if BANNED.search(p.lower())]
    ops = [op["operationId"] for p in schema["paths"].values() for op in p.values()
           if isinstance(op, dict) and "operationId" in op]
    hits += [o for o in ops if BANNED.search(o.lower())]
    hits += [n for n in schema.get("components", {}).get("schemas", {})
             if re.search(r"Item(In|Out)|WorkProduct|Command|Decision", n)]
    assert hits == []
```

(The guard may flag unrelated pre-existing names — e.g. a reviews/walkthroughs schema with `items`. If so, narrow the regex to the agents/tasks surface by filtering paths under `/api/agents/`, `/api/tasks`, `/api/projects`, and list the unrelated names in an explicit `ALLOWED` set with a one-line reason each. Do not rename unrelated features.)

- [ ] **Step 2: Run — expect FAIL.** `uv run pytest tests/test_task_routes.py tests/test_vocabulary_guard.py -v`

- [ ] **Step 3: Implement the routes** in `apps/agents/api.py`:

```python
def _get_task_or_404(agent, ref: str):
    task = services.get_task(agent, ref)
    if task is None:
        raise HttpError(404, f"task {ref} not found")
    return task


@router.get("/{slug}/tasks/", response=list[AgentTaskOut], summary="List the agent's tasks",
            operation_id="list_tasks")
def list_tasks(request, slug: str, project: str = "", status: str = "", waiting: str = "",
               ask: str = "", batch: str = ""):
    agent = _get_agent_or_404(request, slug)
    qs = services.filter_tasks(agent.tasks.select_related("agent", "project"),
                               user=request.user, project=project, status=status,
                               waiting=waiting, ask=ask, batch=batch)
    return [AgentTaskOut.model_validate(t) for t in qs]


@router.post("/{slug}/tasks/", response={201: list[AgentTaskOut]},
             summary="Create tasks (a list; idempotency_key replays)", operation_id="create_tasks")
def create_tasks(request, slug: str, payload: list[AgentTaskIn]):
    agent = _get_agent_or_404(request, slug)
    # Writing a task that dispatches work is the reshaping tier — an editor, or
    # the agent itself under its own login (carried over from create_items).
    is_self = agent.user_id is not None and agent.user_id == request.user.pk
    if not is_self:
        agent = _agent_for_write(request, slug)
    try:
        tasks = services.create_tasks(agent, [p.model_dump(exclude_unset=True) for p in payload])
    except services.UnknownPersonError as exc:
        raise HttpError(422, str(exc)) from exc
    return 201, [AgentTaskOut.model_validate(t) for t in tasks]


@router.get("/{slug}/tasks/{ref}/", response=AgentTaskDetailOut, operation_id="get_task")
def get_task(request, slug: str, ref: str):
    agent = _get_agent_or_404(request, slug)
    return AgentTaskDetailOut.model_validate(_get_task_or_404(agent, ref))


@router.post("/{slug}/tasks/{ref}/actions", response=ActOut,
             summary="Act on a task: approve, decline, reply, dispatch or done",
             operation_id="act_on_task")
def act_on_task(request, slug: str, ref: str, payload: AgentTaskActionIn):
    if payload.action in ("dispatch", "done"):
        agent = _agent_for_write(request, slug)
    else:
        agent = _get_agent_or_404(request, slug)
    task = _get_task_or_404(agent, ref)
    try:
        task, row, turns = services.act(
            task, action=payload.action, comment=payload.comment,
            by=request.user.email or request.user.get_username(), by_user=request.user,
            actor_workspace_ids=_visible_agent_workspace_ids(request))
    except services.ClosedAskError as exc:
        raise HttpError(409, str(exc)) from exc
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    return ActOut(task=AgentTaskOut.model_validate(task),
                  action=AgentTaskActionOut.model_validate(row),
                  turn_ids=[t.id for t in turns])
```

Plus `GET /{slug}/actions/` (`list_task_actions`, `?status=`), `POST /{slug}/actions/{id}/applied` (`mark_task_action_applied`, agent self or editor — copy the gate from the deleted `apply_command`), `PATCH /{slug}/tasks/{ref}/` (body of old `patch_task`, `ref` lookup). `get_project` returns `AgentProjectDetailOut` with `tasks` = project tasks ordered live-first, and `recent_turns` = last 10 `Turn`s for the agent whose `task_ext_ids` overlap the project's ext_ids (`Turn.objects.filter(agent=agent, task_ext_ids__overlap=ids)` — check the field type in `apps/harness/models.py`; if it is a JSONField use `Q(task_ext_ids__contains=[id])` OR-ed per id). Check `_visible_agent_workspace_ids` returns ids — it is what `dispatch` compares against.

`apps/agents/fleet_api.py`:

```python
from ninja import Router
from apps.api.auth import session_auth
from apps.agents import services
from apps.agents.api import _visible_agent_workspace_ids
from apps.agents.models import AgentProject, AgentTask
from apps.agents.schemas import AgentProjectOut, AgentTaskOut

router = Router(auth=session_auth, tags=["tasks"])


@router.get("/tasks/", response=list[AgentTaskOut], operation_id="list_fleet_tasks",
            summary="Tasks across every agent you can see (same filters + agent)")
def list_fleet_tasks(request, agent: str = "", project: str = "", status: str = "",
                     waiting: str = "", ask: str = "", batch: str = ""):
    qs = AgentTask.objects.filter(agent__workspace_id__in=_visible_agent_workspace_ids(request))
    if agent:
        qs = qs.filter(agent__slug=agent)
    qs = services.filter_tasks(qs.select_related("agent", "project"), user=request.user,
                               project=project, status=status, waiting=waiting, ask=ask, batch=batch)
    return [AgentTaskOut.model_validate(t) for t in qs.order_by("ask_kind", "created_at")]


@router.get("/projects/", response=list[AgentProjectOut], operation_id="list_fleet_projects",
            summary="Projects across every agent you can see")
def list_fleet_projects(request, status: str = "active", repo_slug: str = ""):
    qs = AgentProject.objects.filter(agent__workspace_id__in=_visible_agent_workspace_ids(request))
    if status:
        qs = qs.filter(status=status)
    if repo_slug:
        qs = qs.filter(repo_slug=repo_slug)
    return [AgentProjectOut.model_validate(p) for p in qs.select_related("agent", "owner_user")]
```

Mount in `apps/api/api.py`: `api.add_router("", fleet_router)`; remove the two item routers. Order the fleet tasks review-before-question: replace `.order_by("ask_kind", ...)` with a `Case` on `ask_kind` (`review`=0, `question`=1, else 2) then `created_at`. Add/remove `route_gates.py` entries to match operation ids (follow the file's existing tier tuples; `act_on_task`/`create_tasks`/`mark_task_action_applied` mirror the gates of the routes they replace).

- [ ] **Step 4: Fix the remaining suite.** Run `uv run pytest -x -q`; update each listed test file to the new route/field names (mechanical: `/items/{uuid}/decide {"decision":"implement"}` → `/tasks/{ext_id}/actions {"action":"approve"}`; `skip`/`dismiss` → `decline`; question answers → `reply`; `decided_at` → `ask_closed_at`; `/tasks/{id}/commands {"kind":k}` → `/tasks/{ext_id}/actions {"action":ACTION_FOR_KIND[k]}`, `edit`/`reassign` → `PATCH`). Delete tests whose only subject was a removed thing (listed above). Update `apps/push/*` and `apps/realtime/snapshot.py` only if a field name changed under them (they call `waiting_q()`).
- [ ] **Step 5: Run full backend — expect PASS.** `uv run pytest -q`
- [ ] **Step 6: Commit** — `git commit -am "feat(api): tasks by ref, one actions route, fleet tasks/projects; items, commands, work products removed"`

---

### Task 5: Frontend API client

**Files:**
- Regenerate: `frontend/src/api/generated.ts` (`cd frontend && npm run gen:api`)
- Modify: `frontend/src/api/agents.ts` (delete work-product, command, waiting, sync functions; add task/action/project-detail/fleet functions)
- Delete: `frontend/src/api/items.ts`, `frontend/src/lib/itemBands.ts`
- Test: `frontend/src/api/agents.test.ts` (new, mocks `apiV2`)

**Interfaces — produces:**

```ts
export type TaskOut = Schemas['AgentTaskOut']
export type TaskDetailOut = Schemas['AgentTaskDetailOut']
export type TaskActionOut = Schemas['AgentTaskActionOut']
export type TaskAction = 'approve' | 'decline' | 'reply' | 'dispatch' | 'done'
export type ProjectOut = Schemas['AgentProjectOut']
export type ProjectDetailOut = Schemas['AgentProjectDetailOut']
export interface TaskFilters { project?: string; status?: string; waiting?: 'me'; ask?: 'open' | 'closed'; batch?: string }
export function listTasks(slug: string, f?: TaskFilters): Promise<TaskOut[]>
export function listFleetTasks(f?: TaskFilters & { agent?: string }): Promise<TaskOut[]>
export function getTask(slug: string, ref: string): Promise<TaskDetailOut>
export function actOnTask(slug: string, ref: string, action: TaskAction, comment?: string): Promise<{ task: TaskOut; action: TaskActionOut; turn_ids: number[] }>
export function patchTask(slug: string, ref: string, body: Schemas['AgentTaskPatch']): Promise<TaskOut>
export function listProjects(slug: string, status?: string): Promise<ProjectOut[]>
export function getProject(slug: string, ref: string): Promise<ProjectDetailOut>
export function createProject(slug: string, body: { name: string; outcome: string }): Promise<ProjectOut>
export function patchProject(slug: string, ref: string, body: Schemas['AgentProjectPatch']): Promise<ProjectOut>
export function listTaskActions(slug: string, status?: 'pending' | 'applied'): Promise<TaskActionOut[]>
```

Rename the existing `AgentTaskOut`/`listAgentProjects`/`createAgentProject`/`patchAgentProject` exports to the names above and update importers (grep). Each function follows the file's existing `apiV2.GET(...)` + `unwrap(res, '<name>')` pattern.

- [ ] **Step 1: Write the failing test**

```ts
// frontend/src/api/agents.test.ts
import { describe, expect, it, vi } from 'vitest'
const GET = vi.fn(async () => ({ data: [], error: undefined, response: new Response() }))
const POST = vi.fn(async () => ({ data: { task: {}, action: {}, turn_ids: [] }, error: undefined, response: new Response() }))
vi.mock('@/api/client.v2', () => ({ apiV2: { GET, POST } }))
const { listTasks, actOnTask } = await import('./agents')

describe('task client', () => {
  it('passes filters as query params', async () => {
    await listTasks('eva', { waiting: 'me', project: 'P2' })
    expect(GET).toHaveBeenCalledWith('/api/agents/{slug}/tasks/', {
      params: { path: { slug: 'eva' }, query: { waiting: 'me', project: 'P2' } },
    })
  })
  it('acts by ext_id', async () => {
    await actOnTask('eva', 'T2', 'reply', 'Tuesday')
    expect(POST).toHaveBeenCalledWith('/api/agents/{slug}/tasks/{ref}/actions', {
      params: { path: { slug: 'eva', ref: 'T2' } }, body: { action: 'reply', comment: 'Tuesday' },
    })
  })
})
```

(Match the mock path to how `agents.ts` imports `apiV2` — check line 1–12.)

- [ ] **Step 2: Run — FAIL.** `cd frontend && npx vitest run src/api/agents.test.ts`
- [ ] **Step 3: Implement**, `npm run gen:api` first.
- [ ] **Step 4: Run — PASS.** Then `npx tsc --noEmit` will list every broken importer — that list is Tasks 6–10's work; do not fix them here.
- [ ] **Step 5: Commit** — `git commit -am "feat(frontend): task/project/action client; items client removed"`

---

### Task 6: One TaskCard with the action set

**Files:**
- Modify: `frontend/src/components/TasksBoard.tsx` (`TaskCard` ~L427–570, action runner ~L300–420, `AgentActivity` ~L602, pending strip ~L573)
- Delete: `frontend/src/components/items/ItemCard.tsx`, `ItemCard.test.tsx`; move `ItemAge.tsx` → `frontend/src/components/TaskAge.tsx` (rename component `TaskAge`, prop `closedAt`), its test likewise
- Test: `frontend/src/components/TaskCard.test.tsx` (new)

**Interfaces:**
- Consumes: `actOnTask`, `TaskOut`, `TaskActionOut` (Task 5).
- Produces: `TaskCard({ task, onChanged, canEdit, showAgent? }: {...}): JSX.Element` and `availableActions(task: TaskOut, canEdit: boolean): TaskAction[]` (exported for tests). `TasksBoard({ tasks, actions, onChanged, canEdit })` — `commands` prop becomes `actions: TaskActionOut[]`.

Rules for `availableActions`: open review → `['approve','decline']`; open question → `['reply','decline']`; live task (`suggested`/`in_progress`) with no open ask → `['reply']`; plus `canEdit && live` → append `'dispatch','done'`; done/declined → `[]`. The reply box shows when `reply` is available; on a question its button reads **Answer** (or **Answer & run** when `on_approve` is non-empty); on a review/live task it reads **Reply**. **Approve** reads **Approve & run** when `on_approve` is non-empty. Keep the `min-h-11 sm:min-h-0` touch sizing from `ItemCard`. Errors from the API (409/422) render inline in `text-destructive` and the card refetches via `onChanged` on 409.

- [ ] **Step 1: Write the failing test**

```tsx
// frontend/src/components/TaskCard.test.tsx
import { describe, expect, it } from 'vitest'
import { availableActions } from './TasksBoard'

const t = (o: object) => ({ status: 'suggested', ask_kind: '', ask_open: false, on_approve: [], ...o }) as never

describe('availableActions', () => {
  it('open review', () => expect(availableActions(t({ ask_kind: 'review', ask_open: true }), false)).toEqual(['approve', 'decline']))
  it('open question', () => expect(availableActions(t({ ask_kind: 'question', ask_open: true }), false)).toEqual(['reply', 'decline']))
  it('live task, editor', () => expect(availableActions(t({ status: 'in_progress' }), true)).toEqual(['reply', 'dispatch', 'done']))
  it('finished', () => expect(availableActions(t({ status: 'done' }), true)).toEqual([]))
})
```

Add a render test: a question card, type "Tuesday", click **Answer** → `actOnTask('eva','T2','reply','Tuesday')` called (mock `@/api/agents`).

- [ ] **Step 2: Run — FAIL.** `npx vitest run src/components/TaskCard.test.tsx`
- [ ] **Step 3: Implement** — port the ask block (title, `TaskAge`, `Markdown` body, reply input, buttons) from `ItemCard` into `TaskCard`; replace `postTaskCommand(...)` calls with `actOnTask(task.agent_slug, task.ext_id, action, comment)`; replace edit/reassign command UI with `patchTask`. `AgentActivity` and the "N queued" strip read `TaskActionOut` (`a.action`, `a.task_ext_id`, `a.status === 'pending'`).
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(frontend): one TaskCard, five actions"`

---

### Task 7: Tasks page with the filter bar; nav and redirects

**Files:**
- Rename: `frontend/src/pages/agents/AgentWorkSection.tsx` → `AgentTasksSection.tsx` (and its test)
- Delete: `pages/agents/InboxSection.tsx` (+test), `pages/agents/ItemsSection.tsx`, `pages/agents/WorkRedirect.tsx`
- Modify: `frontend/src/components/agents/AgentLeftNav.tsx` (items array ~L37–62), `frontend/src/router.tsx` (L302–345), `frontend/src/pages/AgentWorkspacePage.tsx` (waiting count source)
- Test: `frontend/src/pages/agents/AgentTasksSection.test.tsx`

**Interfaces:**
- Consumes: `listTasks`, `listTaskActions`, `TaskCard`, `TasksBoard`, `ProjectGroupHeader` (from `projectParts.tsx`).
- Produces: `AgentTasksSection()`; URL state: `?waiting=me`, `?view=open|done`, `?project=P2|none`, `?by=project`.

Filter bar (top of page): three toggle chips **Waiting on you · N** (`waiting=me`), **Open** (default; `status=suggested,in_progress`), **Done** (`status=done,declined`); a `<select>` Project (All / each project / No project); checkbox **Group by project** (`by=project`, renders existing `ByProject`). Each change rewrites the query string with `useSearchParams` and refetches with the matching `TaskFilters`. The waiting count N comes from `listTasks(slug, { waiting: 'me' })` and is also what the nav badge shows.

Nav items become:

```ts
const items: NavItem[] = [
  { to: 'projects', label: 'Projects' },
  { to: 'tasks', label: 'Tasks', count: waiting },
  { to: 'turns', label: 'Turns' },
  { to: 'schedules', label: 'Schedules' },
  { to: 'huddles', label: 'Huddles' },
  { to: 'skills', label: 'Skills' },
  { to: 'settings', label: 'Settings' },
]
```

Router children (replace the work/inbox/items/projects/tasks/work-products/syncs entries):

```tsx
{ index: true, element: <Navigate to="projects" replace /> },
{ path: 'projects', element: <LazySection><AgentProjectsSection /></LazySection> },
{ path: 'projects/:ref', element: <LazySection><AgentProjectPage /></LazySection> },
{ path: 'tasks', element: <LazySection><AgentTasksSection /></LazySection> },
// Old addresses for the same things — bookmarks and links in old emails.
{ path: 'work', element: <Navigate to="../tasks" replace /> },
{ path: 'inbox', element: <Navigate to="../tasks?waiting=me" replace /> },
{ path: 'needs-you', element: <Navigate to="../tasks?waiting=me" replace /> },
{ path: 'items', element: <Navigate to="../tasks" replace /> },
{ path: 'overview', element: <Navigate to="../projects" replace /> },
{ path: 'work-products', element: <Navigate to="../projects" replace /> },
{ path: 'syncs', element: <Navigate to="../turns#status-reports" replace /> },
```

- [ ] **Step 1: Write the failing test** — render `AgentTasksSection` at `/w/connect/agents/eva/tasks?waiting=me` with `listTasks` mocked; assert it was called with `{ waiting: 'me' }`; click **Done** and assert a call with `{ status: 'done,declined' }`; assert the redirect `/w/connect/agents/eva/inbox` lands on `tasks?waiting=me` (router test using `createMemoryRouter` with the real route objects if `router.tsx` exports them; otherwise assert the `Navigate` target in a focused test of the route array).
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run — PASS.** `npx vitest run src/pages/agents`
- [ ] **Step 5: Commit** — `git commit -am "feat(frontend): Tasks page with waiting/open/done filters; Inbox and Work retired"`

---

### Task 8: Projects page and project page

**Files:**
- Create: `frontend/src/pages/agents/AgentProjectsSection.tsx`, `frontend/src/pages/agents/AgentProjectPage.tsx`
- Modify: `frontend/src/pages/agents/projectParts.tsx` (`NewProject` takes name + outcome; `StatusChip` exported; `ProjectGroupHeader` unchanged)
- Test: `AgentProjectsSection.test.tsx`, `AgentProjectPage.test.tsx`

**Interfaces:**
- Consumes: `listProjects`, `getProject`, `createProject`, `patchProject`, `TaskCard`.
- Produces: `AgentProjectsSection()`, `AgentProjectPage()` (reads `:ref`).

Projects page: `WorkbenchSubHeader title="Projects" count`; `NewProject` (name input, outcome input, both required, **Add project**); list of active projects as rows, each a `Link` to `projects/<ext_id>`:

```tsx
<Link to={p.ext_id} className="block rounded-lg border border-border p-3 hover:bg-muted">
  <div className="flex flex-wrap items-center gap-2">
    <span className="text-[11px] text-muted-foreground">{p.ext_id}</span>
    <span className="text-sm font-medium text-foreground">{p.name}</span>
    <StatusChip status={p.status} />
    <span className="ml-auto text-[11px] text-muted-foreground">
      {p.owner_email ?? p.owner_note} · {p.open_task_count} open · {relativeTime(p.updated_at)}
    </span>
  </div>
  {p.outcome && <p className="mt-1 line-clamp-1 text-[13px] text-foreground-secondary">{p.outcome}</p>}
</Link>
```

(Use the codebase's existing relative-time helper — grep `relativeTime\|timeAgo` in `frontend/src/lib`.) Done + archived in a `<details>` "Done and archived (N)". Empty state: "No projects yet. A project is a piece of work with an end — add one above."

Project page: back link "← Projects"; **Header** (ext_id, name, outcome, a `<select>` status → `patchProject`, owner, Drive folder link, repo); **Tasks** (`TaskCard` per `detail.tasks`, live first; empty → "No tasks in this project yet."); **Activity** (`detail.recent_turns`, each a link to `../../turns#<id>` showing status, preview, relative time; empty → "No turns have touched this project yet."); **Links** (`<details>` collapsed, `label` → `url`). Use the `Section` helper from `pages/agents/sectionLayout.tsx` for the four sections so they share headings.

- [ ] **Step 1: Write the failing tests** — Projects page lists active rows and hides done ones inside the collapsed group; Add project is disabled until both fields are filled and calls `createProject('eva', {name, outcome})`. Project page renders the four section headings in order (`getAllByRole('heading')` text equals `['Tasks','Activity','Links']` after the header) and changing status calls `patchProject('eva','P1',{status:'done'})`.
- [ ] **Step 2: Run — FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(frontend): first-class Projects page and project page"`

---

### Task 9: Status reports to Turns; work products gone from the UI

**Files:**
- Modify: `frontend/src/pages/agents/AgentTurnsSection.tsx` (add `<Section id="status-reports" title="Status reports">` using `listAgentSyncs` + `SyncCard`, moved from the work-products page)
- Delete: `frontend/src/pages/agents/AgentWorkProductsSection.tsx` (+test); `WorkProductCard` from `components/agents/cards.tsx`
- Modify: `frontend/src/pages/AgentsPage.tsx` L59/78/88 and `pages/agents/AgentSettingsSection.tsx` L65 (+test) — drop the "Work" `CountStat` and `work_product_count`
- Test: extend `AgentTurnsSection` test (create if absent): status reports render; `#status-reports` anchor exists

- [ ] **Step 1: Failing test** — render Turns with one mocked sync; expect "Status reports" heading and the sync title.
- [ ] **Step 2: Run — FAIL.** **Step 3: Implement.** **Step 4: Run — PASS.**
- [ ] **Step 5: Commit** — `git commit -am "feat(frontend): status reports on Turns; work products removed"`

---

### Task 10: Fleet "Waiting on you", guide, e2e, scripts, docs

**Files:**
- Replace: `frontend/src/components/supervisor/ItemInbox.tsx` → `WaitingOnYou.tsx` (uses `listFleetTasks({ waiting: 'me' })`, renders `TaskCard showAgent`), update `pages/SupervisorPage.tsx`
- Modify: `frontend/src/guide/surfaces.ts` (L238 agent card text; L328 work-products surface → projects + project surfaces; Work → Tasks; Inbox removed), `guide/coverage.ts` L64, `guide/grouping.test.ts` L32 count, `guide/paths.ts` L86 copy ("publish work products" → "record project links")
- Modify: `frontend/e2e/seed.py`, `frontend/e2e/agents.spec.ts`, `frontend/e2e/supervisor.spec.ts`; delete `frontend/e2e/items.spec.ts` and add the same journeys to a new `frontend/e2e/tasks.spec.ts` (approve from Waiting on you; answer a question)
- Modify: `scripts/e2e_embed_widget.py`, `docs/architecture/api-surface.md`, `docs/architecture/access.md`, `ARCHITECTURE.md`/`README.md`/`CLAUDE.md` mentions of items/work products (grep)

- [ ] **Step 1:** Port the supervisor test (`SupervisorPage` or `ItemInbox` tests if present) to `WaitingOnYou` — one fleet task renders with its agent tag and Approve button. Run — FAIL. Implement. Run — PASS.
- [ ] **Step 2:** `cd frontend && npx tsc --noEmit && npx vitest run && npm run build` — all clean. `rg -n -i "\bitems?\b.*(decide|dismiss)|ItemCard|ItemInbox|work[-_ ]products?|listAgentCommands|postTaskCommand" frontend/src docs scripts` returns nothing relevant.
- [ ] **Step 3:** Run the e2e suite the repo's way (see `frontend/e2e/README` or `package.json` scripts; pre-existing local Playwright errors are known — compare against `main`).
- [ ] **Step 4: Commit** — `git commit -am "feat: fleet Waiting on you; guide, e2e and docs in the new vocabulary"`

---

### Task 11: Ship canopy-web

- [ ] **Step 1:** Grep every agent repo once more for callers (they break at deploy — this list feeds Part 2):

```bash
for r in ada eva hal echo ace; do (cd ~/emdash/repositories/$r 2>/dev/null && git fetch -q && echo "== $r" && git grep -n -E "/items|items/|/commands|work-products|tasks/sync|tasks/waiting|agent-runs/projects|decide_item|dismiss_item|create_items|list_items" origin/main); done
gh api repos/dimagi-internal/ace/contents >/dev/null 2>&1 && echo "check ace via a fresh clone if not local"
```

- [ ] **Step 2:** `uv run pytest -q && (cd frontend && npm run build)` green. Push, `gh pr create` (body: what changed, the deleted routes list, the caller list from Step 1, "no back-compat by decision 2026-10-07"; end with the 🤖 line), `gh pr merge <n> --auto`, confirm with `gh pr view <n> --json autoMergeRequest`.
- [ ] **Step 3:** After merge, `gh workflow run "Deploy to Labs (AWS)" --ref main`; watch it; then verify live: `GET https://canopy.dimagi.com/api/agents/eva/tasks/?waiting=me` (via the canopy-web MCP `list_tasks` tool) and open `/w/dimagi/agents/eva/projects` and a project page.

---

## Part 2 — callers (one PR each, immediately after the deploy)

### Task 12: canopy plugin

**Files (canopy repo; find with `rg -n` from its root):** `runtime/src/orchestrator/agent_web.py` (`push_items` → `push_tasks` posting a list to `/api/agents/<slug>/tasks/`; delete `push_work`), `agent_cli.py` (`agent-publish` items/work subcommands → `tasks`; `canopy agent tasks` drain reads `GET /actions/?status=pending` and marks via `POST /actions/{id}/applied`; any `tasks/sync` call → `POST /tasks/` with `idempotency_key=f"{slug}:{ext_id}"`), `agent_health.py` L162 (`/items/?state=open` → `/tasks/?ask=open`), any `/api/agent-runs/projects/` → `/api/projects/`, `agent-core/turn.md` + `agent-core/task-tracker.md` + `agent-core/deliverables.md` (vocabulary: tasks, asks, actions; deliverables → `PATCH` project `links`), MCP tool names in docs (`decide_item` → `act_on_task`, etc.).

- [ ] Write/adjust the CLI tests for `push_tasks` and the drain (body shape: bare list; drain marks each applied). Run the canopy test suite. Bump the plugin version per repo rule (memory: version bump per PR; no auto-merge in canopy — merge after CI). Ship.

### Task 13: ada

**Files:** `bin/ada-publish` (L455, L705: `/items/` → `/tasks/?batch=` / `?ask=`), `bin/ada-withdraw` (L62–122: `--item-id` → `--task` ext_id; dismiss → `POST /tasks/{ref}/actions {"action":"decline"}`), `bin/_canopy_http.py` (docstring), `config/gating.json` L28 (the rail pattern targets `/api/items/.../dismiss` — retarget to `/api/agents/[^/]+/tasks/[^/]+/actions` with a decline body, naming `bin/ada-withdraw` as the sanctioned path), `skills/conduct/SKILL.md` L29/L39, `skills/fleet-review/SKILL.md` L129/L399 (idempotency key `item-<id>-<i>` → `task-<pk>-<i>`, settled = `?ask=closed`), `skills/fleet-sync/templates/round1.md` L17.

- [ ] Update `tests/` that cover publish/withdraw and the gating rail (`hooks/gating_guard.py` tests). `uv run pytest`. Branch → PR → merge (ada ships without review per CLAUDE.md).

### Task 14: eva and anything Task 11 Step 1 found

- [ ] Fix eva's 2 references (from `git grep` in Task 11), plus any hits in hal/echo/ace. One PR per repo, each repo's own shipping skill.

### Task 15: Fleet check

- [ ] `canopy agent doctor --all` (remember the PAT-leak false positive: verify each agent from its own repo). Run one Ada turn and confirm her board publish and withdraw work against the new routes. Close by updating Ada's memory: the items/commands/work-products vocabulary is retired (replace `fleet-review-publish-mechanics.md`'s `/api/items/{id}/dismiss` line).
