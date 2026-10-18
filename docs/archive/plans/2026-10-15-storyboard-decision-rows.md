# Storyboard decision rows — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace a storyboard's free-text assumptions / divergences / open questions with decision rows (recommended option, state, who decided, history) that people confirm, change or comment on, in ace-web's pattern.

**Architecture:** A new `StoryboardDecision` table in the product app `apps/storyboards`, one row per decision, with a request-free service module (`apps/storyboards/decisions.py`) that owns validation, the publish upsert (which refuses to overwrite a human ruling), and person edits with history and undo. The board read (`GET /api/storyboards/{slug}`) gains `decisions[]`; the publish paths (`POST /`, `PATCH /{slug}`) accept `decisions[]` and convert the old brief lists for one release; a new self-enforcing route `POST /{slug}/decisions/{key}/edit` applies a member's edit immediately. The frontend replaces the `Caveats` section with a grouped decision log that uses one row component for every state.

**Tech Stack:** Django 5 + Django Ninja + Pydantic v2 (backend), PostgreSQL, pytest (`uv run pytest`), React 19 + Vite + Tailwind 4 + vitest (frontend), `openapi-fetch` types generated into `frontend/src/api/generated.ts`.

**Spec:** `docs/superpowers/specs/2026-10-15-storyboard-decisions-and-feedback-rounds-design.md` (section B and "Owner decisions"). Executors read both.

## Global Constraints

- Field vocabularies, copied verbatim from the spec: `status` ∈ `ai-default` / `human-decided` / `overridden` / `deferred`; `review_ask` ∈ (absent) / `recommended-confirmation` / `required-before`; `needed_by` ∈ `external-share` / `next-run`; `evidence_basis` ∈ `stated` / `inferred` / `conflicting`.
- `recommended` must be one of `options` when `options` is non-empty. Option labels are 1–8 words.
- `value` is derived, never stored: `override` if set, else `recommended`.
- `decided_by`, `decided_by_verified`, `decided_at` are set by the server from the signed-in user, never by the caller.
- A publish may not change the effective value of, or drop, a row whose `status` is `human-decided` / `overridden` or that is `confirmed` → 409. Changing a ruling means a new row with `supersedes`.
- Outside a round, a **member's** edit applies immediately, attributed and undoable (owner decision 2026-10-15). A non-member's edit is refused in this plan (403); plan C turns it into staging inside an open round.
- The brief keeps `audience`, `purpose`, `data_disclosure`, `short_version`. Its `assumptions` / `divergences` / `open_questions` are still accepted for one release and converted to rows on write; the stored brief never holds them after this plan.
- `storyboards` is PRODUCT tier; it may import framework apps. Nothing framework-side may import it (`tests/test_architecture_boundary.py`).
- Every new route declares its gate in `apps/api/route_gates.py` (`GATES`, plus `NOT_SETTINGS`, plus `VIEWER_MAY_MUTATE` for a member-tier write) — `tests/test_every_route_declares_its_gate.py`.
- Changing `apps/**/schemas.py` or `api.py` (including a route docstring) ⇒ regenerate and commit `frontend/src/api/generated.ts` (`cd frontend && npm run gen:api:local`). Maintainer rationale goes in `#` comments, not docstrings.
- Frontend: semantic design tokens only — no raw Tailwind palette literals, no `dark:` variants (`docs/architecture/frontend.md`).
- Plain-language rule for anything a reviewer reads: no ids, `snake_case`, or "Phase N" in rendered copy.

## Review Focus

1. **Two people confirm/change the same row at once.** Expect: both edits land in order, the second one's history holds the first; no lost update. Test: `test_concurrent_edits_both_recorded_in_history` (Task 2) — uses `select_for_update` inside `apply_edit`.
2. **The publisher re-sends a board after a person ruled on a row, with the AI's old recommendation.** Expect: 409 naming the row, nothing on the board changes (whole publish rolls back). Test: `test_publish_cannot_reset_a_ruling_and_rolls_back` (Task 2).
3. **A board created before this plan (brief lists, no rows) is read and then patched.** Expect: the migration turned its lists into rows once; a later PATCH with only `title` leaves the rows alone. Test: `test_legacy_brief_migrates_once_and_patch_title_keeps_rows` (Task 3).
4. **Undo with nothing to undo, or undo after the publisher superseded the row.** Expect: 409 "nothing to undo", never a 500 or a silent no-op. Test: `test_undo_with_empty_history_is_409` (Task 2).
5. **A write-in that already exists among the options (different case/whitespace).** Expect: no duplicate option; treated as picking the existing one. Test: `test_write_in_matching_existing_option_does_not_duplicate` (Task 2).

---

## File Structure

| File | Responsibility |
|---|---|
| `apps/storyboards/models.py` (modify) | `StoryboardDecision` model. |
| `apps/storyboards/migrations/0007_storyboarddecision.py` (generated) | Table. |
| `apps/storyboards/migrations/0008_brief_lists_to_decisions.py` (create) | Data migration: brief lists → rows; strip lists from the brief. |
| `apps/storyboards/decisions.py` (create) | Vocabularies, validation, legacy conversion, publish upsert, person edits, undo, serialization. Request-free. |
| `apps/storyboards/schemas.py` (modify) | `DecisionIn`, `DecisionOut`, `DecisionEditIn`; `decisions` on in/out models. |
| `apps/storyboards/services.py` (modify) | `resolve_board` emits `decisions[]` (stored rows + read-time narrative caveat rows). |
| `apps/storyboards/api.py` (modify) | Publish paths write rows; new `edit_storyboard_decision` route. |
| `apps/api/route_gates.py` (modify) | Gate + classification for the new route. |
| `frontend/src/api/storyboards.ts` (modify) | `Decision` type, `editDecision()`. |
| `frontend/src/components/storyboard/DecisionLog.tsx` (create) | Grouping + `DecisionRow`. |
| `frontend/src/components/storyboard/DecisionLog.test.tsx` (create) | Grouping/order, chips, actions. |
| `frontend/src/pages/StoryboardPage.tsx` (modify) | Render `DecisionLog` in place of `Caveats`. |
| `tests/test_storyboard_decisions.py` (create) | Model/service/API tests. |
| `tests/test_decision_vocabulary_matches_ace.py` (create) | Drift pin against ACE `lib/decisions-schema.ts`. |
| `docs/architecture/pages.md` (modify) | Storyboard page entry mentions the decision log. |

---

### Task 1: The `StoryboardDecision` model and the vocabulary pin

**Files:**
- Modify: `apps/storyboards/models.py` (append after `class Entry`)
- Create: `apps/storyboards/decisions.py` (vocabulary constants only in this task)
- Generate: `apps/storyboards/migrations/0007_storyboarddecision.py`
- Test: `tests/test_storyboard_decisions.py`, `tests/test_decision_vocabulary_matches_ace.py`

**Interfaces:**
- Produces: `StoryboardDecision` (fields below), `decisions.STATUSES`, `decisions.REVIEW_ASKS`, `decisions.NEEDED_BY`, `decisions.EVIDENCE_BASES`, `decisions.HUMAN_STATUSES`.

- [ ] **Step 1: Write the failing tests**

`tests/test_decision_vocabulary_matches_ace.py`:

```python
"""canopy-web's decision vocabularies are ACE's (one contract, two stores).

The values below are COPIED from ACE `lib/decisions-schema.ts` (schema v6):
`status` enum in `DecisionRowSchema` (line ~228), `REVIEW_ASKS` (line 116), and
the `evidence_basis` enum (line ~260). They are pinned here rather than read
from the other repo, which CI cannot see. If ACE changes a value, change it
here AND in apps/storyboards/decisions.py in the same PR — that is the point.

`needed_by` is deliberately NOT ACE's (`award`/`go-live`/…): a set of demos has
its own two moments (owner decision 2026-10-15).
"""
from apps.storyboards import decisions

ACE_STATUS = ("ai-default", "human-decided", "overridden", "deferred")
ACE_REVIEW_ASKS = ("recommended-confirmation", "required-before")
ACE_EVIDENCE_BASIS = ("stated", "inferred", "conflicting")


def test_status_matches_ace():
    assert decisions.STATUSES == ACE_STATUS


def test_review_asks_match_ace():
    assert decisions.REVIEW_ASKS == ACE_REVIEW_ASKS


def test_evidence_basis_matches_ace():
    assert decisions.EVIDENCE_BASES == ACE_EVIDENCE_BASIS


def test_needed_by_is_the_demo_set_vocabulary():
    assert decisions.NEEDED_BY == ("external-share", "next-run")
```

`tests/test_storyboard_decisions.py` (first block — later tasks append to it):

