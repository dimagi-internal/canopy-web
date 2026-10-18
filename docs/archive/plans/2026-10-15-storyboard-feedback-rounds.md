# Storyboard feedback rounds — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let several people comment on a storyboard over a round, then hand the whole batch to the agent with one close action that starts exactly one turn; the agent answers every comment, publishes the next version, and the next round opens by itself.

**Architecture:** A generic `FeedbackRound` lives in the framework app `apps/feedback` (beside `Feedback`, which gains a nullable `round` FK), because a framework model may not point at a product model. `apps/storyboards/rounds.py` (product) owns everything that makes it a storyboard round: version pinning, note adoption, visibility, staging decision edits, the close (applies staged edits, creates one `AgentTask` on the board's project and approves it through `agents.services.act`, which enqueues one turn), and the consume (refuses unanswered notes, opens the next round). Review-page suggestions are also written into the pool. On the plugin side, canopy gains `scripts/ddd/feedback_round.py` + a `ddd-feedback-round` skill step and `scripts/ddd/demo_set.py` (publish a board with `decisions[]`); ACE's `demo-narrative` and `narrative-iteration-review` skills move onto them.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2, PostgreSQL, pytest (`uv run pytest`); React 19 + vitest; canopy plugin Python (`uv run pytest tests/ddd`); ACE TypeScript tests (`npm test`).

**Spec:** `docs/superpowers/specs/2026-10-15-storyboard-decisions-and-feedback-rounds-design.md` (section C, "Owner decisions", Rollout). **Depends on:** `docs/superpowers/plans/2026-10-15-storyboard-decision-rows.md` (plan B) being merged — this plan uses `StoryboardDecision`, `apps/storyboards/decisions.py` and the `edit_storyboard_decision` route.

## Global Constraints

- Round states: `open` → `closed` → `consumed`. One open round per board (DB constraint).
- Only the board's owner/editors (`perms.CONTENT_WRITE` in its workspace) may open, close or consume a round. Commenters and guests cannot.
- Opening a round requires `Storyboard.agent_project`; refuse with 422 otherwise.
- Close is ONE transaction: round → closed; staged edits applied (closer picks conflicts); one `AgentTask` created on the linked project; one `act(approve)` → exactly one enqueued turn.
- Consume refuses (409) while any note in the round is still `new`. Consume opens round N+1 in the same transaction, pinned to each narrative's current version (owner decision 2026-10-15).
- Inside an open round, every decision edit is **staged**, never applied, for members and link holders alike; a link holder needs the `suggest` grant and a typed name. Outside a round a member's edit applies immediately (plan B); a link holder's is refused with 409.
- In an open round, everyone who can read the board sees every round note. Author names are shown; emails are never shown to non-members. Outside a round, nothing changes: notes stay members-only (`list_notes`).
- A guest's note inside a round needs a non-empty `author_name` (422). A signed-in member's name comes from their account, never the payload.
- `apps/feedback` is FRAMEWORK tier: it must not import `apps.storyboards`, `apps.reviews` or any product app (`tests/test_architecture_boundary.py`). It may import `apps.agents` (framework) — but it does not need to.
- Every new route is registered in `apps/api/route_gates.py` (`GATES`, `NOT_SETTINGS`, and `VIEWER_MAY_MUTATE` where a member/token-tier write) — `tests/test_every_route_declares_its_gate.py`.
- Changing `schemas.py`/`api.py` ⇒ `cd frontend && npm run gen:api:local` and commit `generated.ts`.
- Frontend: semantic tokens only; no palette literals, no `dark:`.
- canopy plugin: `canopy version bump` for any `plugins/canopy/` change (`check-version` is required). ACE: `bash scripts/version-bump.sh`.

## Review Focus

1. **Twenty people comment while the closer clicks Close twice (two tabs).** Expect exactly one task and one turn; the second click gets 409 "this round is closed". Test: `test_double_close_creates_one_task_and_one_turn` (Task 5).
2. **A note arrives in the same second the round closes.** Expect it lands in no round (round is locked/closed) and is adopted by round N+1 when consume opens it — never silently attached to a closed round. Test: `test_note_after_close_waits_for_next_round` (Task 2 + Task 6).
3. **The agent consumes before answering every note.** Expect 409 listing the unanswered note ids; round stays `closed`; no new round. Test: `test_consume_refuses_unanswered_notes` (Task 6).
4. **The board has no project, or its project's agent was deleted.** Expect open → 422 with a sentence saying to link a project; never a 500 at close. Test: `test_open_requires_a_linked_project` (Task 2).
5. **A guest stages two different picks for one row and a member stages a third; the closer picks none.** Expect close → 409 naming the row; nothing applied, round still open. Test: `test_close_with_unpicked_conflict_is_409_and_changes_nothing` (Task 5).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/feedback/models.py` (modify) | `FeedbackRound`; `Feedback.round` FK. |
| `apps/feedback/migrations/0004_feedbackround.py` (generated) | Table + FK + constraints. |
| `apps/feedback/rounds.py` (create) | Framework round lifecycle: open, current, stage, lock, close, consume, unanswered. Request-free; no product imports. |
| `apps/feedback/services.py` (modify) | `ingest(..., round=None)`. |
| `apps/agents/models.py` (modify) | `ProjectParticipant.REVIEW` source + constraint. |
| `apps/agents/migrations/00NN_participant_source_review.py` (generated) | Constraint update. |
| `apps/storyboards/rounds.py` (create) | Storyboard round rules: pin, open, adopt, note validation, visibility payloads, staging, close→task→turn, consume→next round, review-suggestion ingest, participant link. |
| `apps/storyboards/schemas.py` (modify) | `RoundOut`, `RoundNoteOut`, `StagedEditOut`, `RoundCloseIn`, `RoundConsumeIn`, `RoundDetailOut`; `StoryboardOut.round/previous_round`; `DecisionOut.pending`. |
| `apps/storyboards/api.py` (modify) | Routes: open, read (agent), close, consume; `leave_feedback` + `edit_storyboard_decision` round-aware. |
| `apps/storyboards/services.py` (modify) | `resolve_board(..., viewer=...)` emits round blocks. |
| `apps/reviews/api.py` (modify) | `suggest_review` also writes to the pool. |
| `apps/api/route_gates.py` (modify) | Gates for the four new routes. |
| `frontend/src/api/storyboards.ts` (modify) | Round types + `openRound`, `closeRound`. |
| `frontend/src/components/storyboard/RoundBanner.tsx` (create) | Round state, counts, open/close (editors), close dialog with conflict picks. |
| `frontend/src/components/storyboard/MyNotes.tsx` (create) | "What happened to my notes from round N". |
| `frontend/src/components/storyboard/DecisionLog.tsx` (modify) | Pending chips, guest staging, per-row comment composer. |
| `frontend/src/pages/StoryboardPage.tsx` (modify) | Wire banner, round notes for everyone, my-notes. |
| `tests/test_feedback_rounds.py`, `tests/test_storyboard_rounds.py` (create) | Backend tests. |
| canopy: `scripts/ddd/feedback_round.py`, `scripts/ddd/demo_set.py`, `tests/ddd/test_feedback_round.py`, `tests/ddd/test_demo_set.py`, `plugins/canopy/skills/ddd-feedback-round/SKILL.md`, `plugins/canopy/agents/ddd.md` | Plugin step + board publisher. |
| ACE: `skills/demo-narrative/SKILL.md`, `skills/narrative-iteration-review/SKILL.md`, `test/skills/demo-set-decisions.test.ts` | Publisher + reader move to decisions and rounds. |

---

### Task 1: `FeedbackRound` and the framework lifecycle

**Files:**
- Modify: `apps/feedback/models.py`, `apps/feedback/services.py:ingest`
- Create: `apps/feedback/rounds.py`
- Generate: `apps/feedback/migrations/0004_feedbackround.py`
- Test: `tests/test_feedback_rounds.py`

**Interfaces:**
- Produces:
  - `FeedbackRound` with `OPEN="open"`, `CLOSED="closed"`, `CONSUMED="consumed"`; fields `target_kind`, `target_ref`, `workspace`, `number`, `state`, `pinned_versions: dict`, `opened_by`, `opened_at`, `closed_by`, `closed_at`, `close_note`, `staged_edits: list`, `task` (FK `agents.AgentTask`, null), `consumed_by_run: str`, `consumed_at`.
  - `Feedback.round` (FK, null, `related_name="notes"`).
  - `rounds.RoundError(status: int, detail: str)`.
  - `rounds.current(*, workspace_id, target_kind, target_ref) -> FeedbackRound | None`
  - `rounds.by_number(*, workspace_id, target_kind, target_ref, number) -> FeedbackRound | None`
  - `rounds.latest_finished(*, workspace_id, target_kind, target_ref) -> FeedbackRound | None`
  - `rounds.open_round(*, workspace, target_kind, target_ref, pinned_versions, by_user=None, adopt=None) -> FeedbackRound`
  - `rounds.stage_edit(rnd, edit: dict) -> dict` (adds `edit_id`, `at`)
  - `rounds.lock_open(rnd) -> FeedbackRound` (call inside `transaction.atomic`)
  - `rounds.mark_closed(rnd, *, by_user, note) -> None` (caller holds the lock)
  - `rounds.unanswered(rnd) -> list[int]`
  - `rounds.mark_consumed(rnd, *, run_id) -> FeedbackRound`
  - `services.ingest(items, *, workspace, submitted_by=None, round=None)`

- [ ] **Step 1: Write the failing tests** — `tests/test_feedback_rounds.py`:

```python
"""The framework half of a feedback round: generic over its target, like Feedback."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from apps.feedback import rounds
from apps.feedback import services as fsvc
from apps.feedback.models import Feedback, FeedbackRound
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db
T = {"target_kind": "storyboard", "target_ref": "supply"}


@pytest.fixture()
def ws():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    return Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)


def _note(ws, rnd=None, **over):
    item = {"target_kind": "narrative", "target_ref": "n1", "body": "hm", "author_name": "Lily", **over}
    (pk,) = fsvc.ingest([item], workspace=ws, round=rnd)["ids"]
    return Feedback.objects.get(pk=pk)


def test_open_numbers_rounds_and_adopts_roundless_notes(ws):
    loose = _note(ws)
    r1 = rounds.open_round(workspace=ws, **T, pinned_versions={"n1": 3},
                           adopt=Feedback.objects.filter(pk=loose.pk))
    assert r1.number == 1 and r1.state == "open" and r1.pinned_versions == {"n1": 3}
    loose.refresh_from_db()
    assert loose.round == r1


def test_only_one_open_round_per_target(ws):
    rounds.open_round(workspace=ws, **T, pinned_versions={})
    with pytest.raises(rounds.RoundError) as e:
        rounds.open_round(workspace=ws, **T, pinned_versions={})
    assert e.value.status == 409


def test_db_forbids_two_open_rounds(ws):
    FeedbackRound.objects.create(workspace=ws, **T, number=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        FeedbackRound.objects.create(workspace=ws, **T, number=2)


def test_ingest_attaches_round(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    assert _note(ws, r).round == r


def test_stage_edit_appends_with_id(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    e = rounds.stage_edit(r, {"key": "banner", "action": "confirm", "by_name": "Lily"})
    r.refresh_from_db()
    assert r.staged_edits == [e] and e["edit_id"] and e["at"]


def test_stage_edit_on_closed_round_is_409(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    with transaction.atomic():
        rounds.mark_closed(rounds.lock_open(r), by_user=None, note="")
    with pytest.raises(rounds.RoundError) as e:
        rounds.stage_edit(r, {"key": "banner", "action": "confirm"})
    assert e.value.status == 409


def test_consume_refuses_new_notes_then_succeeds(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    n = _note(ws, r)
    with transaction.atomic():
        rounds.mark_closed(rounds.lock_open(r), by_user=None, note="")
    with pytest.raises(rounds.RoundError) as e:
        rounds.mark_consumed(r, run_id="supply-2026-10-20-001")
    assert e.value.status == 409 and str(n.pk) in e.value.detail
    fsvc.resolve(n.pk, workspace_slugs=[ws.pk], state="answered", note="done", resolved_in_version=4)
    r = rounds.mark_consumed(r, run_id="supply-2026-10-20-001")
    assert r.state == "consumed" and r.consumed_by_run == "supply-2026-10-20-001"


def test_consume_requires_closed(ws):
    r = rounds.open_round(workspace=ws, **T, pinned_versions={})
    with pytest.raises(rounds.RoundError) as e:
        rounds.mark_consumed(r, run_id="x")
    assert e.value.status == 409
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_feedback_rounds.py -v`
Expected: FAIL — `ImportError: cannot import name 'rounds'` / `FeedbackRound`.

- [ ] **Step 3: Model** — append to `apps/feedback/models.py` **above** `class Feedback` is not possible because `Feedback` references it; put `FeedbackRound` after `ArtifactView` and give `Feedback` the FK by string:

In `class Feedback`, after `submitted_by`:

```python
    round = models.ForeignKey(
        "feedback.FeedbackRound", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="notes",
    )
    """The feedback round this note was left in, when one was open. Null = left
    between rounds; the next round adopts it when it opens."""
```

Then append:

```python
class FeedbackRound(models.Model):
    """A window in which several people comment, closed by one deliberate act
    that hands the whole batch to an agent (spec C).

    Framework tier and generic over its target by string, exactly like
    Feedback — a round on a storyboard today, on something else later. The
    product app that owns the target owns the routes and the rules.
    """

    OPEN, CLOSED, CONSUMED = "open", "closed", "consumed"
    STATE_CHOICES = [(OPEN, "Open"), (CLOSED, "Closed"), (CONSUMED, "Consumed")]

    target_kind = models.CharField(max_length=32)
    target_ref = models.CharField(max_length=200)
    workspace = models.ForeignKey("workspaces.Workspace", on_delete=models.CASCADE,
                                  related_name="feedback_rounds")
    number = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=STATE_CHOICES, default=OPEN)
    pinned_versions = models.JSONField(default=dict, blank=True)
    """{ref: version} frozen at open, so every note is about the same cut."""

    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    closed_at = models.DateTimeField(null=True, blank=True)
    close_note = models.TextField(blank=True, default="")
    staged_edits = models.JSONField(default=list, blank=True)
    """Edits proposed during the round, applied by the owning app at close."""
    task = models.ForeignKey("agents.AgentTask", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+")
    consumed_by_run = models.CharField(max_length=200, blank=True, default="")
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-number"]
        constraints = [
            models.UniqueConstraint(fields=["workspace", "target_kind", "target_ref", "number"],
                                    name="uniq_round_number_per_target"),
            models.UniqueConstraint(fields=["workspace", "target_kind", "target_ref"],
                                    condition=models.Q(state="open"),
                                    name="one_open_round_per_target"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"round:{self.target_kind}:{self.target_ref}:{self.number}:{self.state}"
```

- [ ] **Step 4: Lifecycle module** — `apps/feedback/rounds.py`:

```python
"""Feedback-round lifecycle (framework). Request-free and target-agnostic: the
product app that owns the target decides who may open/close and what closing
does; this module only keeps the state machine honest."""
from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.db.models import Max
from django.utils import timezone

from apps.feedback.models import Feedback, FeedbackRound


class RoundError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _of(workspace_id, target_kind, target_ref):
    return FeedbackRound.objects.filter(workspace_id=workspace_id, target_kind=target_kind,
                                        target_ref=target_ref)


def current(*, workspace_id, target_kind, target_ref):
    return _of(workspace_id, target_kind, target_ref).filter(state=FeedbackRound.OPEN).first()


def by_number(*, workspace_id, target_kind, target_ref, number):
    return _of(workspace_id, target_kind, target_ref).filter(number=number).first()


def latest_finished(*, workspace_id, target_kind, target_ref):
    return (_of(workspace_id, target_kind, target_ref)
            .exclude(state=FeedbackRound.OPEN).order_by("-number").first())


@transaction.atomic
def open_round(*, workspace, target_kind, target_ref, pinned_versions, by_user=None, adopt=None):
    existing = current(workspace_id=workspace.pk, target_kind=target_kind, target_ref=target_ref)
    if existing is not None:
        raise RoundError(409, f"round {existing.number} is already open")
    last = _of(workspace.pk, target_kind, target_ref).aggregate(m=Max("number"))["m"] or 0
    try:
        with transaction.atomic():
            rnd = FeedbackRound.objects.create(
                workspace=workspace, target_kind=target_kind, target_ref=target_ref,
                number=last + 1, pinned_versions=dict(pinned_versions or {}),
                opened_by=by_user if getattr(by_user, "is_authenticated", False) else None,
            )
    except IntegrityError:
        raise RoundError(409, "a round is already open") from None
    if adopt is not None:
        Feedback.objects.filter(pk__in=adopt.filter(round__isnull=True).values("pk")).update(round=rnd)
    return rnd


def lock_open(rnd) -> FeedbackRound:
    """Re-read under a row lock; refuse unless still open. Call inside atomic()."""
    rnd = FeedbackRound.objects.select_for_update().get(pk=rnd.pk)
    if rnd.state != FeedbackRound.OPEN:
        raise RoundError(409, f"round {rnd.number} is {rnd.state}")
    return rnd


def stage_edit(rnd, edit: dict) -> dict:
    with transaction.atomic():
        rnd = lock_open(rnd)
        entry = {**edit, "edit_id": uuid.uuid4().hex[:12], "at": timezone.now().isoformat()}
        rnd.staged_edits = [*rnd.staged_edits, entry]
        rnd.save(update_fields=["staged_edits"])
    return entry


def mark_closed(rnd, *, by_user, note: str) -> None:
    rnd.state = FeedbackRound.CLOSED
    rnd.closed_by = by_user if getattr(by_user, "is_authenticated", False) else None
    rnd.closed_at = timezone.now()
    rnd.close_note = (note or "").strip()
    rnd.save(update_fields=["state", "closed_by", "closed_at", "close_note"])


def unanswered(rnd) -> list[int]:
    return sorted(rnd.notes.filter(state=Feedback.STATE_NEW).values_list("pk", flat=True))


@transaction.atomic
def mark_consumed(rnd, *, run_id: str) -> FeedbackRound:
    rnd = FeedbackRound.objects.select_for_update().get(pk=rnd.pk)
    if rnd.state != FeedbackRound.CLOSED:
        raise RoundError(409, f"round {rnd.number} is {rnd.state}; only a closed round is consumed")
    left = unanswered(rnd)
    if left:
        raise RoundError(409, "every note needs an answer before the round is consumed; "
                              f"still new: {', '.join(map(str, left))}")
    rnd.state = FeedbackRound.CONSUMED
    rnd.consumed_by_run = (run_id or "").strip()
    rnd.consumed_at = timezone.now()
    rnd.save(update_fields=["state", "consumed_by_run", "consumed_at"])
    return rnd
```

In `apps/feedback/services.py:ingest`, change the signature to `def ingest(items: list[dict], *, workspace, submitted_by=None, round=None) -> dict:` and the create to `Feedback.objects.create(**data, workspace=workspace, submitted_by=submitted_by, round=round)`; add to the docstring: "``round`` attaches every created note to that feedback round."

- [ ] **Step 5: Migration**

Run: `uv run python manage.py makemigrations feedback -n feedbackround`
Expected: `apps/feedback/migrations/0004_feedbackround.py` with `CreateModel(FeedbackRound)`, two constraints, `AddField(feedback.round)`, and a dependency on the latest `agents` migration.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_feedback_rounds.py tests/test_architecture_boundary.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/feedback tests/test_feedback_rounds.py
git commit -m "feat(feedback): FeedbackRound — a window of notes closed by one act"
```

---

### Task 2: Opening a storyboard round, and notes that land in it

**Files:**
- Create: `apps/storyboards/rounds.py`
- Modify: `apps/storyboards/api.py` (`leave_feedback`, new `open_storyboard_round`), `apps/storyboards/schemas.py`, `apps/agents/models.py` (`ProjectParticipant.REVIEW`), `apps/api/route_gates.py`
- Generate: `apps/agents/migrations/<next>_participant_source_review.py`
- Test: `tests/test_storyboard_rounds.py`

**Interfaces:**
- Consumes: `apps.feedback.rounds.*` (Task 1), `services.board_feedback(board)`.
- Produces (`apps/storyboards/rounds.py`):
  - `TARGET = "storyboard"`, `summary(rnd) -> dict` (the `RoundOut` fields)
  - `current(board) -> FeedbackRound | None`, `by_number(board, n) -> FeedbackRound | None`, `latest_finished(board)`
  - `pinned_versions(board) -> dict[str, int]`
  - `open_round(board, *, by_user) -> FeedbackRound` (422 without `agent_project`)
  - `check_anchor(board, anchor_id: str) -> None` (raises `RoundError(422|404)`)
  - `author_for(request_user, payload_name: str, *, member: bool, in_round: bool) -> str`
  - `link_participant(board, user) -> None` (best-effort)
- Route: `POST /api/storyboards/{slug}/rounds` → `RoundOut` (`open_storyboard_round`), editors only.
- Schema: `RoundOut {number, state, opened_at, opened_by, closed_at, close_note, pinned_versions, note_count, people_count}`.

- [ ] **Step 1: Write the failing tests** — `tests/test_storyboard_rounds.py`:

```python
"""Feedback rounds on a storyboard (spec C)."""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, ProjectParticipant
from apps.feedback.models import Feedback, FeedbackRound
from apps.storyboards import rounds as board_rounds
from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")
    ace_user = User.objects.create_user("ace", "ace@dimagi-ai.com", "pw")
    viewer = User.objects.create_user("lily", "lily@dimagi.com", "pw", first_name="Lily")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=ace_user, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj, user=ace_user)
    project = AgentProject.objects.create(agent=agent, ext_id="P9", name="Connect Supply demos")
    board = Storyboard.objects.create(slug="supply", title="Connect Supply", workspace=ws,
                                      capability=Storyboard.CAP_SUGGEST, agent_project=project)
    act = Act.objects.create(storyboard=board, title="RUTF", position=0)
    Entry.objects.create(act=act, narrative_slug="supply-rutf", position=0)
    token = board.ensure_share_token()
    clients = {}
    for name, u in (("owner", jj), ("ace", ace_user), ("viewer", viewer)):
        c = Client()
        c.force_login(u)
        clients[name] = c
    return {"ws": ws, "board": board, "token": token, "project": project, "agent": agent,
            "users": {"owner": jj, "ace": ace_user, "viewer": viewer}, **clients}


def _open(world):
    resp = world["owner"].post("/api/storyboards/supply/rounds")
    assert resp.status_code == 200, resp.content
    return resp.json()


def _note(world, client=None, **body):
    c = client or Client()
    payload = {"narrative_slug": "supply-rutf", "anchor_id": "cut:procurements", "body": "Too fast",
               **body}
    return c.post(f"/api/storyboards/supply/feedback?t={world['token']}", payload,
                  content_type="application/json")


def test_open_requires_a_linked_project(world):
    world["board"].agent_project = None
    world["board"].save()
    resp = world["owner"].post("/api/storyboards/supply/rounds")
    assert resp.status_code == 422
    assert "project" in resp.json()["detail"]


def test_only_editors_open(world):
    assert world["viewer"].post("/api/storyboards/supply/rounds").status_code == 403


def test_open_pins_versions_and_adopts_loose_notes(world, monkeypatch):
    monkeypatch.setattr(board_rounds, "pinned_versions", lambda b: {"supply-rutf": 3})
    _note(world, author_name="Early bird")
    out = _open(world)
    assert out["number"] == 1 and out["pinned_versions"] == {"supply-rutf": 3}
    assert Feedback.objects.get(author_name="Early bird").round.number == 1


def test_guest_note_in_round_needs_a_name(world):
    _open(world)
    assert _note(world, author_name="").status_code == 422
    assert _note(world, author_name="Sophie").status_code == 200
    assert Feedback.objects.get(author_name="Sophie").round.number == 1


def test_member_note_uses_account_name_and_joins_the_project(world, monkeypatch):
    from apps.contacts import people

    sentinel = object()
    monkeypatch.setattr(people, "by_email", lambda e: sentinel if e == "lily@dimagi.com" else None)
    linked = []
    monkeypatch.setattr("apps.agents.participants.ensure",
                        lambda project, person, *, source, role="": linked.append((project, person, source)))
    _open(world)
    assert _note(world, client=world["viewer"], author_name="Not me").status_code == 200
    assert Feedback.objects.get(body="Too fast").author_name == "Lily"
    assert linked == [(world["project"], sentinel, ProjectParticipant.REVIEW)]


def test_note_anchor_must_be_known(world):
    _open(world)
    assert _note(world, author_name="S", anchor_id="decision:nope").status_code == 404
    assert _note(world, author_name="S", anchor_id="weird:thing").status_code == 422
    assert _note(world, author_name="S", anchor_id="product:procurements").status_code == 200
    assert _note(world, author_name="S", anchor_id="overview").status_code == 200  # a bare scene id


def test_note_after_close_waits_for_next_round(world):
    _open(world)
    rnd = FeedbackRound.objects.get()
    rnd.state = FeedbackRound.CLOSED
    rnd.save()
    assert _note(world, author_name="Late").status_code == 200
    assert Feedback.objects.get(author_name="Late").round is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storyboard_rounds.py -v`
Expected: FAIL — `ImportError: cannot import name 'rounds' from 'apps.storyboards'`.

- [ ] **Step 3: Participant source** — in `apps/agents/models.py` `ProjectParticipant`:

```python
    FACT, TURN, MANUAL, SUMMARY, REVIEW = "fact", "turn", "manual", "summary", "review"
    SOURCE_CHOICES = [
        (FACT, "A fact about them was filed against the project"),
        (TURN, "A conversation with them raised one of its tasks"),
        (MANUAL, "Added by a person"),
        (SUMMARY, "An agent wrote their contributor page from their own words"),
        (REVIEW, "They commented in a feedback round on one of its storyboards"),
    ]
```

and the check constraint becomes `condition=models.Q(source__in=["fact", "turn", "manual", "summary", "review"]), name="project_participant_source_known_v3"`. Run `uv run python manage.py makemigrations agents -n participant_source_review`. Grep for other copies of the source list (`grep -rn '"summary"' apps/agents apps/contacts frontend/src | grep -i source`) and add `review` where a list is enumerated (e.g. a frontend label map).

- [ ] **Step 4: Storyboard round module** — `apps/storyboards/rounds.py`:

```python
"""Storyboard feedback rounds — the product rules over apps.feedback.rounds.

Who may open/close/consume, what versions a round pins, which anchors a note
may carry, who sees round notes, and what closing does (one task, one turn)."""
from __future__ import annotations

import logging

from apps.feedback import rounds as frounds
from apps.feedback.models import Feedback
from apps.runs import aggregate
from apps.storyboards.models import Entry, Storyboard

logger = logging.getLogger(__name__)

TARGET = "storyboard"
_ANCHOR_PREFIXES = ("act:", "cut:", "decision:", "product:", "scene:")
RoundError = frounds.RoundError


def _where(board: Storyboard) -> dict:
    return {"workspace_id": board.workspace_id, "target_kind": TARGET, "target_ref": board.slug}


def current(board):
    return frounds.current(**_where(board))


def by_number(board, number: int):
    return frounds.by_number(**_where(board), number=number)


def latest_finished(board):
    return frounds.latest_finished(**_where(board))


def pinned_versions(board) -> dict[str, int]:
    """Each narrative's current version — what everyone in the round watches."""
    out: dict[str, int] = {}
    for slug in Entry.objects.filter(act__storyboard=board).values_list("narrative_slug", flat=True):
        narrative = aggregate.build_narrative(slug, {board.workspace_id}) or {}
        versions = narrative.get("versions") or []
        if versions and versions[0].get("version") is not None:
            out[slug] = int(versions[0]["version"])
    return out


def open_round(board, *, by_user):
    if board.agent_project_id is None:
        raise RoundError(422, "Link this storyboard to a board project before opening a round: "
                              "closing the round hands it to that project's agent.")
    from apps.storyboards import services

    return frounds.open_round(
        workspace=board.workspace, target_kind=TARGET, target_ref=board.slug,
        pinned_versions=pinned_versions(board), by_user=by_user,
        adopt=services.board_feedback(board).filter(state=Feedback.STATE_NEW),
    )


def check_anchor(board, anchor_id: str) -> None:
    a = (anchor_id or "").strip()
    if not a:
        return
    if a.startswith("decision:"):
        if not board.decisions.filter(key=a.split(":", 1)[1]).exists():
            raise RoundError(404, "no such decision on this storyboard")
        return
    if a.startswith(_ANCHOR_PREFIXES):
        return
    if ":" in a:
        raise RoundError(422, f"unknown anchor {a!r}")
    # A bare id is a scene id — the convention NarrativeReviewPage already uses.


def _display_name(user) -> str:
    return user.get_full_name() or user.first_name or user.email or user.username


def author_for(user, payload_name: str, *, member: bool, in_round: bool) -> str:
    if member and getattr(user, "is_authenticated", False):
        return _display_name(user)
    name = (payload_name or "").strip()
    if in_round and not name:
        raise RoundError(422, "Tell us your name so the others in this round know who said it.")
    return name


def link_participant(board, user) -> None:
    """A signed-in member who comments joins the board's project (best-effort)."""
    if board.agent_project_id is None or not getattr(user, "is_authenticated", False):
        return
    try:
        from apps.agents import participants
        from apps.agents.models import ProjectParticipant
        from apps.contacts import people

        person = people.by_email(user.email or "")
        if person is not None:
            participants.ensure(board.agent_project, person, source=ProjectParticipant.REVIEW)
    except Exception:  # noqa: BLE001
        logger.exception("rounds: could not link %s to %s's project", user.pk, board.slug)
```

- [ ] **Step 5: Routes** — in `apps/storyboards/api.py`:

Schema (`schemas.py`, near `NotesOut`):

```python
class RoundOut(StrictModel):
    number: int
    state: str
    opened_at: str
    opened_by: str
    closed_at: str | None
    close_note: str
    pinned_versions: dict[str, int]
    note_count: int
    people_count: int
```

Summary helper — in `apps/storyboards/rounds.py` (append; the routes and Task 3's payload both use it):

```python
def summary(rnd) -> dict:
    notes = rnd.notes.all()
    return {
        "number": rnd.number,
        "state": rnd.state,
        "opened_at": rnd.opened_at.isoformat(),
        "opened_by": (rnd.opened_by.get_full_name() or rnd.opened_by.email) if rnd.opened_by else "",
        "closed_at": rnd.closed_at.isoformat() if rnd.closed_at else None,
        "close_note": rnd.close_note,
        "pinned_versions": rnd.pinned_versions,
        "note_count": notes.count(),
        "people_count": notes.values("author_name").distinct().count(),
    }
```

Route (in `api.py`, after `leave_feedback`):

```python
@router.post("/{slug}/rounds", response=RoundOut, auth=session_auth,
             summary="Open a feedback round on a storyboard")
def open_storyboard_round(request: HttpRequest, slug: str) -> dict:
    """Editors only. Pins every narrative's current version and adopts the notes
    left since the last round. Needs the board linked to a project."""
    board = _editable_or_404(request, slug)
    try:
        rnd = board_rounds.open_round(board, by_user=request.user)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    return board_rounds.summary(rnd)
```

`leave_feedback` changes (keep everything else):

```python
    rnd = board_rounds.current(board)
    try:
        board_rounds.check_anchor(board, payload.anchor_id)
        author = board_rounds.author_for(request.user, payload.author_name,
                                         member=is_member, in_round=rnd is not None)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    ...
    item["author_name"] = author
    if is_member:
        item["author_email"] = request.user.email or ""
    result = feedback_services.ingest([item], workspace=board.workspace,
                                      submitted_by=request.user if request.user.is_authenticated else None,
                                      round=rnd)
    if is_member and rnd is not None:
        board_rounds.link_participant(board, request.user)
    return result
```

Import: `from apps.storyboards import rounds as board_rounds`, `RoundOut` from schemas.

Gates (`route_gates.py`): `GATES["open_storyboard_round"] = ("content.write",)`; `NOT_SETTINGS["open_storyboard_round"] = "content"`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_storyboard_rounds.py tests/test_storyboard_api.py tests/test_every_route_declares_its_gate.py tests/test_architecture_boundary.py -v`
Expected: PASS. If an existing `test_storyboard_api.py` test posts a note with an anchor such as `act:<key>` it still passes; if one used another `x:y` form, it now 422s — update the test to a real anchor.

- [ ] **Step 7: Commit**

```bash
git add apps/storyboards apps/agents apps/api/route_gates.py tests/test_storyboard_rounds.py
git commit -m "feat(storyboards): open a feedback round; notes land in it, named"
```

---

### Task 3: Everyone in the round sees the round's notes

**Files:**
- Modify: `apps/storyboards/rounds.py` (`round_payload`), `apps/storyboards/services.py:resolve_board`, `apps/storyboards/api.py:get_storyboard`, `apps/storyboards/schemas.py`
- Test: `tests/test_storyboard_rounds.py` (append)

**Interfaces:**
- Produces:
  - `rounds.note_out(fb, *, viewer) -> dict` — `{id, kind, body, suggested_text, author_name, anchor_id, target_kind, target_ref, target_version, state, disposition_note, resolved_in_version, created_at, mine}`; never an email.
  - `rounds.round_payload(board, rnd, *, viewer) -> dict` — `RoundOut` fields + `notes: [RoundNoteOut]` + `staged_edits: [StagedEditOut]`.
  - `StoryboardOut.round: RoundPayloadOut | None`, `StoryboardOut.previous_round: RoundPayloadOut | None`.
  - `services.resolve_board(board, *, is_member=False, viewer=None)`.

- [ ] **Step 1: Failing tests** (append)

```python
def test_token_holder_sees_everyones_round_notes_without_emails(world):
    _open(world)
    _note(world, author_name="Sophie", author_email="sophie@example.org")
    _note(world, client=world["viewer"], body="Name the suppliers")
    body = Client().get(f"/api/storyboards/supply?t={world['token']}").json()
    notes = body["round"]["notes"]
    assert {n["author_name"] for n in notes} == {"Sophie", "Lily"}
    assert all("author_email" not in n for n in notes)
    assert "sophie@example.org" not in str(body)


def test_outside_a_round_token_holders_see_no_notes(world):
    _note(world, author_name="Sophie")
    body = Client().get(f"/api/storyboards/supply?t={world['token']}").json()
    assert body["round"] is None and body["previous_round"] is None


def test_member_sees_mine_flag(world):
    _open(world)
    _note(world, client=world["viewer"], body="mine")
    _note(world, author_name="Sophie", body="theirs")
    notes = world["viewer"].get("/api/w/connect/storyboards/supply").json()["round"]["notes"]
    assert {n["body"]: n["mine"] for n in notes} == {"mine": True, "theirs": False}


def test_previous_round_carries_dispositions(world):
    from apps.feedback import services as fsvc

    _open(world)
    _note(world, author_name="Sophie")
    rnd = FeedbackRound.objects.get()
    fb = rnd.notes.get()
    fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="answered", note="Slowed cut 1",
                 resolved_in_version=4)
    rnd.state = FeedbackRound.CONSUMED
    rnd.save()
    prev = Client().get(f"/api/storyboards/supply?t={world['token']}").json()["previous_round"]
    (n,) = prev["notes"]
    assert n["state"] == "answered" and n["disposition_note"] == "Slowed cut 1"
    assert n["resolved_in_version"] == 4
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storyboard_rounds.py -k "sees or previous or outside" -v`
Expected: FAIL — `KeyError: 'round'`.

- [ ] **Step 3: Implement** — `apps/storyboards/rounds.py` (append):

```python
def note_out(fb, *, viewer) -> dict:
    return {
        "id": fb.pk, "kind": fb.kind, "body": fb.body, "suggested_text": fb.suggested_text,
        "author_name": fb.author_name or "Anonymous", "anchor_id": fb.anchor_id,
        "target_kind": fb.target_kind, "target_ref": fb.target_ref,
        "target_version": fb.target_version, "state": fb.state,
        "disposition_note": fb.disposition_note, "resolved_in_version": fb.resolved_in_version,
        "created_at": fb.created_at.isoformat(),
        "mine": bool(getattr(viewer, "is_authenticated", False) and fb.submitted_by_id == viewer.pk),
    }


def round_payload(board, rnd, *, viewer) -> dict | None:
    if rnd is None:
        return None
    notes = rnd.notes.exclude(body="", suggested_text="").order_by("created_at")
    return {
        **summary(rnd),
        "notes": [note_out(fb, viewer=viewer) for fb in notes],
        "staged_edits": [
            {k: e.get(k, "") for k in ("edit_id", "key", "action", "value", "reasoning", "by_name", "at")}
            for e in rnd.staged_edits
        ],
    }
```

Schemas: add

```python
class RoundNoteOut(StrictModel):
    id: int
    kind: str
    body: str
    suggested_text: str
    author_name: str
    anchor_id: str
    target_kind: str
    target_ref: str
    target_version: int | None
    state: str
    disposition_note: str
    resolved_in_version: int | None
    created_at: str
    mine: bool


class StagedEditOut(StrictModel):
    edit_id: str
    key: str
    action: str
    value: str
    reasoning: str
    by_name: str
    at: str


class RoundPayloadOut(RoundOut):
    notes: list[RoundNoteOut] = []
    staged_edits: list[StagedEditOut] = []
```

and on `StoryboardOut`: `round: RoundPayloadOut | None = None` and `previous_round: RoundPayloadOut | None = None`, with a `#` comment: "Round notes are visible to every reader while the round is open (spec C, Visibility) — names only, never emails."

`services.resolve_board(board, *, is_member=False, viewer=None)` adds:

```python
        "round": rounds.round_payload(board, rounds.current(board), viewer=viewer),
        "previous_round": rounds.round_payload(board, rounds.latest_finished(board), viewer=viewer),
```

(import `from apps.storyboards import rounds` inside the function to avoid an import cycle). In `api.py` pass `viewer=request.user` from `get_storyboard`, `create_storyboard`, `patch_storyboard`.

Update `list_notes`'s docstring: the independence rule still holds **outside** a round; inside one, `GET /{slug}` shows round notes to everyone by design (spec C).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_storyboard_rounds.py tests/test_storyboard_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/storyboards tests/test_storyboard_rounds.py
git commit -m "feat(storyboards): round notes are visible to everyone in the round"
```

---

### Task 4: Decision edits inside a round are staged

**Files:**
- Modify: `apps/storyboards/api.py:edit_storyboard_decision`, `apps/storyboards/decisions.py:serialize` (+`pending`), `apps/storyboards/schemas.py:DecisionOut`, `apps/storyboards/services.py:board_decisions`
- Modify: `tests/test_storyboard_decisions.py::test_non_member_edit_is_refused` (403 → 409, the behaviour changed on purpose)
- Test: `tests/test_storyboard_rounds.py` (append)

**Interfaces:**
- Consumes: `rounds.current`, `frounds.stage_edit`, `decisions.apply_edit`.
- Produces: `decisions.serialize(row, *, editable=True, pending: list[dict] | None = None)`; `DecisionOut.pending: list[StagedEditOut] = []`; `decisions.check_edit(row, *, action, value) -> None` (validates a staged edit without saving).

- [ ] **Step 1: Failing tests** (append)

```python
from apps.storyboards import decisions as dsvc

ROW = {"id": "banner", "question": "Rename?", "options": ["Rename and re-record", "Keep as is"],
       "recommended": "Rename and re-record", "review_ask": "required-before",
       "needed_by": "external-share"}


def _edit(world, client=None, token=True, **body):
    c = client or Client()
    q = f"?t={world['token']}" if token else ""
    return c.post(f"/api/storyboards/supply/decisions/banner/edit{q}", body,
                  content_type="application/json")


def test_in_round_a_guest_edit_is_staged_not_applied(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    resp = _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["value"] == "Rename and re-record"
    assert body["pending"][0]["by_name"] == "Sophie"
    assert world["board"].decisions.get(key="banner").status == "ai-default"


def test_in_round_a_member_edit_is_staged_too(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, client=world["viewer"], token=False, action="confirm")
    assert world["board"].decisions.get(key="banner").confirmed is False
    assert FeedbackRound.objects.get().staged_edits[0]["by_name"] == "Lily"


def test_guest_needs_suggest_grant_and_a_name(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    assert _edit(world, action="confirm").status_code == 422
    world["board"].capability = Storyboard.CAP_COMMENT
    world["board"].save()
    assert _edit(world, action="confirm", author_name="S").status_code == 403


def test_staged_edit_is_validated_now(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    assert _edit(world, action="pick", value="Not an option", author_name="S").status_code == 422
    assert _edit(world, action="undo", author_name="S").status_code == 409


def test_outside_a_round_a_guest_edit_is_409(world):
    dsvc.publish(world["board"], [ROW])
    resp = _edit(world, action="confirm", author_name="S")
    assert resp.status_code == 409 and "round" in resp.json()["detail"]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storyboard_rounds.py -k "staged or guest_edit or grant" -v`
Expected: FAIL (403 / no `pending`).

- [ ] **Step 3: Implement**

`apps/storyboards/decisions.py`:

```python
def check_edit(row, *, action: str, value: str = "") -> None:
    """Validate an edit that will be applied later (staged in a round)."""
    if action not in EDIT_ACTIONS:
        raise DecisionInvalid(f"action must be one of {list(EDIT_ACTIONS)}")
    if action == "undo":
        raise DecisionConflict("undo is available after the round closes")
    value = (value or "").strip()
    if action == "pick" and value not in row.options:
        raise DecisionInvalid(f"{value!r} is not one of this decision's options")
    if action == "write_in":
        if not value:
            raise DecisionInvalid("a write-in needs a value")
        if len(value.split()) > _MAX_OPTION_WORDS:
            raise DecisionInvalid(f"a write-in is at most {_MAX_OPTION_WORDS} words")
```

`serialize(row, *, editable=True, pending=None)` adds `"pending": list(pending or [])`. `DecisionOut` gains `pending: list[StagedEditOut] = []` (move `StagedEditOut` above `DecisionOut`). `services.board_decisions(board)` passes `pending=[e for e in staged if e["key"] == d.key]`, where `staged` is `rounds.current(board).staged_edits` (or `[]`), trimmed to the `StagedEditOut` keys.

Route body becomes:

```python
    board = _readable_or_404(request, slug, ws)
    row = board.decisions.filter(key=key).first()
    if row is None:
        raise HttpError(404, "no such decision on this storyboard")
    member = _is_member(request, board)
    token = request.GET.get("t")
    if member and not token and csrf_rejected(request):
        raise HttpError(403, "CSRF verification failed")
    rnd = board_rounds.current(board)
    try:
        if rnd is not None:
            if not member and not board.grants(Storyboard.CAP_SUGGEST, token):
                raise HttpError(403, "this link does not grant changing decisions")
            name = board_rounds.author_for(request.user, payload.author_name,
                                           member=member, in_round=True)
            decision_rows.check_edit(row, action=payload.action, value=payload.value)
            board_rounds.stage(rnd, row, action=payload.action, value=payload.value,
                               reasoning=payload.reasoning, by_name=name,
                               by_user=request.user if member else None)
            if member:
                board_rounds.link_participant(board, request.user)
            return decision_rows.serialize(row, pending=board_rounds.pending_for(board, row.key))
        if not member:
            raise HttpError(409, "No feedback round is open, so only a member can change a "
                                 "decision right now.")
        row = decision_rows.apply_edit(row, action=payload.action, value=payload.value,
                                       reasoning=payload.reasoning, by_user=request.user)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    except decision_rows.DecisionInvalid as e:
        raise HttpError(422, str(e))
    except decision_rows.DecisionConflict as e:
        raise HttpError(409, str(e))
    return decision_rows.serialize(row)
```

`apps/storyboards/rounds.py` (append):

```python
_STAGED_KEYS = ("edit_id", "key", "action", "value", "reasoning", "by_name", "at")


def stage(rnd, row, *, action, value, reasoning, by_name, by_user=None) -> dict:
    return frounds.stage_edit(rnd, {
        "key": row.key, "action": action, "value": (value or "").strip(),
        "reasoning": (reasoning or "").strip(), "by_name": by_name,
        "by_user_id": by_user.pk if getattr(by_user, "is_authenticated", False) else None,
    })


def pending_for(board, key: str) -> list[dict]:
    rnd = current(board)
    if rnd is None:
        return []
    return [{k: e.get(k, "") for k in _STAGED_KEYS} for e in rnd.staged_edits if e.get("key") == key]
```

Update the route's docstring: "Inside an open round every edit is staged (members too); outside one a member's edit applies immediately and a link holder's is refused." Update `GATES["edit_storyboard_decision"]`'s comment accordingly. In `tests/test_storyboard_decisions.py`, change `test_non_member_edit_is_refused` to expect 409.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_storyboard_rounds.py tests/test_storyboard_decisions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/storyboards tests/
git commit -m "feat(storyboards): decision edits inside a round are staged until close"
```

---

### Task 5: Closing a round — staged edits land, one task, one turn

**Files:**
- Modify: `apps/storyboards/rounds.py` (`close_round`, `_hand_to_agent`), `apps/storyboards/api.py` (route), `apps/storyboards/schemas.py` (`RoundCloseIn`), `apps/api/route_gates.py`
- Test: `tests/test_storyboard_rounds.py` (append)

**Interfaces:**
- Consumes: `frounds.lock_open`, `frounds.mark_closed`, `decisions.apply_edit`, `apps.agents.services.create_tasks`, `apps.agents.services.act`.
- Produces: `rounds.close_round(board, rnd, *, by_user, note: str, picks: dict[str, str]) -> FeedbackRound`; route `POST /api/storyboards/{slug}/rounds/{number}/close` (`close_storyboard_round`) body `RoundCloseIn {close_note: str = "", picks: dict[str, str] = {}}` (row key → chosen `edit_id`) → `RoundOut`.

- [ ] **Step 1: Failing tests** (append)

```python
from apps.agents.models import AgentTask
from apps.harness.models import Turn


def _close(world, client=None, **body):
    c = client or world["owner"]
    return c.post("/api/storyboards/supply/rounds/1/close", body, content_type="application/json")


def test_close_applies_edits_and_starts_exactly_one_turn(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    for i in range(12):
        _note(world, author_name=f"Reviewer {i}", body=f"note {i}")
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    resp = _close(world, close_note="Lily's team is done")
    assert resp.status_code == 200, resp.content
    rnd = FeedbackRound.objects.get()
    assert rnd.state == "closed" and rnd.task is not None
    row = world["board"].decisions.get(key="banner")
    assert row.override == "Keep as is" and row.decided_by_name == "Sophie"
    assert row.decided_by_verified is False
    task = rnd.task
    assert task.project == world["project"] and task.agent == world["agent"]
    assert "12 notes" in task.title
    assert Turn.objects.filter(raised_from_task=task).count() == 1


def test_double_close_creates_one_task_and_one_turn(world):
    _open(world)
    _note(world, author_name="S")
    assert _close(world).status_code == 200
    assert _close(world).status_code == 409
    assert AgentTask.objects.count() == 1
    assert Turn.objects.count() == 1


def test_close_with_unpicked_conflict_is_409_and_changes_nothing(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    _edit(world, client=world["viewer"], token=False, action="confirm")
    resp = _close(world)
    assert resp.status_code == 409 and "banner" in resp.json()["detail"]
    assert FeedbackRound.objects.get().state == "open"
    assert world["board"].decisions.get(key="banner").status == "ai-default"
    assert AgentTask.objects.count() == 0


def test_close_with_a_pick_applies_the_chosen_edit(world):
    dsvc.publish(world["board"], [ROW])
    _open(world)
    _edit(world, action="pick", value="Keep as is", author_name="Sophie")
    _edit(world, client=world["viewer"], token=False, action="confirm")
    lily = next(e for e in FeedbackRound.objects.get().staged_edits if e["by_name"] == "Lily")
    assert _close(world, picks={"banner": lily["edit_id"]}).status_code == 200
    row = world["board"].decisions.get(key="banner")
    assert row.confirmed is True and row.decided_by == world["users"]["viewer"]


def test_only_editors_close(world):
    _open(world)
    assert _close(world, client=world["viewer"]).status_code == 403
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storyboard_rounds.py -k close -v`
Expected: FAIL — route 404/405.

- [ ] **Step 3: Implement** — `apps/storyboards/rounds.py` (append):

```python
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.db import transaction


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def _choose(staged: list[dict], picks: dict[str, str]) -> list[dict]:
    """One edit per row. Same intent from several people is not a conflict;
    different intents need the closer's pick (spec C, Closing a round)."""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for e in staged:
        by_key[e["key"]].append(e)
    chosen, conflicts = [], []
    for key, edits in by_key.items():
        intents = {(e["action"] if e["action"] != "write_in" else "pick", _norm(e.get("value", "")))
                   for e in edits}
        if len(intents) == 1:
            chosen.append(edits[-1])
        elif key in picks:
            match = next((e for e in edits if e["edit_id"] == picks[key]), None)
            if match is None:
                raise RoundError(422, f"{key}: the picked edit is not one of this round's edits")
            chosen.append(match)
        else:
            conflicts.append(key)
    if conflicts:
        raise RoundError(409, "People disagreed on these decisions; pick one edit for each: "
                              + ", ".join(sorted(conflicts)))
    return chosen


def _hand_to_agent(board, rnd, *, by_user, note: str):
    """One task on the board's project, approved once → exactly one turn."""
    from apps.agents import services as agent_services
    from apps.workspaces import services as wsvc

    project = board.agent_project
    notes = rnd.notes.count()
    people = rnd.notes.values("author_name").distinct().count()
    where = f"?ws={board.workspace_id}"
    (task,) = agent_services.create_tasks(project.agent, [{
        "title": f"Feedback round {rnd.number} on “{board.title}”: "
                 f"{notes} notes from {people} {'person' if people == 1 else 'people'}",
        "ask_kind": "review",
        "ask_body": note or "",
        "project": project.ext_id,
        "source_url": wsvc.scoped_url(board.workspace_id, f"/storyboard/{board.slug}"),
        "next_action": f"Run ddd-feedback-round for storyboard {board.slug} round {rnd.number}.",
        "plan": (f"1. Read the round: GET /api/storyboards/{board.slug}/rounds/{rnd.number}{where}\n"
                 "2. Answer every note: POST /api/feedback/<id>/resolve (answered or declined).\n"
                 "3. Publish the next version of each narrative the notes change.\n"
                 f"4. POST /api/storyboards/{board.slug}/rounds/{rnd.number}/consume{where} "
                 "with the run id — this opens the next round."),
        "idempotency_key": f"storyboard-round-{rnd.pk}",
    }])
    agent_services.act(
        task, action="approve", comment=note or "", by=by_user.email, by_user=by_user,
        actor_workspace_ids={board.workspace_id}, may_start_turns=True,
    )
    return task


def close_round(board, rnd, *, by_user, note: str = "", picks: dict[str, str] | None = None):
    from apps.storyboards import decisions as decision_rows

    User = get_user_model()
    with transaction.atomic():
        rnd = frounds.lock_open(rnd)
        for e in _choose(list(rnd.staged_edits), picks or {}):
            row = board.decisions.filter(key=e["key"]).first()
            if row is None:  # the publisher dropped it since; nothing to apply
                continue
            user = User.objects.filter(pk=e.get("by_user_id")).first() if e.get("by_user_id") else None
            decision_rows.apply_edit(row, action=e["action"], value=e.get("value", ""),
                                     reasoning=e.get("reasoning", ""), by_user=user,
                                     by_name=e.get("by_name", ""))
        frounds.mark_closed(rnd, by_user=by_user, note=note)
        rnd.task = _hand_to_agent(board, rnd, by_user=by_user, note=note)
        rnd.save(update_fields=["task"])
    return rnd
```

Note for the implementer: `apply_edit` with `by_user=None` uses `by_name` and sets `decided_by_verified=False`, which is what `test_close_applies_edits…` asserts for a guest.

Schema: `class RoundCloseIn(StrictModel): close_note: str = ""; picks: dict[str, str] = {}` (docstring: "`picks` maps a decision id to the staged edit chosen where people disagreed.").

Route:

```python
@router.post("/{slug}/rounds/{number}/close", response=RoundOut, auth=session_auth,
             summary="Close a feedback round and hand it to the project's agent")
def close_storyboard_round(request: HttpRequest, slug: str, number: int, payload: RoundCloseIn) -> dict:
    """Editors only. Applies staged decision edits, then creates one task on the
    board's project and approves it — exactly one agent turn for the whole round."""
    board = _editable_or_404(request, slug)
    rnd = board_rounds.by_number(board, number)
    if rnd is None:
        raise HttpError(404, "no such round")
    try:
        rnd = board_rounds.close_round(board, rnd, by_user=request.user, note=payload.close_note,
                                       picks=payload.picks)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    return board_rounds.summary(rnd)
```

Gates: `GATES["close_storyboard_round"] = ("content.write",)`, `NOT_SETTINGS["close_storyboard_round"] = "content"`.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_storyboard_rounds.py tests/test_task_actions.py tests/test_every_route_declares_its_gate.py -v`
Expected: PASS. If `act()` refuses because the new task is not "live" or because `create_tasks` rejected a field, read the error and fix the payload (`create_tasks` takes only `_TASK_FIELDS` + `_TASK_CREATE_EXTRAS` + `ask_kind`, `project`, `idempotency_key`, …) — do not loosen `act()`.

- [ ] **Step 5: Commit**

```bash
git add apps/storyboards apps/api/route_gates.py tests/test_storyboard_rounds.py
git commit -m "feat(storyboards): closing a round applies its edits and starts one agent turn"
```

---

### Task 6: The agent's read, and consume → next round

**Files:**
- Modify: `apps/storyboards/rounds.py` (`detail`, `consume_round`), `apps/storyboards/api.py`, `apps/storyboards/schemas.py`, `apps/api/route_gates.py`
- Test: `tests/test_storyboard_rounds.py` (append)

**Interfaces:**
- Produces:
  - `GET /api/storyboards/{slug}/rounds/{number}` (`get_storyboard_round`, members) → `RoundDetailOut` = `RoundPayloadOut` + `notes[].author_email` + `decisions: list[DecisionOut]` (current rulings) + `board_url: str`.
  - `POST /api/storyboards/{slug}/rounds/{number}/consume` (`consume_storyboard_round`, editors) body `RoundConsumeIn {run_id: str}` → `RoundOut` of the NEW open round.
  - `rounds.consume_round(board, rnd, *, run_id, by_user) -> FeedbackRound` (returns the next round).

- [ ] **Step 1: Failing tests** (append)

```python
def _closed_round_with_a_note(world):
    _open(world)
    _note(world, author_name="Sophie", author_email="s@example.org")
    assert _close(world).status_code == 200
    return FeedbackRound.objects.get(number=1)


def test_agent_reads_the_round_with_emails_and_rulings(world):
    dsvc.publish(world["board"], [ROW])
    _closed_round_with_a_note(world)
    body = world["ace"].get("/api/w/connect/storyboards/supply/rounds/1").json()
    assert body["state"] == "closed" and body["notes"][0]["author_email"] == "s@example.org"
    assert body["decisions"][0]["id"] == "banner"


def test_token_holder_cannot_read_round_detail(world):
    _closed_round_with_a_note(world)
    assert Client().get(f"/api/storyboards/supply/rounds/1?t={world['token']}").status_code in (401, 404)


def test_consume_refuses_unanswered_notes(world):
    _closed_round_with_a_note(world)
    resp = world["ace"].post("/api/storyboards/supply/rounds/1/consume", {"run_id": "r1"},
                             content_type="application/json")
    assert resp.status_code == 409
    assert FeedbackRound.objects.filter(state="open").count() == 0


def test_consume_opens_the_next_round_on_new_versions(world, monkeypatch):
    from apps.feedback import services as fsvc

    rnd = _closed_round_with_a_note(world)
    for fb in rnd.notes.all():
        fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="declined", note="out of scope")
    monkeypatch.setattr(board_rounds, "pinned_versions", lambda b: {"supply-rutf": 5})
    resp = world["ace"].post("/api/storyboards/supply/rounds/1/consume", {"run_id": "r1"},
                             content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["number"] == 2 and resp.json()["pinned_versions"] == {"supply-rutf": 5}
    assert FeedbackRound.objects.get(number=1).state == "consumed"
    assert FeedbackRound.objects.get(number=1).consumed_by_run == "r1"


def test_note_left_while_closed_is_adopted_by_the_next_round(world):
    from apps.feedback import services as fsvc

    rnd = _closed_round_with_a_note(world)
    _note(world, author_name="Late")
    for fb in rnd.notes.all():
        fsvc.resolve(fb.pk, workspace_slugs=["connect"], state="answered", note="done")
    world["ace"].post("/api/storyboards/supply/rounds/1/consume", {"run_id": "r1"},
                      content_type="application/json")
    assert Feedback.objects.get(author_name="Late").round.number == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storyboard_rounds.py -k "consume or reads or cannot_read or adopted" -v`
Expected: FAIL — routes missing.

- [ ] **Step 3: Implement** — `apps/storyboards/rounds.py` (append):

```python
def detail(board, rnd, *, viewer) -> dict:
    """What the agent reads: every note with its author's email, the staged
    edits, and the board's current decisions (rulings are binding)."""
    from apps.storyboards import services
    from apps.workspaces import services as wsvc

    out = round_payload(board, rnd, viewer=viewer)
    emails = dict(rnd.notes.values_list("pk", "author_email"))
    for n in out["notes"]:
        n["author_email"] = emails.get(n["id"], "")
    out["decisions"] = services.board_decisions(board)
    out["board_url"] = wsvc.scoped_url(board.workspace_id, f"/storyboard/{board.slug}")
    return out


def consume_round(board, rnd, *, run_id: str, by_user):
    with transaction.atomic():
        frounds.mark_consumed(rnd, run_id=run_id)
        return open_round(board, by_user=by_user)
```

Schemas:

```python
class RoundDetailNoteOut(RoundNoteOut):
    author_email: str


class RoundDetailOut(RoundOut):
    notes: list[RoundDetailNoteOut]
    staged_edits: list[StagedEditOut]
    decisions: list[DecisionOut]
    board_url: str


class RoundConsumeIn(StrictModel):
    run_id: str
```

Routes:

```python
@router.get("/{slug}/rounds/{number}", response=RoundDetailOut, auth=session_auth,
            summary="Read a feedback round in full (members)")
def get_storyboard_round(request: HttpRequest, slug: str, number: int) -> dict:
    """The agent's read of a round. Members only; a token holder 404s."""
    board = _owned_or_404(request, slug)
    rnd = board_rounds.by_number(board, number)
    if rnd is None:
        raise HttpError(404, "no such round")
    return board_rounds.detail(board, rnd, viewer=request.user)


@router.post("/{slug}/rounds/{number}/consume", response=RoundOut, auth=session_auth,
             summary="Mark a closed round consumed and open the next one")
def consume_storyboard_round(request: HttpRequest, slug: str, number: int,
                             payload: RoundConsumeIn) -> dict:
    """Editors only (the agent's PAT). Refuses while any note is unanswered;
    opens round N+1 on each narrative's current version."""
    board = _editable_or_404(request, slug)
    rnd = board_rounds.by_number(board, number)
    if rnd is None:
        raise HttpError(404, "no such round")
    try:
        nxt = board_rounds.consume_round(board, rnd, run_id=payload.run_id, by_user=request.user)
    except board_rounds.RoundError as e:
        raise HttpError(e.status, e.detail)
    return board_rounds.summary(nxt)
```

Gates: `"get_storyboard_round": ("member",)`, `"consume_storyboard_round": ("content.write",)`; `NOT_SETTINGS["consume_storyboard_round"] = "content"`.

- [ ] **Step 4: Run tests, regenerate types, commit**

Run: `uv run pytest tests/test_storyboard_rounds.py tests/test_feedback_rounds.py tests/test_every_route_declares_its_gate.py -v` → PASS.
Run: `cd frontend && npm run gen:api:local && cd ..`

```bash
git add apps/storyboards apps/api/route_gates.py frontend/src/api/generated.ts tests/
git commit -m "feat(storyboards): the agent reads a round; consuming it opens the next"
```

---

### Task 7: Review-page suggestions join the same pool

**Files:**
- Modify: `apps/storyboards/rounds.py` (`ingest_review_suggestion`), `apps/reviews/api.py:suggest_review`
- Test: `tests/test_storyboard_rounds.py` (append)

**Interfaces:**
- Produces: `rounds.ingest_review_suggestion(review, *, name, email, response_json, by_user=None) -> list[int]` — one `Feedback` per edited scene (kind `suggestion`, `anchor_id` = scene id, `suggested_text` = new narration; `body` = the scene's `feedback`; a deleted scene → body "Remove this scene"), plus one comment for `overall_feedback`; attached to the open round of any board in the review's workspace that carries the narrative. Never raises (logs).

- [ ] **Step 1: Failing tests** (append)

```python
from apps.reviews.models import ReviewRequest


def test_review_suggestions_land_in_the_open_round(world):
    _open(world)
    review = ReviewRequest.objects.create(run_id="supply-rutf-2026-10-14-001", narrative_slug="supply-rutf",
                                          version=3, workspace=world["ws"], gate="concept_change",
                                          visibility="link", request_json={})
    resp = world["viewer"].post(f"/api/reviews/{review.pk}/suggest/", {
        "response_json": {"decisions": {}, "overall_feedback": "Lead with the forecast",
                          "edited_scenes": [{"id": "overview", "title": "t", "narration": "New words",
                                             "deleted": False, "features": [], "feedback": "shorter"}]},
    }, content_type="application/json")
    assert resp.status_code == 200, resp.content
    rows = Feedback.objects.filter(round__number=1, target_ref="supply-rutf")
    assert {(r.kind, r.anchor_id) for r in rows} == {("suggestion", "overview"), ("comment", "")}
    assert rows.get(kind="suggestion").suggested_text == "New words"
    assert rows.get(kind="suggestion").target_version == 3
```

Check `ReviewRequest`'s required fields (`apps/reviews/models.py`) and its share/visibility field names before running; adjust the create kwargs to the real ones (e.g. a share token field) — keep the assertions.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_storyboard_rounds.py -k review_suggestions -v` → FAIL (no rows).

- [ ] **Step 3: Implement** — `apps/storyboards/rounds.py`:

```python
def ingest_review_suggestion(review, *, name, email, response_json, by_user=None) -> list[int]:
    """A review-page suggestion is feedback too: one narrative, one pool (spec C)."""
    try:
        from apps.feedback import services as fsvc

        slug = review.narrative_slug or ""
        rj = response_json if isinstance(response_json, dict) else {}
        base = {"target_kind": "narrative", "target_ref": slug, "target_version": review.version,
                "author_name": (name or "").strip(), "author_email": (email or "").strip(),
                "channel": "web", "source_ref": ""}
        items = []
        for s in rj.get("edited_scenes") or []:
            if not isinstance(s, dict) or not s.get("id"):
                continue
            if s.get("deleted"):
                items.append({**base, "kind": "comment", "anchor_id": s["id"], "body": "Remove this scene"})
            else:
                items.append({**base, "kind": "suggestion", "anchor_id": s["id"],
                              "suggested_text": s.get("narration") or "", "body": s.get("feedback") or ""})
        for scene_id, text in (rj.get("narration_edits") or {}).items():
            items.append({**base, "kind": "suggestion", "anchor_id": scene_id, "suggested_text": text})
        if (rj.get("overall_feedback") or "").strip():
            items.append({**base, "kind": "comment", "anchor_id": "", "body": rj["overall_feedback"]})
        if not items or not slug:
            return []
        board = (Storyboard.objects.filter(workspace_id=review.workspace_id,
                                           acts__entries__narrative_slug=slug).distinct())
        rnd = next((r for r in (current(b) for b in board) if r is not None), None)
        return fsvc.ingest(items, workspace=review.workspace, submitted_by=by_user, round=rnd)["ids"]
    except Exception:  # noqa: BLE001 — a pool write must never cost the reviewer their suggestion
        logger.exception("rounds: could not pool review %s's suggestion", review.pk)
        return []
```

In `apps/reviews/api.py:suggest_review`, after `count = review.add_suggestion(...)`:

```python
    # One place a narrative's feedback lives (spec C): the suggestion also lands
    # in the Feedback pool, in a storyboard round when one is open.
    from apps.storyboards import rounds as board_rounds  # product→product, imported late: no cycle

    board_rounds.ingest_review_suggestion(
        review, name=name, email=email, response_json=payload.response_json,
        by_user=request.user if request.user.is_authenticated else None,
    )
```

- [ ] **Step 4: Run** — `uv run pytest tests/test_storyboard_rounds.py tests/ -k "review" -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/storyboards/rounds.py apps/reviews/api.py tests/test_storyboard_rounds.py
git commit -m "feat(reviews): suggestions also land in the feedback pool and the open round"
```

---

### Task 8: The round on the page

**Files:**
- Modify: `frontend/src/api/storyboards.ts`
- Create: `frontend/src/components/storyboard/RoundBanner.tsx`, `RoundBanner.test.tsx`, `MyNotes.tsx`, `MyNotes.test.tsx`
- Modify: `frontend/src/components/storyboard/DecisionLog.tsx` (+ test), `NoteComposer.tsx` (export `readReviewerName`), `frontend/src/pages/StoryboardPage.tsx`
- Modify: `docs/architecture/pages.md`

**Interfaces:**
- Consumes: `StoryboardOut.round` / `previous_round` (Task 3), `DecisionOut.pending` (Task 4), the open/close routes (Tasks 2, 5).
- Produces:
  - Types `RoundNote`, `StagedEdit`, `RoundPayload`; `Storyboard.round?: RoundPayload | null`, `Storyboard.previous_round?: RoundPayload | null`; `Decision.pending?: StagedEdit[]`.
  - `openRound(slug, ws?) : Promise<RoundSummary>`, `closeRound(slug, number, body: {close_note?: string; picks?: Record<string,string>}, ws?) : Promise<RoundSummary>`.
  - `conflictsOf(staged: StagedEdit[]): Record<string, StagedEdit[]>` (rows whose staged intents differ).
  - `<RoundBanner board canManage ws onChanged />`, `<MyNotes round viewerName />`, `export function mineOf(notes: RoundNote[], viewerName: string): RoundNote[]`.

- [ ] **Step 1: Failing tests**

`RoundBanner.test.tsx`:

```tsx
import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { conflictsOf, RoundBanner } from './RoundBanner'
import type { StagedEdit, Storyboard } from '@/api/storyboards'

const edit = (o: Partial<StagedEdit>): StagedEdit =>
  ({ edit_id: 'e', key: 'banner', action: 'pick', value: 'Keep as is', reasoning: '', by_name: 'S', at: '', ...o })

describe('conflictsOf', () => {
  it('flags rows whose staged edits disagree, not rows where people agree', () => {
    const c = conflictsOf([
      edit({ edit_id: '1' }), edit({ edit_id: '2', by_name: 'L', value: 'keep as IS' }),
      edit({ edit_id: '3', key: 'sqlns', action: 'confirm', value: '' }),
      edit({ edit_id: '4', key: 'sqlns', value: 'Add SQ-LNS' }),
    ])
    expect(Object.keys(c)).toEqual(['sqlns'])
  })
})

const board = (round: Storyboard['round']): Storyboard => ({
  slug: 'supply', title: 'Connect Supply', lede: '', capability: 'suggest', layout: 'review',
  is_member: true, acts: [], decisions: [], round, previous_round: null,
})

describe('RoundBanner', () => {
  it('offers an editor the Open button when no round is open', () => {
    render(<RoundBanner board={board(null)} canManage onChanged={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Open a feedback round' })).toBeTruthy()
  })

  it('shows counts and asks the closer to pick where people disagreed', () => {
    const round = { number: 1, state: 'open', opened_at: '', opened_by: 'Jonathan', closed_at: null,
      close_note: '', pinned_versions: {}, note_count: 7, people_count: 3, notes: [],
      staged_edits: [edit({ edit_id: '1' }), edit({ edit_id: '2', action: 'confirm', value: '' })] }
    render(<RoundBanner board={board(round)} canManage onChanged={vi.fn()} />)
    expect(screen.getByText(/7 notes from 3 people/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close round and start the next run' }))
    expect(screen.getByText(/People disagreed/)).toBeTruthy()
    expect(screen.getAllByRole('radio')).toHaveLength(2)
  })

  it('a non-editor sees the round but no controls', () => {
    const round = { number: 2, state: 'open', opened_at: '', opened_by: '', closed_at: null,
      close_note: '', pinned_versions: {}, note_count: 0, people_count: 0, notes: [], staged_edits: [] }
    render(<RoundBanner board={board(round)} canManage={false} onChanged={vi.fn()} />)
    expect(screen.getByText(/Feedback round 2 is open/)).toBeTruthy()
    expect(screen.queryByRole('button')).toBeNull()
  })
})
```

`MyNotes.test.tsx`:

```tsx
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { mineOf, MyNotes } from './MyNotes'
import type { RoundNote } from '@/api/storyboards'

const note = (o: Partial<RoundNote>): RoundNote => ({ id: 1, kind: 'comment', body: 'b', suggested_text: '',
  author_name: 'Sophie', anchor_id: '', target_kind: 'narrative', target_ref: 'n', target_version: 3,
  state: 'answered', disposition_note: 'Slowed cut 1', resolved_in_version: 4, created_at: '', mine: false, ...o })

describe('MyNotes', () => {
  it('mine = flagged by the server, or (guest) the remembered name', () => {
    const notes = [note({ id: 1, mine: true, author_name: 'Lily' }), note({ id: 2 }), note({ id: 3, author_name: 'Bob' })]
    expect(mineOf(notes, 'sophie').map((n) => n.id)).toEqual([1, 2])
  })

  it('says what happened to each note', () => {
    const round = { number: 1, state: 'consumed', opened_at: '', opened_by: '', closed_at: null, close_note: '',
      pinned_versions: {}, note_count: 1, people_count: 1, notes: [note({ mine: true })], staged_edits: [] }
    render(<MyNotes round={round} viewerName="" />)
    expect(screen.getByText('Changed in version 4')).toBeTruthy()
    expect(screen.getByText('Slowed cut 1')).toBeTruthy()
  })
})
```

Add to `DecisionLog.test.tsx`:

```tsx
it('shows pending edits from the round', () => {
  render(<DecisionRow d={{ ...base, pending: [{ edit_id: '1', key: 'banner', action: 'pick',
    value: 'Keep as is', reasoning: '', by_name: 'Sophie', at: '' }] }} canEdit={false} onEdit={vi.fn()} />)
  expect(screen.getByText('Pending: Sophie → Keep as is')).toBeTruthy()
})
```

- [ ] **Step 2: Run to verify failure**

Run: `cd frontend && npx vitest run src/components/storyboard`
Expected: FAIL — modules missing.

- [ ] **Step 3: Implement**

`frontend/src/api/storyboards.ts` — add the types (mirroring `RoundNoteOut`, `StagedEditOut`, `RoundPayloadOut`), the two optional `Storyboard` fields, `pending?: StagedEdit[]` on `Decision`, and:

```ts
export type RoundSummary = Omit<RoundPayload, 'notes' | 'staged_edits'>

export async function openRound(slug: string, ws?: string | null): Promise<RoundSummary> {
  return postJson(withToken(`/api/storyboards/${encodeURIComponent(slug)}/rounds`, null, ws), {},
    'Couldn’t open the round')
}

export async function closeRound(
  slug: string, number: number, body: { close_note?: string; picks?: Record<string, string> },
  ws?: string | null,
): Promise<RoundSummary> {
  return postJson(withToken(`/api/storyboards/${encodeURIComponent(slug)}/rounds/${number}/close`, null, ws),
    body, 'Couldn’t close the round')
}
```

(`postJson` is the helper plan B Task 5 extracted; if it was not extracted, extract it now from `leaveFeedback`.)

`RoundBanner.tsx`:

```tsx
import { useState } from 'react'
import type { StagedEdit, Storyboard } from '@/api/storyboards'
import { closeRound, openRound } from '@/api/storyboards'

const norm = (s: string) => s.trim().replace(/\s+/g, ' ').toLowerCase()
const intent = (e: StagedEdit) => `${e.action === 'write_in' ? 'pick' : e.action}|${norm(e.value)}`

/** Rows where people staged different edits — the closer must pick one each. */
export function conflictsOf(staged: StagedEdit[]): Record<string, StagedEdit[]> {
  const byKey: Record<string, StagedEdit[]> = {}
  for (const e of staged) (byKey[e.key] ??= []).push(e)
  return Object.fromEntries(
    Object.entries(byKey).filter(([, es]) => new Set(es.map(intent)).size > 1),
  )
}

export function RoundBanner({ board, canManage, ws, onChanged }: {
  board: Storyboard
  canManage: boolean
  ws?: string | null
  onChanged: () => void
}) {
  const round = board.round
  const [closing, setClosing] = useState(false)
  const [note, setNote] = useState('')
  const [picks, setPicks] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const run = async (fn: () => Promise<unknown>) => {
    setError('')
    try { await fn(); onChanged() } catch (e) { setError(e instanceof Error ? e.message : 'That failed') }
  }
  if (!round) {
    if (!canManage) return null
    return (
      <div className="rounded-md border border-border bg-muted p-4 text-sm">
        <p className="text-foreground">No feedback round is open. Open one to collect everyone’s notes, then close it to start the next run.</p>
        <button type="button" className="mt-2 rounded-md bg-primary px-3 py-1 text-primary-foreground"
          onClick={() => run(() => openRound(board.slug, ws))}>Open a feedback round</button>
        {error && <p role="alert" className="mt-2 text-destructive">{error}</p>}
      </div>
    )
  }
  const conflicts = conflictsOf(round.staged_edits)
  return (
    <div className="rounded-md border border-border bg-muted p-4 text-sm">
      <p className="text-foreground">
        Feedback round {round.number} is open. Everyone’s notes are shown. {round.note_count} notes from{' '}
        {round.people_count} {round.people_count === 1 ? 'person' : 'people'} so far.
      </p>
      {canManage && !closing && (
        <button type="button" className="mt-2 rounded-md bg-primary px-3 py-1 text-primary-foreground"
          onClick={() => setClosing(true)}>Close round and start the next run</button>
      )}
      {canManage && closing && (
        <div className="mt-3 flex flex-col gap-3">
          {Object.keys(conflicts).length > 0 && (
            <fieldset className="flex flex-col gap-2">
              <legend className="font-medium text-foreground">People disagreed. Pick one answer for each:</legend>
              {Object.entries(conflicts).map(([key, edits]) => (
                <div key={key} className="flex flex-col gap-1">
                  <span className="text-muted-foreground">{board.decisions?.find((d) => d.id === key)?.plain_question ?? key}</span>
                  {edits.map((e) => (
                    <label key={e.edit_id} className="flex items-center gap-2">
                      <input type="radio" name={`pick-${key}`} checked={picks[key] === e.edit_id}
                        onChange={() => setPicks((p) => ({ ...p, [key]: e.edit_id }))} />
                      {e.by_name}: {e.action === 'confirm' ? 'keep the recommendation' : e.value}
                    </label>
                  ))}
                </div>
              ))}
            </fieldset>
          )}
          <textarea aria-label="Note for the agent" value={note} onChange={(e) => setNote(e.target.value)}
            placeholder="Anything the agent should know (optional)"
            className="rounded-md border border-border bg-background p-2" />
          <div className="flex gap-2">
            <button type="button" className="rounded-md bg-primary px-3 py-1 text-primary-foreground"
              onClick={() => run(() => closeRound(board.slug, round.number, { close_note: note, picks }, ws))}>
              Close and hand to the agent</button>
            <button type="button" className="rounded-md border border-border px-3 py-1" onClick={() => setClosing(false)}>Cancel</button>
          </div>
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-destructive">{error}</p>}
    </div>
  )
}
```

`MyNotes.tsx`:

```tsx
import type { RoundNote, RoundPayload } from '@/api/storyboards'

/** The server marks a signed-in author's notes; a guest is known only by the
 *  name they typed, which NoteComposer remembers on this device. */
export function mineOf(notes: RoundNote[], viewerName: string): RoundNote[] {
  const name = viewerName.trim().toLowerCase()
  return notes.filter((n) => n.mine || (name !== '' && n.author_name.trim().toLowerCase() === name))
}

function outcome(n: RoundNote): string {
  if (n.state === 'answered') return n.resolved_in_version ? `Changed in version ${n.resolved_in_version}` : 'Answered'
  if (n.state === 'declined') return 'Not changed'
  return 'Not answered yet'
}

export function MyNotes({ round, viewerName }: { round: RoundPayload; viewerName: string }) {
  const mine = mineOf(round.notes, viewerName)
  if (mine.length === 0) return null
  return (
    <section aria-label="Your notes" className="mt-10 flex flex-col gap-3">
      <h2 className="text-lg font-semibold text-foreground">What happened to your notes from round {round.number}</h2>
      <ul className="flex flex-col gap-2">
        {mine.map((n) => (
          <li key={n.id} className="rounded-md border border-border bg-card p-3 text-sm">
            <p className="text-foreground">{n.body || n.suggested_text}</p>
            <p className="mt-1 font-medium text-foreground">{outcome(n)}</p>
            {n.disposition_note && <p className="text-muted-foreground">{n.disposition_note}</p>}
          </li>
        ))}
      </ul>
    </section>
  )
}
```

`DecisionLog.tsx` changes:
- In `DecisionRow`, under the value line, render each `d.pending` entry as `<p className="text-sm text-muted-foreground">Pending: {e.by_name} → {e.action === 'confirm' ? d.recommended : e.value}</p>`.
- `DecisionLog` gains props `inRound: boolean` and `capability`; a row is editable when `d.editable && (canEdit || (inRound && capability === 'suggest'))`. A guest's edit sends `author_name: readReviewerName()` (export that function from `NoteComposer.tsx`); if empty, show "Add your name in a note first" instead of the buttons.
- Under each row, when `capability !== 'read'`, render the existing `NoteComposer` with `anchorLabel={d.plain_question || d.question}` and `defaults={{ anchor_id: \`decision:${d.id}\` }}` (use its real prop names — read the component's props block first).

`StoryboardPage.tsx`:
- Above the brief: `<RoundBanner board={board} canManage={board.is_member && <editor flag>} ws={workspace} onChanged={reload} />`. The page has no editor flag today; add `can_manage: bool` to `StoryboardOut` (server: `perms.can(request.user, board.workspace_id, perms.CONTENT_WRITE)` in `get_storyboard`) in this task, with a backend test asserting `false` for a viewer and a token holder.
- Notes source: when `board.round` is set, group `board.round.notes` by `anchor_id` and feed the existing `NotesReturned` placements for every reader; otherwise keep today's member-only `listNotes` path.
- After the decision log: `{board.previous_round && <MyNotes round={board.previous_round} viewerName={readReviewerName()} />}`.
- `reload` = re-run the existing `getStoryboard` effect (lift it into a callback).

Docs: in `docs/architecture/pages.md`'s storyboard entry add: "When a feedback round is open, every reader sees every round note (names, never emails) and a banner with the counts; editors open and close rounds. Closing hands the round to the board project's agent as one task (one turn). Decision edits during a round show as pending until close. After the agent consumes a round, each person sees what happened to their notes."

- [ ] **Step 4: Run frontend checks**

Run: `cd frontend && npx vitest run src/components/storyboard src/pages/StoryboardPage.test.tsx src/pages/StoryboardDemoSet.test.tsx && npm run build`
Expected: PASS, clean build. Also `uv run pytest tests/test_storyboard_rounds.py -k can_manage -v` → PASS, then `npm run gen:api:local` for the `can_manage` field.

- [ ] **Step 5: Commit**

```bash
git add frontend/src apps/storyboards docs/architecture/pages.md tests/
git commit -m "feat(storyboards): round banner, everyone's notes, pending edits, and my notes"
```

---

### Task 9: Ship canopy-web

- [ ] `uv run pytest` → all pass. `cd frontend && npm test && npm run build` → pass. `uv run python manage.py makemigrations --check --dry-run` → no changes.
- [ ] Push, open the PR ("Storyboards: feedback rounds — many comment, one close starts one run"), arm auto-merge, wait with `gh pr checks --watch` (no sleep loops), confirm merged.
- [ ] Deploy needs the owner's approval in a manual-mode session. After deploy, on the RUTF board (`connect-supply-rutf`): link `agent_project` to ACE's Connect Supply project, open round 1, leave one test note as a member and one as a token holder, confirm both appear for a token-holding reader, then delete only the test notes you created (or leave them and say so).

---

## Plugin side

### Task 10 (canopy): publish a demo set with decisions, and consume a round

Repo: `/Users/haldimagi/emdash/repositories/canopy` (fresh worktree off `origin/main`).

**Files:**
- Create: `scripts/ddd/demo_set.py`, `scripts/ddd/feedback_round.py`, `tests/ddd/test_demo_set.py`, `tests/ddd/test_feedback_round.py`, `plugins/canopy/skills/ddd-feedback-round/SKILL.md`
- Modify: `plugins/canopy/agents/ddd.md` (a branch: a turn that carries a round runs `ddd-feedback-round` instead of polling `response_json`)

**Interfaces:**
- Consumes: canopy-web routes from Tasks 2–6 and plan B (`POST /api/storyboards/`, `PATCH /api/storyboards/{slug}`, `GET …/rounds/{n}`, `POST …/rounds/{n}/consume`, `POST /api/feedback/{id}/resolve`); `scripts.ddd.auth` (`resolve_base_url`, `resolve_token`, `resolve_ddd_workspace`, `scoped_api_path`); `scripts.ddd.review._json_request`; `scripts.ddd.run_store.pull/push`.
- Produces:
  - `demo_set.publish(board: dict, *, workspace=None, base_url=None, token=None) -> dict` (create, or PATCH on 409 slug-exists); CLI `python -m scripts.ddd.demo_set publish <board.json>`.
  - `feedback_round.read(slug, number, *, workspace=None, base_url=None, token=None) -> dict`
  - `feedback_round.cluster(notes: list[dict]) -> dict[str, list[dict]]` keyed `"<target_ref>#<anchor_id or '*'>"`.
  - `feedback_round.missing_dispositions(round_doc: dict, dispositions: dict[str, dict]) -> list[int]`
  - `feedback_round.apply(slug, number, dispositions, *, version, run_id, workspace=None, base_url=None, token=None) -> dict` — validates, resolves every note, records on the run doc, consumes; raises `UnansweredNotes` before any write when a note lacks a disposition.
  - CLI: `python -m scripts.ddd.feedback_round read <slug> <n> [--out f]`, `… apply <slug> <n> --dispositions f.json --version N --run-id R`.

- [ ] **Step 1: Failing tests** — `tests/ddd/test_feedback_round.py`:

```python
"""scripts/ddd/feedback_round.py — HTTP mocked, no network."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.ddd import feedback_round as fr

ROUND = {"number": 1, "state": "closed", "notes": [
    {"id": 11, "target_ref": "supply-rutf", "anchor_id": "cut:buying", "body": "Too fast"},
    {"id": 12, "target_ref": "supply-rutf", "anchor_id": "cut:buying", "body": "Name the suppliers"},
    {"id": 13, "target_ref": "supply-rutf", "anchor_id": "", "body": "Lead with the forecast"},
], "decisions": [], "staged_edits": []}


def test_cluster_groups_by_narrative_and_anchor():
    c = fr.cluster(ROUND["notes"])
    assert sorted(c) == ["supply-rutf#*", "supply-rutf#cut:buying"]
    assert [n["id"] for n in c["supply-rutf#cut:buying"]] == [11, 12]


def test_missing_dispositions_names_every_unanswered_note():
    d = {"11": {"state": "answered", "note": "Slowed it"}}
    assert fr.missing_dispositions(ROUND, d) == [12, 13]


def test_a_disposition_needs_a_state_and_words():
    with pytest.raises(fr.UnansweredNotes, match="13"):
        fr.missing_or_raise(ROUND, {"11": {"state": "answered", "note": "x"},
                                    "12": {"state": "declined", "note": "y"},
                                    "13": {"state": "answered", "note": ""}})


def test_apply_writes_nothing_when_a_note_is_unanswered():
    calls = []
    with patch.object(fr, "read", return_value=ROUND), patch.object(fr, "_request",
                                                                     side_effect=lambda *a, **k: calls.append(a)):
        with pytest.raises(fr.UnansweredNotes):
            fr.apply("supply", 1, {"11": {"state": "answered", "note": "x"}}, version=4, run_id="r1")
    assert calls == []


def test_apply_resolves_records_and_consumes_in_order():
    calls = []
    disp = {str(n["id"]): {"state": "answered", "note": "done"} for n in ROUND["notes"]}
    with patch.object(fr, "read", return_value=ROUND), \
         patch.object(fr, "_request",
                      side_effect=lambda m, p, b=None, **k: calls.append((m, p, b)) or {"number": 2}), \
         patch.object(fr, "_record_on_run_doc") as rec:
        out = fr.apply("supply", 1, disp, version=4, run_id="r1")
    assert [c[1] for c in calls[:3]] == ["/api/feedback/11/resolve", "/api/feedback/12/resolve",
                                         "/api/feedback/13/resolve"]
    assert calls[0][2] == {"state": "answered", "note": "done", "resolved_in_version": 4}
    assert calls[-1][1] == "/api/storyboards/supply/rounds/1/consume"
    rec.assert_called_once_with("r1", slug="supply", number=1, feedback_ids=[11, 12, 13])
    assert out["next_round"] == 2
```

`tests/ddd/test_demo_set.py`:

```python
from unittest.mock import patch
import urllib.error

from scripts.ddd import demo_set

BOARD = {"slug": "connect-supply-rutf", "title": "Connect Supply", "decisions": [
    {"id": "banner", "question": "Rename?", "options": ["Rename", "Keep"], "recommended": "Rename"}]}


def test_publish_creates_then_patches_when_slug_exists():
    seen = []

    def fake(method, path, body=None):
        seen.append((method, path))
        if method == "POST":
            raise urllib.error.HTTPError(path, 409, "exists", {}, None)
        return {"slug": "connect-supply-rutf", "decisions": body["decisions"]}

    with patch.object(demo_set, "_request", side_effect=fake):
        out = demo_set.publish(BOARD)
    assert seen == [("POST", "/api/storyboards/"), ("PATCH", "/api/storyboards/connect-supply-rutf")]
    assert out["decisions"][0]["id"] == "banner"


def test_publish_refuses_legacy_brief_lists():
    import pytest

    with pytest.raises(ValueError, match="decisions"):
        demo_set.publish({**BOARD, "brief": {"assumptions": ["x"]}})
```

Before implementing, check what `create_storyboard` returns on a duplicate slug (an `IntegrityError` may surface as 500 rather than 409). If it is not 409, add a canopy-web test + fix in the PR for Task 2 (`create_storyboard` → 409 on `uniq_storyboard_workspace_slug`), and keep this client logic.

- [ ] **Step 2: Run** — `uv run pytest tests/ddd/test_feedback_round.py tests/ddd/test_demo_set.py -v` → FAIL (modules missing).

- [ ] **Step 3: Implement** `scripts/ddd/feedback_round.py`:

```python
"""Consume a closed storyboard feedback round (canopy-web spec C, step 2-5).

    python -m scripts.ddd.feedback_round read  <slug> <n> [--out round.json]
    python -m scripts.ddd.feedback_round apply <slug> <n> --dispositions d.json --version N --run-id R

`apply` refuses — before writing anything — unless EVERY note in the round has
a disposition with a state (answered|declined) and words. That is the
"every comment gets an answer" rule, enforced rather than hoped for.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.ddd.auth import (
    resolve_base_url as _resolve_base_url,
    resolve_ddd_workspace as _resolve_ws,
    resolve_token as _resolve_token,
    scoped_api_path as _scoped,
)
from scripts.ddd.review import _json_request

STATES = ("answered", "declined")


class UnansweredNotes(ValueError):
    pass


def _request(method: str, path: str, body: dict | None = None, *, workspace=None, base_url=None, token=None):
    url = f"{_resolve_base_url(base_url)}{_scoped(path, _resolve_ws(workspace))}"
    return _json_request(method, url, _resolve_token(token), body)


def read(slug: str, number: int, **kw) -> dict:
    return _request("GET", f"/api/storyboards/{slug}/rounds/{number}", **kw)


def cluster(notes: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for n in notes:
        out.setdefault(f"{n.get('target_ref', '')}#{n.get('anchor_id') or '*'}", []).append(n)
    return dict(sorted(out.items()))


def missing_dispositions(round_doc: dict, dispositions: dict[str, dict]) -> list[int]:
    missing = []
    for n in round_doc.get("notes") or []:
        d = dispositions.get(str(n["id"])) or {}
        if d.get("state") not in STATES or not (d.get("note") or "").strip():
            missing.append(n["id"])
    return missing


def missing_or_raise(round_doc: dict, dispositions: dict[str, dict]) -> None:
    missing = missing_dispositions(round_doc, dispositions)
    if missing:
        raise UnansweredNotes("every note needs a disposition (answered|declined) and a sentence; "
                              f"missing: {', '.join(map(str, missing))}")


def _record_on_run_doc(run_id: str, *, slug: str, number: int, feedback_ids: list[int]) -> None:
    from scripts.ddd import run_store

    doc = run_store.pull(run_id)
    if doc is None:
        return
    state = dict(doc.get("state") or {})
    state.setdefault("run_id", run_id)
    state.setdefault("feedback_rounds", []).append(
        {"storyboard": slug, "round": number, "feedback_ids": feedback_ids})
    run_store.push(state, base_version=doc.get("state_version"))


def apply(slug: str, number: int, dispositions: dict[str, dict], *, version: int, run_id: str, **kw) -> dict:
    round_doc = read(slug, number, **kw)
    missing_or_raise(round_doc, dispositions)
    ids = [n["id"] for n in round_doc["notes"]]
    for nid in ids:
        d = dispositions[str(nid)]
        body = {"state": d["state"], "note": d["note"].strip()}
        if d["state"] == "answered":
            body["resolved_in_version"] = version
        _request("POST", f"/api/feedback/{nid}/resolve", body, **kw)
    _record_on_run_doc(run_id, slug=slug, number=number, feedback_ids=ids)
    nxt = _request("POST", f"/api/storyboards/{slug}/rounds/{number}/consume", {"run_id": run_id}, **kw)
    return {"resolved": len(ids), "next_round": nxt.get("number")}


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="feedback_round")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("read")
    r.add_argument("slug"); r.add_argument("number", type=int); r.add_argument("--out")
    a = sub.add_parser("apply")
    a.add_argument("slug"); a.add_argument("number", type=int)
    a.add_argument("--dispositions", required=True); a.add_argument("--version", type=int, required=True)
    a.add_argument("--run-id", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "read":
        doc = read(args.slug, args.number)
        doc["clusters"] = cluster(doc.get("notes") or [])
        text = json.dumps(doc, indent=2)
        Path(args.out).write_text(text) if args.out else print(text)
        return 0
    try:
        out = apply(args.slug, args.number, json.loads(Path(args.dispositions).read_text()),
                    version=args.version, run_id=args.run_id)
    except UnansweredNotes as e:
        print(f"feedback_round: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
```

Keep `_request`'s positional order `(method, path, body)`; the tests' fakes accept `**k` because `apply` forwards the workspace/base-url/token keywords to every call.

`scripts/ddd/demo_set.py`:

```python
"""Publish a set of demos as one storyboard, with decision rows (canopy-web spec B).

    python -m scripts.ddd.demo_set publish <board.json>

`board.json` is the StoryboardIn shape: slug, title, lede, brief (audience,
purpose, data_disclosure, short_version), decisions[], acts[], capability.
The brief's old assumptions/divergences/open_questions lists are refused here:
say them as decisions[].
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path

from scripts.ddd.feedback_round import _request

_LEGACY = ("assumptions", "divergences", "open_questions")


def publish(board: dict, **kw) -> dict:
    legacy = [k for k in _LEGACY if (board.get("brief") or {}).get(k)]
    if legacy:
        raise ValueError(f"brief.{', brief.'.join(legacy)}: send these as decisions[] rows instead")
    try:
        return _request("POST", "/api/storyboards/", board, **kw)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            raise
    patch = {k: v for k, v in board.items() if k != "slug"}
    return _request("PATCH", f"/api/storyboards/{board['slug']}", patch, **kw)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="demo_set")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish"); p.add_argument("path")
    args = ap.parse_args(argv)
    out = publish(json.loads(Path(args.path).read_text()))
    print(json.dumps({"slug": out.get("slug"), "decisions": len(out.get("decisions") or [])}))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
```

`plugins/canopy/skills/ddd-feedback-round/SKILL.md` — frontmatter `name: ddd-feedback-round`, description "Consume a closed storyboard feedback round: read every note, cluster, decide each note's disposition, draft and publish vN+1, then apply + consume (which opens the next round). Use when a turn's task says 'Run ddd-feedback-round for storyboard <slug> round <n>'." Body, in order: (1) `python -m scripts.ddd.feedback_round read <slug> <n> --out round.json`; (2) read `clusters` and `decisions` — rulings with status `human-decided`/`overridden` are BINDING; (3) for each cluster decide: change the narrative (answered, with the scene that changed) or decline (with the reason a reviewer can accept); write `dispositions.json` `{ "<note id>": {"state", "note"} }` — every note, no exceptions; (4) draft vN+1 through the normal `ddd-narrative-review`/render/upload path, re-sending decision rows with the ruled values (a 409 means you tried to move a ruling — supersede instead and say why); (5) `python -m scripts.ddd.feedback_round apply <slug> <n> --dispositions dispositions.json --version <vN+1> --run-id <run>`; it refuses before writing anything if a note lacks an answer; (6) report the next round's number and the board link.

`plugins/canopy/agents/ddd.md` — in the section that describes waiting on a review (`await_resolution` / `response_json`), add: "**A turn raised from a storyboard feedback-round task** (its next action says `Run ddd-feedback-round …`) does not poll a review. Run the `ddd-feedback-round` skill; it is the human input for this iteration."

- [ ] **Step 4: Run** — `uv run pytest tests/ddd/test_feedback_round.py tests/ddd/test_demo_set.py -v` → PASS; then `uv run pytest tests/ddd -q` → PASS; `canopy version bump`; `canopy version verify-bump` → green.

- [ ] **Step 5: Commit and ship** — commit, push, PR, auto-merge per canopy's CLAUDE.md (`check-version` must be green), confirm merged.

### Task 11 (ACE): publish decision rows; read rounds

Repo: `/Users/haldimagi/emdash/worktrees/ace-3d026776/emdash-c-we-have-a-lot-of-demos-we-are-58ac-g1nho` (branch off `origin/main`).

**Files:**
- Modify: `skills/demo-narrative/SKILL.md`, `skills/narrative-iteration-review/SKILL.md`
- Create: `test/skills/demo-set-decisions.test.ts`

**Interfaces:**
- Consumes: `scripts.ddd.demo_set publish`, `scripts.ddd.feedback_round read|apply` (Task 10), via `bin/ace-canopy-web --workspace <ws> -- <command>`.

- [ ] **Step 1: Failing test** — `test/skills/demo-set-decisions.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const read = (p: string) => readFileSync(new URL(`../../${p}`, import.meta.url), "utf8");

describe("demo sets publish decision rows and read feedback rounds", () => {
  const demo = read("skills/demo-narrative/SKILL.md");
  const iterate = read("skills/narrative-iteration-review/SKILL.md");

  it("demo-narrative publishes the set with decisions[], through ace-canopy-web", () => {
    expect(demo).toMatch(/scripts\.ddd\.demo_set publish/);
    expect(demo).toMatch(/bin\/ace-canopy-web --workspace <ws> --/);
    expect(demo).toMatch(/decisions\[\]/);
  });

  it("demo-narrative no longer tells the publisher to send brief.assumptions", () => {
    expect(demo).not.toMatch(/brief\.(assumptions|divergences|open_questions)/);
  });

  it("narrative-iteration-review reads feedback rounds as well as review responses", () => {
    expect(iterate).toMatch(/ddd-feedback-round/);
    expect(iterate).toMatch(/scripts\.ddd\.feedback_round/);
  });
});
```

- [ ] **Step 2: Run** — `npx vitest run test/skills/demo-set-decisions.test.ts` → FAIL.

- [ ] **Step 3: Edit the skills**
  - `skills/demo-narrative/SKILL.md`: add a "Publishing a set of demos" step after upload. Write `board.json` (StoryboardIn: slug, title, lede, `brief` = audience/purpose/data_disclosure/short_version only, `decisions[]`, `acts[]`, `capability: "comment"` or `"suggest"`, `agent_project` = the ACE project for the work). One `decisions[]` row per default the set is built on, in plain language: what a reviewer must answer before sharing externally → `review_ask: required-before`, `needed_by: external-share`; what to confirm → `recommended-confirmation`; script-vs-data disagreements → `evidence_basis: conflicting` with both readings in `conflict_signals`. Publish: `(cd "$CANOPY_RT" && "$ACE_ROOT/bin/ace-canopy-web" --workspace <ws> -- uv run python -m scripts.ddd.demo_set publish <path>/board.json)`, then `bin/ace-canopy-web --workspace <ws> verify` the board before citing its link. Publishing or sharing the board follows the turn mode's approval rule.
  - `skills/narrative-iteration-review/SKILL.md`: add "Feedback from a storyboard round" next to the review-response path: when the turn's task says `Run ddd-feedback-round for storyboard <slug> round <n>`, follow canopy's `ddd-feedback-round` skill (`scripts.ddd.feedback_round read`/`apply` through `bin/ace-canopy-web`); every note gets a disposition; stamp each change with `Feedback-Ref: storyboard:<slug>/round-<n>/<note id>` per the feedback-ledger rule.

- [ ] **Step 4: Run** — `npx vitest run test/skills/demo-set-decisions.test.ts` and `npm test` → PASS.

- [ ] **Step 5: Ship** — `bash scripts/version-bump.sh`, commit, push, `gh pr create`, `gh pr merge <n> --auto --merge`, confirm merged, `/ace:update`.

### Task 12: Pilot on the RUTF board (after both deploys)

- [ ] Re-publish `connect-supply-rutf` with `scripts.ddd.demo_set` carrying the RUTF decisions (banner rename — required before external share; SQ-LNS; "at scale" on one programme; marketplace not filmed; the two run-out figures — conflicting; plus the four "make it real" asks), linked to ACE's Connect Supply project. Owner approval first (manual mode: publishing/sharing).
- [ ] Open round 1 for Lily's team before the 12 November Edesia demo; send the link only with the owner's approval.

---

## Self-review notes

- Spec C coverage: object + states + pinning (1–2), who may comment and anchors incl. `decision:`/`product:` (2), visibility reversal inside a round only (3), staged edits (4), close = one transaction, one task, one turn (5), DDD step reading the round with every-note-answered enforced (Task 10, plus server-side 409 in 6), consume + auto-open next round (6, owner decision 1), review suggestions in the pool (7), "what happened to my comment" (3 + 8), project alignment via `ProjectParticipant.REVIEW` + `AgentTask` + run doc (2, 5, 10), ACE rollout (11), pilot (12).
- Owner decision 2 (member edit outside a round applies immediately) is plan B's behaviour, preserved by Task 4's branch; decision 3 (`needed_by` vocabulary) is pinned in plan B's drift test.
- Tier check: `apps/feedback/rounds.py` imports only `apps.feedback`; every storyboard rule is in `apps/storyboards/rounds.py`.