```python
"""Decision rows on a storyboard: the ace-web pattern, in Postgres."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError

from apps.storyboards.models import Storyboard, StoryboardDecision
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("jj", "jj@dimagi.com", "pw", first_name="Jonathan")


@pytest.fixture()
def ws(owner):
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(workspace=ws, user=owner, role="owner")
    return ws


@pytest.fixture()
def board(ws):
    return Storyboard.objects.create(slug="supply", title="Connect Supply", workspace=ws,
                                     capability=Storyboard.CAP_COMMENT)


def test_decision_key_is_unique_per_board(board):
    StoryboardDecision.objects.create(storyboard=board, key="banner", question="Rename?")
    with pytest.raises(IntegrityError):
        StoryboardDecision.objects.create(storyboard=board, key="banner", question="Again?")


def test_decision_defaults(board):
    d = StoryboardDecision.objects.create(storyboard=board, key="sqlns", question="Show SQ-LNS?")
    assert d.status == "ai-default"
    assert d.options == [] and d.history == [] and d.conflict_signals == []
    assert d.confirmed is False and d.override == "" and d.scope == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_decision_vocabulary_matches_ace.py tests/test_storyboard_decisions.py -v`
Expected: FAIL — `ImportError: cannot import name 'decisions'` / `cannot import name 'StoryboardDecision'`.

- [ ] **Step 3: Write the vocabulary module and the model**

`apps/storyboards/decisions.py`:

```python
"""Decision rows on a storyboard — ace-web's decision-log pattern.

Every default a set of demos is built on is ONE row. Whether it reads as an
assumption, an open ask or a ruling follows from `status` and `review_ask`,
exactly as in ACE `docs/decisions-contract.md` and ace-web's DecisionsReview.

Request-free on purpose: the publish routes, the edit route, the round close
(plan C) and the data migration all call this module, so they cannot drift.
"""
from __future__ import annotations

STATUSES = ("ai-default", "human-decided", "overridden", "deferred")
REVIEW_ASKS = ("recommended-confirmation", "required-before")
NEEDED_BY = ("external-share", "next-run")
EVIDENCE_BASES = ("stated", "inferred", "conflicting")

#: A person ruled on these; a publish may not move their value.
HUMAN_STATUSES = frozenset({"human-decided", "overridden"})
```

Append to `apps/storyboards/models.py` (after `class Entry`):

```python
class StoryboardDecision(models.Model):
    """One default the set is built on (spec B). Assumption, open ask and ruling
    are the same row in different states — never three tables.

    A row, not a key in ``Storyboard.brief``: several reviewers edit rows at
    once during a round, and a JSON blob would make every confirm a
    read-modify-write of the whole brief (a lost update is the expected case).
    """

    storyboard = models.ForeignKey(Storyboard, on_delete=models.CASCADE, related_name="decisions")
    key = models.SlugField(max_length=120)
    """The row's stable id — the API calls it ``id``. Reused across versions; it
    is how a ruling binds the next run."""
    scope = models.CharField(max_length=200, blank=True, default="")
    """Blank = the whole set; else the ``narrative_slug`` it is about."""
    position = models.IntegerField(default=0)

    question = models.TextField()
    plain_question = models.TextField(blank=True, default="")
    options = models.JSONField(default=list, blank=True)
    recommended = models.CharField(max_length=300, blank=True, default="")
    override = models.CharField(max_length=300, blank=True, default="")
    reasoning = models.TextField(blank=True, default="")
    override_reasoning = models.TextField(blank=True, default="")
    source = models.TextField(blank=True, default="")
    evidence_basis = models.CharField(max_length=16, default="inferred")
    conflict_signals = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=16, default="ai-default")
    revisit_when = models.TextField(blank=True, default="")
    review_ask = models.CharField(max_length=32, blank=True, default="")
    needed_by = models.CharField(max_length=32, blank=True, default="")
    confirm_reason = models.TextField(blank=True, default="")

    decided_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    decided_by_name = models.CharField(max_length=200, blank=True, default="")
    decided_by_verified = models.BooleanField(default=False)
    decided_at = models.DateTimeField(null=True, blank=True)
    confirmed = models.BooleanField(default=False)
    history = models.JSONField(default=list, blank=True)
    """Newest-first snapshots of the person-owned fields before each edit."""

    supersedes = models.CharField(max_length=120, blank=True, default="")
    superseded_by = models.CharField(max_length=120, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["storyboard", "key"], name="uniq_decision_storyboard_key"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"decision:{self.storyboard_id}:{self.key}"
```

- [ ] **Step 4: Generate the migration**

Run: `uv run python manage.py makemigrations storyboards -n storyboarddecision`
Expected: `apps/storyboards/migrations/0007_storyboarddecision.py` created with `CreateModel(name='StoryboardDecision', …)` and the unique constraint.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_decision_vocabulary_matches_ace.py tests/test_storyboard_decisions.py -v`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/storyboards/models.py apps/storyboards/decisions.py apps/storyboards/migrations/0007_storyboarddecision.py tests/test_storyboard_decisions.py tests/test_decision_vocabulary_matches_ace.py
git commit -m "feat(storyboards): StoryboardDecision rows with ACE's decision vocabulary"
```

---

### Task 2: The decision service — validate, publish upsert, edits, undo, serialize

**Files:**
- Modify: `apps/storyboards/decisions.py`
- Test: `tests/test_storyboard_decisions.py` (append)

**Interfaces:**
- Consumes: `StoryboardDecision`, vocabularies (Task 1).
- Produces:
  - `class DecisionInvalid(ValueError)` → the API maps to 422.
  - `class DecisionConflict(Exception)` → the API maps to 409.
  - `value_of(row: StoryboardDecision) -> str`
  - `validate(raw: dict) -> dict` (normalized row dict; raises `DecisionInvalid`)
  - `publish(board: Storyboard, rows: list[dict]) -> list[StoryboardDecision]` (replaces the board's set; atomic; raises `DecisionConflict`)
  - `apply_edit(row: StoryboardDecision, *, action: str, value: str = "", reasoning: str = "", by_user=None, by_name: str = "") -> StoryboardDecision` — `action` ∈ `confirm` / `pick` / `write_in` / `undo`.
  - `serialize(row: StoryboardDecision, *, editable: bool = True) -> dict`
  - `EDIT_ACTIONS = ("confirm", "pick", "write_in", "undo")`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_storyboard_decisions.py`)

```python
from apps.storyboards import decisions as dsvc


def _row(**over):
    base = {
        "id": "banner",
        "question": "Rename the demo programme banner before sharing?",
        "plain_question": "Should the banner read “RUTF programme (demo)” before Edesia sees it?",
        "options": ["Rename and re-record", "Keep as is"],
        "recommended": "Rename and re-record",
        "reasoning": "The internal name is in every cut.",
        "evidence_basis": "stated",
        "review_ask": "required-before",
        "needed_by": "external-share",
    }
    base.update(over)
    return base


def test_validate_rejects_recommended_outside_options():
    with pytest.raises(dsvc.DecisionInvalid, match="recommended"):
        dsvc.validate(_row(recommended="Something else"))


def test_validate_rejects_unknown_vocabulary():
    with pytest.raises(dsvc.DecisionInvalid, match="review_ask"):
        dsvc.validate(_row(review_ask="before-launch"))
    with pytest.raises(dsvc.DecisionInvalid, match="needed_by"):
        dsvc.validate(_row(needed_by="go-live"))


def test_validate_requires_needed_by_for_required_before():
    with pytest.raises(dsvc.DecisionInvalid, match="needed_by"):
        dsvc.validate(_row(needed_by=""))


def test_validate_rejects_long_option():
    with pytest.raises(dsvc.DecisionInvalid, match="8 words"):
        dsvc.validate(_row(options=["one two three four five six seven eight nine"],
                           recommended="one two three four five six seven eight nine"))


def test_validate_refuses_caller_supplied_decider():
    with pytest.raises(dsvc.DecisionInvalid, match="decided_by"):
        dsvc.validate(_row(decided_by="mallory"))


def test_publish_creates_rows_in_order(board):
    dsvc.publish(board, [_row(), _row(id="sqlns", question="Show SQ-LNS?", options=["RUTF only"],
                                      recommended="RUTF only", review_ask="", needed_by="")])
    keys = list(board.decisions.values_list("key", flat=True))
    assert keys == ["banner", "sqlns"]
    assert dsvc.value_of(board.decisions.get(key="banner")) == "Rename and re-record"


def test_member_pick_overrides_and_records_history(board, owner):
    (row, *_rest) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Keep as is", reasoning="Edesia knows it is a demo",
                          by_user=owner)
    assert row.status == "overridden"
    assert row.override == "Keep as is"
    assert row.decided_by == owner and row.decided_by_verified is True
    assert row.decided_by_name == "Jonathan"
    assert row.history[0]["status"] == "ai-default"
    assert dsvc.value_of(row) == "Keep as is"


def test_confirm_marks_confirmed_and_human_decided(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="confirm", by_user=owner)
    assert row.confirmed is True and row.status == "human-decided" and row.override == ""


def test_pick_of_recommended_is_a_confirm(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Rename and re-record", by_user=owner)
    assert row.confirmed is True and row.status == "human-decided"


def test_write_in_appends_option(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="write_in", value="Rename only, no re-record", by_user=owner)
    assert "Rename only, no re-record" in row.options
    assert row.override == "Rename only, no re-record" and row.status == "overridden"


def test_write_in_matching_existing_option_does_not_duplicate(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="write_in", value="  keep as IS ", by_user=owner)
    assert row.options == ["Rename and re-record", "Keep as is"]
    assert row.override == "Keep as is"


def test_pick_of_unknown_option_is_invalid(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    with pytest.raises(dsvc.DecisionInvalid):
        dsvc.apply_edit(row, action="pick", value="Not an option", by_user=owner)


def test_undo_restores_previous_state(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    row = dsvc.apply_edit(row, action="undo", by_user=owner)
    assert row.status == "ai-default" and row.override == "" and row.history == []


def test_undo_with_empty_history_is_409(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    with pytest.raises(dsvc.DecisionConflict, match="nothing to undo"):
        dsvc.apply_edit(row, action="undo", by_user=owner)


def test_concurrent_edits_both_recorded_in_history(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    stale = StoryboardDecision.objects.get(pk=row.pk)  # a second tab's copy
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    after = dsvc.apply_edit(stale, action="confirm", by_user=owner)  # re-read under lock
    assert after.history[0]["override"] == "Keep as is"
    assert len(after.history) == 2


def test_publish_cannot_reset_a_ruling_and_rolls_back(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    with pytest.raises(dsvc.DecisionConflict, match="banner"):
        dsvc.publish(board, [_row(), _row(id="new-row", question="New?", options=[], recommended="",
                                          review_ask="", needed_by="")])
    assert not board.decisions.filter(key="new-row").exists()
    assert dsvc.value_of(board.decisions.get(key="banner")) == "Keep as is"


def test_publish_keeps_ruling_when_value_matches(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    dsvc.publish(board, [_row(recommended="Keep as is", plain_question="Reworded?")])
    row = board.decisions.get(key="banner")
    assert row.status == "overridden" and row.plain_question == "Reworded?"
    assert row.decided_by == owner and len(row.history) == 1


def test_publish_cannot_drop_a_ruled_row(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="confirm", by_user=owner)
    with pytest.raises(dsvc.DecisionConflict, match="supersede"):
        dsvc.publish(board, [])


def test_publish_drops_untouched_ai_rows(board):
    dsvc.publish(board, [_row()])
    dsvc.publish(board, [])
    assert not board.decisions.exists()


def test_supersede_marks_old_row(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="confirm", by_user=owner)
    dsvc.publish(board, [_row(), _row(id="banner-v2", supersedes="banner", recommended="Keep as is")])
    assert board.decisions.get(key="banner").superseded_by == "banner-v2"


def test_serialize_derives_value_and_hides_user_ids(board, owner):
    (row,) = dsvc.publish(board, [_row()])
    row = dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    out = dsvc.serialize(row)
    assert out["id"] == "banner" and out["value"] == "Keep as is"
    assert out["decided_by"] == "Jonathan" and "decided_by_id" not in out
    assert out["editable"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storyboard_decisions.py -v`
Expected: FAIL — `AttributeError: module 'apps.storyboards.decisions' has no attribute 'validate'`.

- [ ] **Step 3: Implement the service** (append to `apps/storyboards/decisions.py`)

```python
from django.db import transaction
from django.utils import timezone

EDIT_ACTIONS = ("confirm", "pick", "write_in", "undo")
_MAX_OPTION_WORDS = 8
#: What a publisher may send. decided_* / confirmed / history / override_reasoning
#: belong to people and are set only by apply_edit.
_PUBLISHABLE = (
    "id", "scope", "question", "plain_question", "options", "recommended", "override",
    "reasoning", "source", "evidence_basis", "conflict_signals", "status", "revisit_when",
    "review_ask", "needed_by", "confirm_reason", "supersedes",
)
_PERSON_OWNED = ("decided_by", "decided_by_name", "decided_by_verified", "decided_at",
                 "confirmed", "history", "override_reasoning")
#: What a history snapshot holds — enough to undo one edit.
_SNAPSHOT = ("status", "override", "override_reasoning", "confirmed", "options",
             "decided_by_id", "decided_by_name", "decided_by_verified", "decided_at")


class DecisionInvalid(ValueError):
    """The row breaks the contract (422)."""


class DecisionConflict(Exception):
    """The write would undo a person's ruling, or there is nothing to undo (409)."""


def value_of(row) -> str:
    return row.override or row.recommended


def _norm(s: str) -> str:
    return " ".join((s or "").split()).casefold()


def _clean_str(raw: dict, name: str) -> str:
    v = raw.get(name, "")
    if v is None:
        return ""
    if not isinstance(v, str):
        raise DecisionInvalid(f"{name} must be a string")
    return v.strip()


def validate(raw: dict) -> dict:
    """One publisher row, normalized. Raises DecisionInvalid naming the field."""
    unknown = set(raw) - set(_PUBLISHABLE)
    if unknown & set(_PERSON_OWNED):
        raise DecisionInvalid(
            f"{sorted(unknown & set(_PERSON_OWNED))} are set by the server from the signed-in "
            "person (decided_by …) — a publisher may not send them")
    if unknown:
        raise DecisionInvalid(f"unknown decision fields: {sorted(unknown)}")
    row = {name: _clean_str(raw, name) for name in _PUBLISHABLE
           if name not in ("options", "conflict_signals")}
    if not row["id"]:
        raise DecisionInvalid("id is required")
    if not row["question"]:
        raise DecisionInvalid(f"{row['id']}: question is required")
    options = raw.get("options") or []
    signals = raw.get("conflict_signals") or []
    if not isinstance(options, list) or not all(isinstance(o, str) for o in options):
        raise DecisionInvalid(f"{row['id']}: options must be a list of strings")
    if not isinstance(signals, list) or not all(isinstance(s, str) for s in signals):
        raise DecisionInvalid(f"{row['id']}: conflict_signals must be a list of strings")
    row["options"] = [o.strip() for o in options if o.strip()]
    row["conflict_signals"] = [s.strip() for s in signals if s.strip()]
    for o in row["options"]:
        if len(o.split()) > _MAX_OPTION_WORDS:
            raise DecisionInvalid(f"{row['id']}: option {o!r} is longer than {_MAX_OPTION_WORDS} words")
    if row["options"] and row["recommended"] not in row["options"]:
        raise DecisionInvalid(f"{row['id']}: recommended must be one of options")
    if row["override"] and row["override"] not in row["options"]:
        raise DecisionInvalid(f"{row['id']}: override must be one of options")
    row["status"] = row["status"] or "ai-default"
    row["evidence_basis"] = row["evidence_basis"] or "inferred"
    for name, allowed in (("status", STATUSES), ("evidence_basis", EVIDENCE_BASES)):
        if row[name] not in allowed:
            raise DecisionInvalid(f"{row['id']}: {name} must be one of {list(allowed)}")
    if row["review_ask"] and row["review_ask"] not in REVIEW_ASKS:
        raise DecisionInvalid(f"{row['id']}: review_ask must be one of {list(REVIEW_ASKS)}")
    if row["needed_by"] and row["needed_by"] not in NEEDED_BY:
        raise DecisionInvalid(f"{row['id']}: needed_by must be one of {list(NEEDED_BY)}")
    if row["review_ask"] == "required-before" and not row["needed_by"]:
        raise DecisionInvalid(f"{row['id']}: required-before needs needed_by")
    if row["status"] == "deferred" and not row["revisit_when"]:
        raise DecisionInvalid(f"{row['id']}: a deferred row needs revisit_when")
    if row["status"] in HUMAN_STATUSES:
        raise DecisionInvalid(f"{row['id']}: only a person's edit can make a row {row['status']}")
    return row


def _is_ruled(row) -> bool:
    return row.status in HUMAN_STATUSES or row.confirmed


@transaction.atomic
def publish(board, rows: list[dict]):
    """Replace the board's decision set with ``rows`` (the publisher's list).

    A ruled row (a person decided or confirmed it) keeps its value, status,
    decider and history: the publisher may reword it but not move it, and may
    not drop it — it supersedes it instead. Anything else fails the WHOLE
    publish (atomic), so a board is never half-updated.
    """
    from apps.storyboards.models import StoryboardDecision

    clean = [validate(r) for r in rows]
    seen: set[str] = set()
    for r in clean:
        if r["id"] in seen:
            raise DecisionInvalid(f"duplicate decision id {r['id']!r}")
        seen.add(r["id"])
    existing = {d.key: d for d in StoryboardDecision.objects.select_for_update()
                .filter(storyboard=board)}
    superseded = {r["supersedes"]: r["id"] for r in clean if r["supersedes"]}

    for key, old in existing.items():
        if key not in seen and _is_ruled(old) and key not in superseded:
            raise DecisionConflict(
                f"{key}: a person ruled on this row; supersede it instead of dropping it")

    out = []
    for pos, r in enumerate(clean):
        old = existing.get(r["id"])
        incoming_value = r["override"] or r["recommended"]
        if old is not None and _is_ruled(old):
            if incoming_value != value_of(old) and r["id"] not in superseded:
                raise DecisionConflict(
                    f"{r['id']}: a person ruled {value_of(old)!r}; a publish may not change it "
                    "(supersede the row and say why)")
            for name in ("scope", "question", "plain_question", "reasoning", "source",
                         "evidence_basis", "conflict_signals", "review_ask", "needed_by",
                         "confirm_reason", "revisit_when", "supersedes"):
                setattr(old, name, r[name])
            old.options = old.options + [o for o in r["options"] if o not in old.options]
            old.position = pos
            old.superseded_by = superseded.get(r["id"], "")
            old.save()
            out.append(old)
            continue
        fields = {k: v for k, v in r.items() if k != "id"}
        fields["superseded_by"] = superseded.get(r["id"], "")
        row, _ = StoryboardDecision.objects.update_or_create(
            storyboard=board, key=r["id"], defaults={**fields, "position": pos})
        out.append(row)
    for key, old in existing.items():
        if key in superseded:
            old.superseded_by = superseded[key]
            old.save(update_fields=["superseded_by", "updated_at"])
        elif key not in seen:
            old.delete()
    return out


def _snapshot(row) -> dict:
    snap = {name: getattr(row, name) for name in _SNAPSHOT}
    if snap["decided_at"] is not None:
        snap["decided_at"] = snap["decided_at"].isoformat()
    snap["at"] = timezone.now().isoformat()
    return snap


def _restore(row, snap: dict) -> None:
    from django.utils.dateparse import parse_datetime

    for name in _SNAPSHOT:
        value = snap.get(name)
        if name == "decided_at" and value:
            value = parse_datetime(value)
        setattr(row, name, value)


def _person_name(user, by_name: str) -> str:
    if user is not None and getattr(user, "is_authenticated", False):
        return user.get_full_name() or user.first_name or user.email or user.username
    return (by_name or "").strip()


def apply_edit(row, *, action: str, value: str = "", reasoning: str = "", by_user=None,
               by_name: str = ""):
    """A person's edit: confirm / pick / write_in / undo. Attributed and undoable.

    Re-reads the row under a lock so two tabs editing at once both land, in
    order, with the second's history holding the first.
    """
    from apps.storyboards.models import StoryboardDecision

    if action not in EDIT_ACTIONS:
        raise DecisionInvalid(f"action must be one of {list(EDIT_ACTIONS)}")
    user = by_user if getattr(by_user, "is_authenticated", False) else None
    name = _person_name(user, by_name)
    if action != "undo" and not name:
        raise DecisionInvalid("an edit needs a name")

    with transaction.atomic():
        row = StoryboardDecision.objects.select_for_update().get(pk=row.pk)
        if action == "undo":
            if not row.history:
                raise DecisionConflict(f"{row.key}: nothing to undo")
            snap, *rest = row.history
            _restore(row, snap)
            row.history = rest
            row.save()
            return row

        before = _snapshot(row)
        value = (value or "").strip()
        if action == "write_in":
            if not value:
                raise DecisionInvalid("a write-in needs a value")
            match = next((o for o in row.options if _norm(o) == _norm(value)), None)
            if match is None:
                if len(value.split()) > _MAX_OPTION_WORDS:
                    raise DecisionInvalid(f"a write-in is at most {_MAX_OPTION_WORDS} words")
                row.options = [*row.options, value]
                match = value
            value, action = match, "pick"
        if action == "pick":
            if value not in row.options:
                raise DecisionInvalid(f"{value!r} is not one of this decision's options")
            if value == row.recommended:
                action = "confirm"
        if action == "confirm":
            row.override = ""
            row.confirmed = True
            row.status = "human-decided"
        else:
            row.override = value
            row.confirmed = False
            row.status = "overridden"
        row.override_reasoning = (reasoning or "").strip()
        row.decided_by = user
        row.decided_by_name = name
        row.decided_by_verified = user is not None
        row.decided_at = timezone.now()
        row.history = [before, *row.history]
        row.save()
        return row


def serialize(row, *, editable: bool = True) -> dict:
    """The row as the page and the publisher read it. Names, never user ids."""
    return {
        "id": row.key,
        "scope": row.scope,
        "question": row.question,
        "plain_question": row.plain_question,
        "options": list(row.options),
        "recommended": row.recommended,
        "override": row.override,
        "value": value_of(row),
        "reasoning": row.reasoning,
        "override_reasoning": row.override_reasoning,
        "source": row.source,
        "evidence_basis": row.evidence_basis,
        "conflict_signals": list(row.conflict_signals),
        "status": row.status,
        "revisit_when": row.revisit_when,
        "review_ask": row.review_ask,
        "needed_by": row.needed_by,
        "confirm_reason": row.confirm_reason,
        "decided_by": row.decided_by_name,
        "decided_by_verified": row.decided_by_verified,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "confirmed": row.confirmed,
        "history": [
            {"status": h.get("status", ""), "value": h.get("override") or row.recommended,
             "decided_by": h.get("decided_by_name", ""), "at": h.get("at", "")}
            for h in row.history
        ],
        "supersedes": row.supersedes,
        "superseded_by": row.superseded_by,
        "editable": editable,
    }
```

Note for the implementer: `_snapshot` stores `options` so an undo of a write-in removes the appended option too.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storyboard_decisions.py -v`
Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add apps/storyboards/decisions.py tests/test_storyboard_decisions.py
git commit -m "feat(storyboards): decision service — publish upsert, person edits, undo"
```

---

### Task 3: Converting the old brief lists (write path, data migration, read-time narrative caveats)

**Files:**
- Modify: `apps/storyboards/decisions.py` (add `from_legacy_brief`, `from_narrative_caveats`)
- Create: `apps/storyboards/migrations/0008_brief_lists_to_decisions.py`
- Test: `tests/test_storyboard_decisions.py` (append)

**Interfaces:**
- Consumes: `validate`, `publish` (Task 2).
- Produces:
  - `from_legacy_brief(brief: dict, *, taken: set[str] | None = None) -> list[dict]` — publisher-shaped rows (`id`, …) for `assumptions` / `divergences` / `open_questions`.
  - `from_narrative_caveats(caveats: dict, narrative_slug: str) -> list[dict]` — same, with `scope=narrative_slug`, ids prefixed with the slug.
  - `LEGACY_BRIEF_KEYS = ("assumptions", "divergences", "open_questions")`

- [ ] **Step 1: Write the failing tests** (append)

```python
LEGACY = {
    "audience": "Edesia and OES",
    "assumptions": ["SQ-LNS is not in the demo data."],
    "divergences": [{"script": "Four suppliers", "data": "Three in tender 356",
                     "resolution": "Re-record cut 2", "narrative_slug": "supply-rutf"}],
    "open_questions": ["Does OES want the forecast cut?"],
}


def test_from_legacy_brief_maps_each_list():
    rows = dsvc.from_legacy_brief(LEGACY)
    by_kind = {r["id"].split("-")[0]: r for r in rows}
    a, d, q = by_kind["assumption"], by_kind["divergence"], by_kind["question"]
    assert a["status"] == "ai-default" and a["evidence_basis"] == "inferred" and a["review_ask"] == ""
    # An assumption of at most 8 words is its own single option (and the recommendation).
    assert a["options"] == ["SQ-LNS is not in the demo data."]
    assert a["recommended"] == "SQ-LNS is not in the demo data."
    assert d["evidence_basis"] == "conflicting"
    assert d["conflict_signals"] == ["Script: Four suppliers", "Data: Three in tender 356"]
    assert d["recommended"] == "Re-record cut 2" and d["review_ask"] == "recommended-confirmation"
    assert d["scope"] == "supply-rutf"
    assert q["review_ask"] == "recommended-confirmation" and q["options"] == []
    for r in rows:
        dsvc.validate(r)  # every converted row is a valid row


def test_from_legacy_brief_ids_are_stable_and_unique():
    twice = {"assumptions": ["Same words.", "Same words."]}
    ids = [r["id"] for r in dsvc.from_legacy_brief(twice)]
    assert len(set(ids)) == 2
    assert ids == [r["id"] for r in dsvc.from_legacy_brief(twice)]


def test_create_converts_brief_lists_to_rows_and_strips_them(ws, owner):
    from django.test import Client

    c = Client()
    c.force_login(owner)
    resp = c.post("/api/w/connect/storyboards/", {"slug": "s1", "title": "S1", "brief": LEGACY},
                  content_type="application/json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert "assumptions" not in body["brief"] or body["brief"]["assumptions"] == []
    assert len(body["decisions"]) == 3
    assert Storyboard.objects.get(slug="s1").brief == {"audience": "Edesia and OES"}


def test_legacy_brief_migrates_once_and_patch_title_keeps_rows(ws, owner):
    import importlib

    from django.apps import apps as django_apps
    from django.test import Client

    b = Storyboard.objects.create(slug="old", title="Old", workspace=ws, brief=dict(LEGACY))
    mig = importlib.import_module("apps.storyboards.migrations.0008_brief_lists_to_decisions")
    mig.forwards(django_apps, None)
    mig.forwards(django_apps, None)  # idempotent
    b.refresh_from_db()
    assert b.brief == {"audience": "Edesia and OES"}
    assert b.decisions.count() == 3
    c = Client()
    c.force_login(owner)
    resp = c.patch("/api/w/connect/storyboards/old", {"title": "New"}, content_type="application/json")
    assert resp.status_code == 200
    assert b.decisions.count() == 3


def test_narrative_caveats_become_read_only_scoped_rows():
    rows = dsvc.from_narrative_caveats(
        {"assumptions": ["Programme 10112 stands in."], "divergences": [], "open_questions": []},
        "supply-rutf")
    assert rows[0]["scope"] == "supply-rutf"
    assert rows[0]["id"].startswith("supply-rutf--assumption-")
```

(`test_create_converts…` and the PATCH half of `test_legacy_brief_migrates…` need Task 4's schema change; they are written now and go green in Task 4.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storyboard_decisions.py -k "legacy or caveats or converts" -v`
Expected: FAIL — `AttributeError: … has no attribute 'from_legacy_brief'`; the migration module does not exist.

- [ ] **Step 3: Implement conversion** (append to `apps/storyboards/decisions.py`)

```python
from django.utils.text import slugify

LEGACY_BRIEF_KEYS = ("assumptions", "divergences", "open_questions")


def _key(prefix: str, text: str, taken: set[str]) -> str:
    base = f"{prefix}-{slugify(text)[:60]}".rstrip("-") or prefix
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    taken.add(key)
    return key


def _short_option(text: str) -> list[str]:
    """An assumption's one option is the statement itself, when it fits the
    1–8 word rule; longer ones carry no options (the publisher adds them)."""
    return [text] if 0 < len(text.split()) <= _MAX_OPTION_WORDS else []


def from_legacy_brief(brief: dict, *, taken: set[str] | None = None, prefix: str = "") -> list[dict]:
    """#1501's brief lists as decision rows (spec B, "Migration from the brief")."""
    taken = set() if taken is None else taken
    rows: list[dict] = []
    for text in brief.get("assumptions") or []:
        text = (text or "").strip()
        if not text:
            continue
        opts = _short_option(text)
        rows.append({"id": _key(f"{prefix}assumption", text, taken), "question": text,
                     "plain_question": text, "options": opts,
                     "recommended": opts[0] if opts else "", "status": "ai-default",
                     "evidence_basis": "inferred", "review_ask": ""})
    for d in brief.get("divergences") or []:
        script = (d.get("script") or "").strip()
        if not script:
            continue
        data = (d.get("data") or "").strip()
        resolution = (d.get("resolution") or "").strip()
        opts = _short_option(resolution)
        rows.append({
            "id": _key(f"{prefix}divergence", script, taken),
            "scope": (d.get("narrative_slug") or "").strip(),
            "question": f"The script says: {script}. The data shows: {data}." if data
            else f"The script says: {script}.",
            "plain_question": f"The script and the data disagree: {script}",
            "options": opts,
            "recommended": opts[0] if opts else "",
            "reasoning": resolution if not opts else "",
            "evidence_basis": "conflicting",
            "conflict_signals": [s for s in (f"Script: {script}", f"Data: {data}" if data else "") if s],
            "status": "ai-default",
            "review_ask": "recommended-confirmation",
        })
    for text in brief.get("open_questions") or []:
        text = (text or "").strip()
        if not text:
            continue
        rows.append({"id": _key(f"{prefix}question", text, taken), "question": text,
                     "plain_question": text, "options": [], "recommended": "",
                     "status": "ai-default", "evidence_basis": "inferred",
                     "review_ask": "recommended-confirmation"})
    return rows


def from_narrative_caveats(caveats: dict, narrative_slug: str) -> list[dict]:
    """A narrative version's own caveats (request_json), read-time only, scoped
    to that narrative. Not stored: they are rendered read-only until the
    publisher sends decisions[] instead (spec B)."""
    rows = from_legacy_brief(caveats, prefix=f"{narrative_slug}--")
    for r in rows:
        r["scope"] = narrative_slug
    return rows
```

`apps/storyboards/migrations/0008_brief_lists_to_decisions.py`:

```python
"""Brief lists (#1501) → StoryboardDecision rows. Idempotent: a board whose
brief no longer carries the lists is skipped, so a re-run changes nothing."""
from django.db import migrations

from apps.storyboards.decisions import LEGACY_BRIEF_KEYS, from_legacy_brief, validate


def forwards(apps, schema_editor):
    Storyboard = apps.get_model("storyboards", "Storyboard")
    Decision = apps.get_model("storyboards", "StoryboardDecision")
    for board in Storyboard.objects.all():
        brief = dict(board.brief or {})
        if not any(brief.get(k) for k in LEGACY_BRIEF_KEYS):
            continue
        taken = set(Decision.objects.filter(storyboard=board).values_list("key", flat=True))
        start = len(taken)
        for pos, row in enumerate(from_legacy_brief(brief, taken=taken)):
            clean = validate(row)
            key = clean.pop("id")
            Decision.objects.create(storyboard=board, key=key, position=start + pos, **clean)
        board.brief = {k: v for k, v in brief.items() if k not in LEGACY_BRIEF_KEYS}
        board.save(update_fields=["brief"])


class Migration(migrations.Migration):
    dependencies = [("storyboards", "0007_storyboarddecision")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
```

The write-path conversion lands in Task 4 (it needs the schema change). The create test above passes after Task 4; run only the non-API ones now.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_storyboard_decisions.py -k "from_legacy or narrative_caveats_become" -v`
Expected: PASS (3 tests). `test_create_converts…` and `test_legacy_brief_migrates…` are run in Task 4.

- [ ] **Step 5: Commit**

```bash
git add apps/storyboards/decisions.py apps/storyboards/migrations/0008_brief_lists_to_decisions.py tests/test_storyboard_decisions.py
git commit -m "feat(storyboards): convert brief lists and narrative caveats to decision rows"
```

---

### Task 4: The API — publish `decisions[]`, read `decisions[]`, and a member's edit

**Files:**
- Modify: `apps/storyboards/schemas.py`, `apps/storyboards/services.py:resolve_board`, `apps/storyboards/api.py` (create/patch + new route), `apps/api/route_gates.py`
- Regenerate: `frontend/src/api/generated.ts`
- Test: `tests/test_storyboard_decisions.py` (append)

**Interfaces:**
- Consumes: `decisions.publish`, `decisions.apply_edit`, `decisions.serialize`, `decisions.from_legacy_brief`, `decisions.from_narrative_caveats`, `DecisionInvalid`, `DecisionConflict`.
- Produces:
  - `DecisionIn` (publisher row: `id`, `scope`, `question`, `plain_question`, `options`, `recommended`, `override`, `reasoning`, `source`, `evidence_basis`, `conflict_signals`, `status`, `revisit_when`, `review_ask`, `needed_by`, `confirm_reason`, `supersedes`), `DecisionOut` (= `serialize` keys), `DecisionEditIn {action, value, reasoning, author_name}`.
  - `StoryboardIn.decisions: list[DecisionIn] | None`, `StoryboardPatchIn.decisions: list[DecisionIn] | None`, `StoryboardOut.decisions: list[DecisionOut]`.
  - Route `POST /api/storyboards/{slug}/decisions/{key}/edit` → `DecisionOut`, operation id `edit_storyboard_decision`. Self-enforcing (`auth=None`). Plan C extends its non-member branch.
  - `services._apply_decisions(board, payload_decisions, brief_in) -> dict` (the brief to store).

- [ ] **Step 1: Write the failing tests** (append)

```python
from django.test import Client


@pytest.fixture()
def member(owner):
    c = Client()
    c.force_login(owner)
    return c


def test_create_with_decisions_round_trips(ws, member):
    resp = member.post("/api/w/connect/storyboards/",
                       {"slug": "s2", "title": "S2", "decisions": [_row()]},
                       content_type="application/json")
    assert resp.status_code == 200, resp.content
    (d,) = resp.json()["decisions"]
    assert d["id"] == "banner" and d["value"] == "Rename and re-record" and d["editable"] is True


def test_invalid_decision_is_422(ws, member):
    resp = member.post("/api/w/connect/storyboards/",
                       {"slug": "s3", "title": "S3", "decisions": [_row(recommended="nope")]},
                       content_type="application/json")
    assert resp.status_code == 422
    assert not Storyboard.objects.filter(slug="s3").exists()


def test_patch_that_resets_a_ruling_is_409(board, member, owner):
    (row,) = dsvc.publish(board, [_row()])
    dsvc.apply_edit(row, action="pick", value="Keep as is", by_user=owner)
    resp = member.patch(f"/api/w/connect/storyboards/{board.slug}", {"decisions": [_row()]},
                        content_type="application/json")
    assert resp.status_code == 409
    assert "banner" in resp.json()["detail"]


def test_member_edit_applies_immediately(board, member):
    dsvc.publish(board, [_row()])
    resp = member.post(f"/api/storyboards/{board.slug}/decisions/banner/edit",
                       {"action": "pick", "value": "Keep as is", "reasoning": "fine"},
                       content_type="application/json")
    assert resp.status_code == 200, resp.content
    assert resp.json()["value"] == "Keep as is" and resp.json()["decided_by"] == "Jonathan"


def test_non_member_edit_is_refused(board):
    dsvc.publish(board, [_row()])
    token = board.ensure_share_token()
    resp = Client().post(f"/api/storyboards/{board.slug}/decisions/banner/edit?t={token}",
                         {"action": "confirm", "author_name": "Lily"},
                         content_type="application/json")
    assert resp.status_code == 403


def test_edit_unknown_row_is_404(board, member):
    resp = member.post(f"/api/storyboards/{board.slug}/decisions/nope/edit",
                       {"action": "confirm"}, content_type="application/json")
    assert resp.status_code == 404


def test_board_read_includes_narrative_caveats_read_only(board, member, monkeypatch):
    from apps.storyboards import services

    monkeypatch.setattr(services, "_narrative_caveat_rows",
                        lambda b: [dsvc.from_narrative_caveats({"assumptions": ["X holds."]}, "n1")[0]])
    body = member.get(f"/api/w/connect/storyboards/{board.slug}").json()
    (d,) = body["decisions"]
    assert d["scope"] == "n1" and d["editable"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_storyboard_decisions.py -k "round_trips or 422 or 409 or edit or caveats_read_only or converts or migrates" -v`
Expected: FAIL — `decisions` is an unknown field (422 from `StrictModel`) / the route 404s.

- [ ] **Step 3: Schemas** — in `apps/storyboards/schemas.py`, add above `class StoryboardOut`:

```python
class DecisionIn(StrictModel):
    """One decision row as a publisher sends it (spec B). Person-owned fields —
    decided_by, confirmed, history — are not accepted: the server sets them."""

    id: str
    scope: str = ""
    question: str
    plain_question: str = ""
    options: list[str] = []
    recommended: str = ""
    override: str = ""
    reasoning: str = ""
    source: str = ""
    evidence_basis: Literal["stated", "inferred", "conflicting"] = "inferred"
    conflict_signals: list[str] = []
    status: Literal["ai-default", "deferred"] = "ai-default"
    revisit_when: str = ""
    review_ask: Literal["", "recommended-confirmation", "required-before"] = ""
    needed_by: Literal["", "external-share", "next-run"] = ""
    confirm_reason: str = ""
    supersedes: str = ""


class DecisionHistoryOut(StrictModel):
    status: str
    value: str
    decided_by: str
    at: str


class DecisionOut(StrictModel):
    id: str
    scope: str
    question: str
    plain_question: str
    options: list[str]
    recommended: str
    override: str
    value: str
    reasoning: str
    override_reasoning: str
    source: str
    evidence_basis: str
    conflict_signals: list[str]
    status: str
    revisit_when: str
    review_ask: str
    needed_by: str
    confirm_reason: str
    decided_by: str
    decided_by_verified: bool
    decided_at: str | None
    confirmed: bool
    history: list[DecisionHistoryOut]
    supersedes: str
    superseded_by: str
    editable: bool
    """False for a narrative's read-time caveat: shown, not yet a stored row."""


class DecisionEditIn(StrictModel):
    action: Literal["confirm", "pick", "write_in", "undo"]
    value: str = ""
    reasoning: str = ""
    author_name: str = ""
    """A guest's name (plan C, inside a round). Ignored for a signed-in member."""
```

Then add `decisions: list[DecisionOut] = []` to `StoryboardOut` (after `brief`), and `decisions: list[DecisionIn] | None = None` to `StoryboardIn` and `StoryboardPatchIn` (after `brief`; on `StoryboardPatchIn` with the comment `# Replaces the whole set; a ruled row may be reworded, never moved or dropped.`).

- [ ] **Step 4: Service** — in `apps/storyboards/services.py`:

```python
from apps.storyboards import decisions as decision_rows


def _narrative_caveat_rows(board: Storyboard) -> list[dict]:
    """Each narrative's own caveats (its current version's request_json), as
    read-only rows scoped to that narrative. Until the publisher sends
    decisions[], this is where a narrative's assumptions still come from."""
    out: list[dict] = []
    for act in board.acts.all():
        for e in act.entries.all():
            narrative = aggregate.build_narrative(e.narrative_slug, {board.workspace_id})
            current = ((narrative or {}).get("versions") or [None])[0] or {}
            rj = current.get("request_json") or {}
            out.extend(decision_rows.from_narrative_caveats(
                entry_cuts.caveats(rj, e.narrative_slug), e.narrative_slug))
    return out


def board_decisions(board: Storyboard) -> list[dict]:
    """Stored rows, then each narrative's read-time caveats (unsaved, read-only)."""
    from apps.storyboards.models import StoryboardDecision

    stored = [decision_rows.serialize(d) for d in board.decisions.all()]
    taken = {d["id"] for d in stored}
    read_only = []
    for raw in _narrative_caveat_rows(board):
        if raw["id"] in taken:
            continue
        clean = decision_rows.validate(raw)
        unsaved = StoryboardDecision(storyboard=board, key=clean.pop("id"), **clean)
        read_only.append(decision_rows.serialize(unsaved, editable=False))
    return stored + read_only
```

Before writing `_narrative_caveat_rows`, check how `_entry_payload` already reaches a version's `request_json` (it fills `EntryOut.assumptions` via `entry_cuts.caveats`) and reuse that exact lookup instead of the `versions[0]["request_json"]` path above if they differ — one lookup, not two. In `resolve_board`, add `"decisions": board_decisions(board),` to the returned dict.

- [ ] **Step 5: API** — in `apps/storyboards/api.py`:

Imports: `from apps.storyboards import decisions as decision_rows` and `DecisionEditIn, DecisionOut` from schemas.

Add the shared writer above the routes:

```python
def _write_decisions(board: Storyboard, payload) -> None:
    """The brief, minus its legacy lists, plus the decision set.

    The three #1501 lists are still accepted for one release and converted to
    rows, so a publisher that has not moved yet keeps working (spec B)."""
    rows = [d.model_dump() for d in payload.decisions] if payload.decisions is not None else None
    if payload.brief is not None:
        brief_in = payload.brief.model_dump(exclude_defaults=True)
        legacy = decision_rows.from_legacy_brief(brief_in)
        if legacy:
            if rows is None:  # keep what is on the board, add the converted lists
                rows = [decision_rows.publishable(d) for d in board.decisions.all()]
            rows = rows + legacy
        board.brief = {k: v for k, v in brief_in.items()
                       if k not in decision_rows.LEGACY_BRIEF_KEYS}
        board.save(update_fields=["brief", "updated_at"])
    if rows is not None:
        decision_rows.publish(board, rows)
```

and add to `apps/storyboards/decisions.py`:

```python
def publishable(row) -> dict:
    """A stored row back in publisher shape, so a brief-only write can carry the
    existing set through publish() unchanged."""
    return {"id": row.key, **{k: getattr(row, k) for k in _PUBLISHABLE if k != "id"},
            "status": row.status if row.status not in HUMAN_STATUSES else "ai-default",
            "recommended": value_of(row) if _is_ruled(row) else row.recommended,
            "override": ""}
```

(For a ruled row this sends its effective value as `recommended`, which `publish` accepts as "same value" and keeps the person's status — covered by `test_publish_keeps_ruling_when_value_matches`.)

In `create_storyboard`: change `brief=payload.brief.model_dump(...) if payload.brief else {}` to `brief={}`, and after `_replace_acts(board, payload.acts)` call `_write_decisions(board, payload)`. In `patch_storyboard`: delete the `if payload.brief is not None:` block (it moves into `_write_decisions`) and call `_write_decisions(board, payload)` after `_replace_acts`. Wrap both route bodies' write sections:

```python
    try:
        with transaction.atomic():
            ...existing writes...
            _write_decisions(board, payload)
    except decision_rows.DecisionInvalid as e:
        raise HttpError(422, str(e))
    except decision_rows.DecisionConflict as e:
        raise HttpError(409, str(e))
```

New route, after `leave_feedback`:

```python
@router.post(
    "/{slug}/decisions/{key}/edit",
    response=DecisionOut,
    auth=None,
    summary="Confirm, change, write in or undo one decision (public via ?t=<share_token>)",
)
def edit_storyboard_decision(request: HttpRequest, slug: str, key: str, payload: DecisionEditIn,
                             ws: str = "") -> dict:
    """A workspace member's edit applies immediately, attributed and undoable.

    Self-enforcing like the read: a wrong token 404s. A link holder may edit
    only inside an open feedback round, where the edit is staged."""
    board = _readable_or_404(request, slug, ws)
    row = board.decisions.filter(key=key).first()
    if row is None:
        raise HttpError(404, "no such decision on this storyboard")
    # Plan C replaces this refusal with staging inside an open round.
    if not _is_member(request, board):
        raise HttpError(403, "only a member can change a decision outside a feedback round")
    try:
        row = decision_rows.apply_edit(row, action=payload.action, value=payload.value,
                                       reasoning=payload.reasoning, by_user=request.user)
    except decision_rows.DecisionInvalid as e:
        raise HttpError(422, str(e))
    except decision_rows.DecisionConflict as e:
        raise HttpError(409, str(e))
    return decision_rows.serialize(row)
```

CSRF: a member's call rides an ambient session, so check it the way `suggest_review` does (`from apps.common.csrf import csrf_rejected`; `apps/reviews/api.py:634`). Put this first in the member branch: `if not request.GET.get("t") and csrf_rejected(request): raise HttpError(403, "CSRF verification failed")`. Django's test `Client` does not enforce CSRF by default, so the tests above are unaffected; add one test with `Client(enforce_csrf_checks=True)` asserting 403 without the header.

- [ ] **Step 6: Route gates** — in `apps/api/route_gates.py`:
  - `GATES` under `# --- apps/storyboards/api.py`: `"edit_storyboard_decision": ("token-link", "member"),  # members apply immediately; a token holder only inside an open round (plan C)`
  - `VIEWER_MAY_MUTATE`: `"edit_storyboard_decision": "a member's ruling on a demo-set default is the reader-tier act the decision log exists for; it is attributed and undoable (owner decision 2026-10-15)",`
  - `NOT_SETTINGS`: add `"edit_storyboard_decision": "content",` next to `"leave_feedback": "content"`.

- [ ] **Step 7: Run the backend tests**

Run: `uv run pytest tests/test_storyboard_decisions.py tests/test_storyboard_api.py tests/test_storyboard_demo_set.py tests/test_every_route_declares_its_gate.py tests/test_architecture_boundary.py -v`
Expected: PASS. `test_storyboard_demo_set.py` asserted brief lists on the board read; update those assertions to read `body["decisions"]` (three rows converted from `BRIEF`) — the behaviour changed on purpose.

- [ ] **Step 8: Regenerate the API types**

Run: `cd frontend && npm run gen:api:local && cd ..`
Expected: `frontend/src/api/generated.ts` gains `DecisionIn`, `DecisionOut`, `DecisionEditIn` and the `edit_storyboard_decision` operation.

- [ ] **Step 9: Commit**

```bash
git add apps/storyboards apps/api/route_gates.py frontend/src/api/generated.ts tests/
git commit -m "feat(storyboards): publish and read decision rows; members edit them"
```

---

### Task 5: The decision log on the page

**Files:**
- Modify: `frontend/src/api/storyboards.ts`
- Create: `frontend/src/components/storyboard/DecisionLog.tsx`, `frontend/src/components/storyboard/DecisionLog.test.tsx`
- Modify: `frontend/src/pages/StoryboardPage.tsx:16,219` (import + replace `<Caveats board={board} />`)
- Modify: `frontend/src/components/storyboard/DemoBrief.tsx` (delete `Caveats`, `CaveatsView`, `collect`, `Tagged` once nothing imports them — check `grep -rn "Caveats" frontend/src`)
- Modify: `docs/architecture/pages.md` (storyboard entry)

**Interfaces:**
- Consumes: `DecisionOut` shape (Task 4), `POST /api/storyboards/{slug}/decisions/{key}/edit`.
- Produces:
  - `export interface Decision` (fields of `DecisionOut`), `Storyboard.decisions?: Decision[]`.
  - `export async function editDecision(slug: string, key: string, body: {action: 'confirm'|'pick'|'write_in'|'undo'; value?: string; reasoning?: string; author_name?: string}, token?: string|null, ws?: string|null): Promise<Decision>`
  - `export function groupDecisions(rows: Decision[]): { title: string; open: boolean; rows: Decision[] }[]`
  - `export function DecisionLog(props: { board: Storyboard; token?: string|null; ws?: string|null; canEdit: boolean })` (holds its own row state)
  - `export function DecisionRow(props: { d: Decision; canEdit: boolean; onEdit: (body) => Promise<void> })`

- [ ] **Step 1: Write the failing test** — `frontend/src/components/storyboard/DecisionLog.test.tsx`:

```tsx
import { describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import type { Decision } from '@/api/storyboards'
import { DecisionRow, groupDecisions } from './DecisionLog'

const base: Decision = {
  id: 'banner', scope: '', question: 'q', plain_question: 'Rename the banner before Edesia sees it?',
  options: ['Rename and re-record', 'Keep as is'], recommended: 'Rename and re-record', override: '',
  value: 'Rename and re-record', reasoning: 'The internal name is in every cut.', override_reasoning: '',
  source: '', evidence_basis: 'stated', conflict_signals: [], status: 'ai-default', revisit_when: '',
  review_ask: 'required-before', needed_by: 'external-share', confirm_reason: '', decided_by: '',
  decided_by_verified: false, decided_at: null, confirmed: false, history: [], supersedes: '',
  superseded_by: '', editable: true,
}
const row = (over: Partial<Decision>): Decision => ({ ...base, ...over })

describe('groupDecisions', () => {
  it('puts asks first, then confirmations, then assumptions, then deferred; each row once', () => {
    const groups = groupDecisions([
      row({ id: 'a', review_ask: '', needed_by: '' }),
      row({ id: 'd', review_ask: '', needed_by: '', status: 'deferred', revisit_when: 'after Nov 12' }),
      row({ id: 'c', review_ask: 'recommended-confirmation', needed_by: '' }),
      row({ id: 'r' }),
      row({ id: 's', superseded_by: 'r' }),
    ])
    expect(groups.map((g) => g.title)).toEqual([
      'Answer before sharing', 'Please confirm', 'What the agent assumed', 'Not needed now', 'Replaced',
    ])
    expect(groups.map((g) => g.rows.map((r) => r.id))).toEqual([['r'], ['c'], ['a'], ['d'], ['s']])
    expect(groups.map((g) => g.open)).toEqual([true, true, false, false, false])
  })

  it('omits empty groups', () => {
    expect(groupDecisions([row({ id: 'a', review_ask: '', needed_by: '' })]).map((g) => g.title))
      .toEqual(['What the agent assumed'])
  })
})

describe('DecisionRow', () => {
  it('shows the plain question, the value and a recommended chip', () => {
    render(<DecisionRow d={base} canEdit={false} onEdit={vi.fn()} />)
    expect(screen.getByText('Rename the banner before Edesia sees it?')).toBeTruthy()
    expect(screen.getByText('Rename and re-record')).toBeTruthy()
    expect(screen.getByText('Recommended')).toBeTruthy()
    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull()
  })

  it('shows who changed it', () => {
    render(<DecisionRow d={row({ status: 'overridden', override: 'Keep as is', value: 'Keep as is',
      decided_by: 'Lily' })} canEdit={false} onEdit={vi.fn()} />)
    expect(screen.getByText('Changed by Lily')).toBeTruthy()
  })

  it('confirms through onEdit', async () => {
    const onEdit = vi.fn().mockResolvedValue(undefined)
    render(<DecisionRow d={base} canEdit onEdit={onEdit} />)
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(onEdit).toHaveBeenCalledWith({ action: 'confirm' })
  })

  it('a read-only caveat offers no actions even to an editor', () => {
    render(<DecisionRow d={row({ editable: false })} canEdit onEdit={vi.fn()} />)
    expect(screen.queryByRole('button', { name: 'Confirm' })).toBeNull()
  })
})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd frontend && npx vitest run src/components/storyboard/DecisionLog.test.tsx`
Expected: FAIL — cannot resolve `./DecisionLog`.

- [ ] **Step 3: API client** — in `frontend/src/api/storyboards.ts` add:

```ts
/** One default the set is built on — ace-web's decision row (spec B). */
export interface Decision {
  id: string
  scope: string
  question: string
  plain_question: string
  options: string[]
  recommended: string
  override: string
  /** Derived: `override` if set, else `recommended`. */
  value: string
  reasoning: string
  override_reasoning: string
  source: string
  evidence_basis: 'stated' | 'inferred' | 'conflicting' | string
  conflict_signals: string[]
  status: 'ai-default' | 'human-decided' | 'overridden' | 'deferred' | string
  revisit_when: string
  review_ask: '' | 'recommended-confirmation' | 'required-before' | string
  needed_by: '' | 'external-share' | 'next-run' | string
  confirm_reason: string
  decided_by: string
  decided_by_verified: boolean
  decided_at: string | null
  confirmed: boolean
  history: { status: string; value: string; decided_by: string; at: string }[]
  supersedes: string
  superseded_by: string
  /** False for a narrative's read-time caveat — shown, not yet a stored row. */
  editable: boolean
}

export type DecisionEdit = {
  action: 'confirm' | 'pick' | 'write_in' | 'undo'
  value?: string
  reasoning?: string
  author_name?: string
}

export async function editDecision(
  slug: string, key: string, body: DecisionEdit, token?: string | null, ws?: string | null,
): Promise<Decision> {
  const resp = await fetch(
    apiUrl(withToken(`/api/storyboards/${encodeURIComponent(slug)}/decisions/${encodeURIComponent(key)}/edit`, token, ws)),
    {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
      body: JSON.stringify(body),
    },
  )
  if (!resp.ok) {
    let detail = ''
    try { detail = (await resp.json())?.detail ?? '' } catch { /* ignore */ }
    throw new Error(detail || `Request failed (${resp.status})`)
  }
  return resp.json() as Promise<Decision>
}
```

…and add `decisions?: Decision[]` to `interface Storyboard`. `getCsrfToken()` may return null; pass `getCsrfToken() ?? ''` as `leaveFeedback` (same file, line ~177) does. If the POST/error boilerplate now appears twice, extract a `postJson<T>(url, body, fallback)` helper beside `getJson` and use it in both.

- [ ] **Step 4: Component** — `frontend/src/components/storyboard/DecisionLog.tsx`:

```tsx
import { useState } from 'react'
import type { Decision, DecisionEdit, Storyboard } from '@/api/storyboards'
import { editDecision } from '@/api/storyboards'

/**
 * The set's decision log — ace-web's pattern (spec B). Asks and assumptions
 * are the same row in different states, so ONE row component renders them all;
 * the grouping puts what the reader must do first, and each row appears once.
 */
export function groupDecisions(rows: Decision[]): { title: string; open: boolean; rows: Decision[] }[] {
  const live = rows.filter((d) => !d.superseded_by)
  const groups = [
    { title: 'Answer before sharing', open: true,
      rows: live.filter((d) => d.status !== 'deferred' && d.review_ask === 'required-before') },
    { title: 'Please confirm', open: true,
      rows: live.filter((d) => d.status !== 'deferred' && d.review_ask === 'recommended-confirmation') },
    { title: 'What the agent assumed', open: false,
      rows: live.filter((d) => d.status !== 'deferred' && !d.review_ask) },
    { title: 'Not needed now', open: false, rows: live.filter((d) => d.status === 'deferred') },
    { title: 'Replaced', open: false, rows: rows.filter((d) => d.superseded_by) },
  ]
  return groups.filter((g) => g.rows.length > 0)
}

function chip(d: Decision): string {
  if (d.status === 'overridden') return `Changed by ${d.decided_by}`
  if (d.confirmed) return `Confirmed by ${d.decided_by}`
  if (d.status === 'deferred') return `Later: ${d.revisit_when}`
  return 'Recommended'
}

export function DecisionRow({ d, canEdit, onEdit }: {
  d: Decision
  canEdit: boolean
  onEdit: (body: DecisionEdit) => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [writeIn, setWriteIn] = useState('')
  const [error, setError] = useState('')
  const editable = canEdit && d.editable
  const run = async (body: DecisionEdit) => {
    setBusy(true)
    setError('')
    try {
      await onEdit(body)
      setWriteIn('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'That did not save')
    } finally {
      setBusy(false)
    }
  }
  return (
    <li id={`decision-${d.id}`} className="rounded-md border border-border bg-card p-4">
      <p className="font-medium text-foreground">{d.plain_question || d.question}</p>
      {d.value && (
        <p className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <span className="text-foreground">{d.value}</span>
          <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">{chip(d)}</span>
        </p>
      )}
      {d.evidence_basis === 'conflicting' && d.conflict_signals.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-sm text-muted-foreground">
          {d.conflict_signals.map((s) => <li key={s}>{s}</li>)}
        </ul>
      )}
      {(d.reasoning || d.confirm_reason) && (
        <details className="mt-2 text-sm text-muted-foreground">
          <summary>Why</summary>
          {d.confirm_reason && <p>{d.confirm_reason}</p>}
          {d.reasoning && <p>{d.reasoning}</p>}
          {d.override_reasoning && <p>{d.decided_by}: {d.override_reasoning}</p>}
        </details>
      )}
      {editable && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
          {d.value && !d.confirmed && (
            <button type="button" disabled={busy} onClick={() => run({ action: 'confirm' })}
              className="rounded-md bg-primary px-3 py-1 text-primary-foreground">Confirm</button>
          )}
          {d.options.filter((o) => o !== d.value).map((o) => (
            <button key={o} type="button" disabled={busy} onClick={() => run({ action: 'pick', value: o })}
              className="rounded-md border border-border px-3 py-1">{o}</button>
          ))}
          <input aria-label="Write your own answer" value={writeIn} onChange={(e) => setWriteIn(e.target.value)}
            placeholder="Or write your own" className="rounded-md border border-border bg-background px-2 py-1" />
          {writeIn.trim() && (
            <button type="button" disabled={busy}
              onClick={() => run({ action: 'write_in', value: writeIn.trim() })}
              className="rounded-md border border-border px-3 py-1">Use this</button>
          )}
          {d.history.length > 0 && (
            <button type="button" disabled={busy} onClick={() => run({ action: 'undo' })}
              className="text-muted-foreground underline">Undo</button>
          )}
        </div>
      )}
      {error && <p role="alert" className="mt-2 text-sm text-destructive">{error}</p>}
    </li>
  )
}

export function DecisionLog({ board, token, ws, canEdit }: {
  board: Storyboard
  token?: string | null
  ws?: string | null
  canEdit: boolean
}) {
  // Local copy: an edit returns the updated row, which replaces its old self here.
  const [rows, setRows] = useState<Decision[]>(board.decisions ?? [])
  const onChanged = (d: Decision) => setRows((rs) => rs.map((x) => (x.id === d.id ? d : x)))
  const groups = groupDecisions(rows)
  if (groups.length === 0) return null
  return (
    <section aria-label="Decisions" className="mt-14 flex flex-col gap-6">
      <h2 className="text-xl font-semibold text-foreground">Before you share this</h2>
      {groups.map((g) => (
        <details key={g.title} open={g.open}>
          <summary className="cursor-pointer font-medium text-foreground">
            {g.title} <span className="text-muted-foreground">({g.rows.length})</span>
          </summary>
          <ul className="mt-3 flex flex-col gap-3">
            {g.rows.map((d) => (
              <DecisionRow key={d.id} d={d} canEdit={canEdit}
                onEdit={async (body) => onChanged(await editDecision(board.slug, d.id, body, token, ws))} />
            ))}
          </ul>
        </details>
      ))}
    </section>
  )
}
```

- [ ] **Step 5: Wire it into the page** — in `frontend/src/pages/StoryboardPage.tsx`, change the import on line 16 to `import { BriefPanel } from '@/components/storyboard/DemoBrief'` plus `import { DecisionLog } from '@/components/storyboard/DecisionLog'`, and replace `<Caveats board={board} />` (line ~219) with:

```tsx
<DecisionLog board={board} token={token} ws={workspace} canEdit={board.is_member} />
```

The page reads `const { slug, workspace } = useParams()` and `const token = params.get('t')` (lines 41–43); the component that renders line 219 receives `board` as a prop — thread `token` and `workspace` to it the way `NoteComposer` (line ~365) already gets them. Then delete `Caveats`/`CaveatsView`/`collect` from `DemoBrief.tsx` if `grep -rn "Caveats" frontend/src` shows no other user, and remove the now-unused `Divergence` brief fields from the `StoryboardBrief` TS interface **only** if `npm run build` stays green (the entry-level `assumptions` still exist server-side until the publisher moves).

- [ ] **Step 6: Run the frontend checks**

Run: `cd frontend && npx vitest run src/components/storyboard src/pages/StoryboardPage.test.tsx src/pages/StoryboardDemoSet.test.tsx && npm run build`
Expected: PASS and a clean build. `StoryboardDemoSet.test.tsx` rendered the old caveats; update its fixture to carry `decisions` and assert the new group titles instead.

- [ ] **Step 7: Docs** — in `docs/architecture/pages.md`, in the storyboard page's entry, replace the sentence about assumptions/divergences/open questions with: "Ends with the set's decision log: what must be answered before sharing, what to confirm, and what the agent assumed — one row per decision, each with a recommended option, who decided, and undo. Members' edits apply immediately."

- [ ] **Step 8: Commit**

```bash
git add frontend/src docs/architecture/pages.md
git commit -m "feat(storyboards): the decision log replaces the caveats section"
```

---

### Task 6: Whole-branch verification and ship

- [ ] **Step 1:** Run `uv run pytest` — expected: all pass.
- [ ] **Step 2:** Run `cd frontend && npm test && npm run build` — expected: all pass, clean build.
- [ ] **Step 3:** Run `uv run python manage.py makemigrations --check --dry-run` — expected: "No changes detected".
- [ ] **Step 4:** Push, open the PR ("Storyboards: decision rows replace assumptions and open questions"), arm auto-merge per the repo's merge-queue convention, and wait for it to merge without a sleep loop (`gh pr checks --watch` or `gh run watch`).
- [ ] **Step 5:** Deploying (`gh workflow run "Deploy to Labs (AWS)" --ref main`) needs the owner's approval in a manual-mode session; ask, then deploy and confirm `GET /api/storyboards/connect-supply-rutf` returns `decisions[]` with the RUTF board's four assumptions, two divergences and four open questions converted.

---

## Self-review notes

- Spec B coverage: row type and fields (Tasks 1–2), storage as a model (1), migration from the brief + read-time narrative caveats (3–4), layout and grouping (5), actions confirm/pick/write-in/comment — comments are `Feedback` with anchor `decision:<id>` and need no new code until plan C makes them round-visible; the existing `NoteComposer` takes an `anchor_id`, so add a "Comment" composer to `DecisionRow` in plan C Task 6 where notes become visible to everyone (5), shared-vs-mirrored contract via the drift pin (1), rulings binding the next run via the publish 409 (2, 4).
- Deliberately deferred to plan C: staging edits inside a round, guest edits, per-decision comment threads.
